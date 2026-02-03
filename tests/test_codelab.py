import pytest
import json
from unittest.mock import MagicMock, patch

def test_codelab_generate_problem(client):
    client.post('/register', json={'username': 'code_user', 'password': 'pw'})
    client.post('/login', json={'username': 'code_user', 'password': 'pw'})

    mock_problem_json = {
        "title": "Sum Two",
        "description": "Add two numbers",
        "difficulty": "Easy",
        "topic_tags": "Math",
        "test_cases": [{"input_data": "[1, 2]", "expected_output": "3", "is_hidden": False}]
    }
    mock_response = [{'result': f'```json\n{json.dumps(mock_problem_json)}\n```'}]

    with patch('app.gemini_generate', return_value=iter(mock_response)):
        rv = client.post('/codelab/generate_problem', json={'user_request': 'simple math'})
        assert rv.status_code == 200
        data = json.loads(rv.data)
        assert data['success'] is True
        assert 'new_problem_id' in data

def test_codelab_submit_solution(client, mock_docker_client):
    client.post('/register', json={'username': 'code_user', 'password': 'pw'})
    client.post('/login', json={'username': 'code_user', 'password': 'pw'})

    # 1. Get a problem (mock DB first)
    from app import get_db
    with client.application.app_context():
        db = get_db()
        db.execute("INSERT INTO problems (title, description, difficulty, topic_tags) VALUES ('Test', 'Desc', 'Easy', 'Tag')")
        pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO test_cases (problem_id, input_data, expected_output) VALUES (?, '[1]', '1')", (pid,))
        db.commit()

    # 2. Submit solution
    mock_container = MagicMock()
    mock_container.decode.return_value = json.dumps({"status": "Accepted", "results": [], "final_summary": "Passed"})
    mock_docker_client.containers.run.return_value = mock_container

    rv = client.post('/codelab/submit', json={
        'problem_id': pid,
        'code': 'def sol(x): return x',
        'language': 'python',
        'run_type': 'submit'
    })
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert data['status'] == 'Accepted'
