import json
from unittest.mock import MagicMock

def test_index(client):
    response = client.get('/')
    assert response.status_code == 200

def test_check_auth_logged_out(client):
    response = client.get('/check_auth')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['logged_in'] is False

def test_register_and_login(client):
    # Register
    rv = client.post('/register', json={
        'username': 'testuser',
        'password': 'testpassword'
    })
    assert rv.status_code == 201

    # Login
    rv = client.post('/login', json={
        'username': 'testuser',
        'password': 'testpassword'
    })
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert data['success'] is True

    # Check Auth again
    rv = client.get('/check_auth')
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert data['logged_in'] is True
    assert data['username'] == 'testuser'

def test_docker_mock(mock_docker_client):
    # Just to verify our mock fixture is working
    assert mock_docker_client is not None
