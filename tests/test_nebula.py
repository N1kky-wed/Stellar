import pytest
import json
from unittest.mock import MagicMock, patch

def test_nebula_workflow(client):
    # Login
    client.post('/register', json={'username': 'nebula_user', 'password': 'pw'})
    client.post('/login', json={'username': 'nebula_user', 'password': 'pw'})

    # Start Nebula Step 1
    mock_response_step1 = [{'result': 'Plan: 1. Do this. 2. Do that.'}]
    with patch('app.gemini_generate', return_value=iter(mock_response_step1)):
        rv = client.post('/nebula/step', json={
            'processId': 'proc1',
            'step': 1,
            'model_id': 'gemini-3-flash-preview',
            'context': {'query': 'build a todo app', 'chat_id': 1}
        })
        assert rv.status_code == 200
        data = json.loads(rv.data)
        assert data['step'] == 1
        assert data['next_step'] == 2
        assert 'Plan:' in data['output']

    # Step 2: Frontend
    mock_response_step2 = [{'result': '```html\n<!DOCTYPE html><html><body>Todo</body></html>\n```'}]
    with patch('app.gemini_generate', return_value=iter(mock_response_step2)):
        rv = client.post('/nebula/step', json={
            'processId': 'proc1',
            'step': 2,
            'model_id': 'gemini-3-flash-preview',
            'context': {'chat_id': 1}
        })
        assert rv.status_code == 200
        data = json.loads(rv.data)
        assert data['step'] == 2
        assert data['next_step'] == 3
        assert '<!DOCTYPE html>' in data['output']

    # Step 3: Backend
    mock_response_step3 = [{'result': '```python\napp = Flask(__name__)\n```'}]
    with patch('app.gemini_generate', return_value=iter(mock_response_step3)):
        rv = client.post('/nebula/step', json={
            'processId': 'proc1',
            'step': 3,
            'model_id': 'gemini-3-flash-preview',
            'context': {'chat_id': 1}
        })
        assert rv.status_code == 200
        data = json.loads(rv.data)
        assert data['step'] == 3
        assert data['next_step'] == 4

    # Step 4: Verify (Final)
    mock_response_step4 = [{'result': 'Verification complete.'}]
    with patch('app.gemini_generate', return_value=iter(mock_response_step4)):
        rv = client.post('/nebula/step', json={
            'processId': 'proc1',
            'step': 4,
            'model_id': 'gemini-3-flash-preview',
            'context': {'chat_id': 1}
        })
        assert rv.status_code == 200
        data = json.loads(rv.data)
        assert data['status'] == 'nebula_complete'
        assert data['report_url'] is not None
