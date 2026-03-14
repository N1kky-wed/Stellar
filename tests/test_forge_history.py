import json
import pytest
import time
from unittest.mock import patch, MagicMock
from app import get_db

def test_forge_history(client, mocker):
    # Register and login first to avoid 401
    client.post('/register', json={'username': 'user1', 'password': 'pw'})
    client.post('/login', json={'username': 'user1', 'password': 'pw'})

    # Mock the LLM to return exactly what's needed without using real API key
    mock_gen_response = [{'result': '```json\n{"index.html": "<html></html>", "app.py": "from flask import Flask"}\n```'}]
    mocker.patch('app.gemini_generate', return_value=iter(mock_gen_response))
    mocker.patch('app.generate_forge_title', return_value='Test Forge')


    # Mock docker
    mock_container = MagicMock()
    mock_container.id = "mock_container_id_123"
    mock_container.short_id = "mock_short"
    mock_container.status = "running"
    mock_container.attrs = {'NetworkSettings': {'Ports': {'5000/tcp': [{'HostPort': '1234'}]}}}
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=[(b'mock log', None)])
    mock_container.logs.return_value = [b"mock logs"]

    mock_docker_client = MagicMock()
    mock_docker_client.containers.run.return_value = mock_container
    mock_docker_client.containers.get.return_value = mock_container
    mocker.patch('app.client', mock_docker_client)

    mocker.patch('app.tavily_search', return_value={"results": []})

    # start forge
    resp = client.post("/codelab/forge/start", json={"prompt": "build hello world"})
    assert resp.status_code == 200

    # Wait for the background thread to do its work
    time.sleep(2)

    # connect to stream
    process_id = json.loads(resp.data)['process_id']
    stream = client.get(f"/codelab/forge/stream?process_id={process_id}")

    events = []
    for line in stream.response:
        print("STREAM LINE:", line)
        if b"ide_view" in line:
            events.append(line)
            break
        if b"__STREAM_END__" in line:
            break

    # verify workspace artifact created
    from app import redis_client
    redis_key = f"forge:process:{process_id}"
    files_json = redis_client.hget(redis_key, "files")
    if files_json:
        workspace = {"files": json.loads(files_json)}
    else:
        workspace = {"files": {}}
    assert "index.html" in workspace["files"]
