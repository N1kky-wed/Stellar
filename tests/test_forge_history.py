import json
import pytest
from unittest.mock import MagicMock, patch

def test_forge_history_flow(client, mock_docker_client):
    # Configure mock container
    mock_container = MagicMock()
    mock_container.id = "mock_container_id_123"
    mock_container.short_id = "mock_short"
    mock_container.status = "running"
    mock_container.attrs = {'NetworkSettings': {'Ports': {'5000/tcp': [{'HostPort': '1234'}]}}}
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b'')

    mock_docker_client.containers.run.return_value = mock_container
    mock_docker_client.containers.get.return_value = mock_container

    # 1. Login
    client.post('/register', json={'username': 'user1', 'password': 'pw'})
    client.post('/login', json={'username': 'user1', 'password': 'pw'})

    # 2. Mock gemini response for forge_start
    mock_gen_response = [{'result': '```json\n{"index.html": "<html></html>", "app.py": "from flask import Flask"}\n```'}]

    with patch('app.gemini_generate', return_value=iter(mock_gen_response)):
        # 3. Start Forge
        rv = client.post('/codelab/forge/start', json={'prompt': 'test app'})
        assert rv.status_code == 200
        data = json.loads(rv.data)
        assert data['success'] is True
        process_id = data['process_id']

    # 4. Check History
    # Wait a bit for thread to potentially run?
    # But we are testing sync mostly, the thread runs in background.
    # The initial insert happens before thread start.

    rv = client.get('/api/forge/history')
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert len(data['history']) == 1
    entry = data['history'][0]
    assert entry['process_id'] == process_id

    # Status might be 'starting' or 'created' depending on race.
    assert entry['status'] in ['starting', 'created', 'running', 'failed']

    history_id = entry['id']

    # 5. Resume (Load into session)
    rv = client.post(f'/api/forge/history/{history_id}/resume')
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert data['success'] is True
    assert data['files']['index.html'] == "<html></html>"

    # 6. Delete History
    rv = client.delete(f'/api/forge/history/{history_id}')
    assert rv.status_code == 200

    # 7. Check History again
    rv = client.get('/api/forge/history')
    data = json.loads(rv.data)
    assert len(data['history']) == 0

def test_forge_redeploy_history(client, mock_docker_client):
    # Configure mock container
    mock_container = MagicMock()
    mock_container.id = "mock_container_id_456"
    mock_container.short_id = "mock_short_2"
    mock_docker_client.containers.run.return_value = mock_container

    # Login
    client.post('/register', json={'username': 'user2', 'password': 'pw'})
    client.post('/login', json={'username': 'user2', 'password': 'pw'})

    # Simulate session
    with client.session_transaction() as sess:
        sess['forge_project'] = {
            'files': {"index.html": "", "app.py": ""},
            'container_id': None,
            'process_id': "old_pid"
        }

    # Redeploy
    updated_files = {"index.html": "<h1>New</h1>", "app.py": "pass"}
    rv = client.post('/codelab/forge/redeploy', json={'files': updated_files})
    assert rv.status_code == 200

    # Check history
    rv = client.get('/api/forge/history')
    data = json.loads(rv.data)
    assert len(data['history']) == 1
    assert "Redeploy" in data['history'][0]['project_name']
