import pytest
import json
import io
from unittest.mock import MagicMock, patch

def test_upload_files(client):
    client.post('/register', json={'username': 'file_user', 'password': 'pw'})
    client.post('/login', json={'username': 'file_user', 'password': 'pw'})

    data = {
        'file': (io.BytesIO(b"content"), 'test.txt')
    }
    rv = client.post('/upload_files', data=data, content_type='multipart/form-data')
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert 'test.txt' in data['uploaded_files']

def test_upload_disallowed_file(client):
    client.post('/register', json={'username': 'file_user', 'password': 'pw'})
    client.post('/login', json={'username': 'file_user', 'password': 'pw'})

    data = {
        'file': (io.BytesIO(b"exe content"), 'malware.exe')
    }
    rv = client.post('/upload_files', data=data, content_type='multipart/form-data')
    # It might return 200 with skipped list or 400 depending on implementation.
    # Implementation: status_code = 200 if successful_uploads else 400
    assert rv.status_code == 400
    data = json.loads(rv.data)
    assert 'malware.exe' in data['files_disallowed']
