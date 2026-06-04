import pytest
import json
import io
from unittest.mock import MagicMock, patch

def test_upload_files(auth_client):
    data = {
        'file': (io.BytesIO(b"content"), 'test.txt')
    }
    rv = auth_client.post('/upload_files', data=data, content_type='multipart/form-data')
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert 'test.txt' in data['uploaded_files']

def test_upload_no_file(auth_client):
    rv = auth_client.post('/upload_files', data={}, content_type='multipart/form-data')
    assert rv.status_code == 400
