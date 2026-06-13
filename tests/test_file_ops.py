import pytest
import json
import io
import os
from unittest.mock import MagicMock, patch

# Brief comment: Asserts that uploading files by an approved user is successful and returns 200.
def test_upload_files(auth_client):
    data = {
        'file': (io.BytesIO(b"content"), 'test.txt')
    }
    rv = auth_client.post('/upload_files', data=data, content_type='multipart/form-data')
    assert rv.status_code == 200
    data = json.loads(rv.data)
    assert 'test.txt' in data['uploaded_files']

# Brief comment: Asserts that uploading files without sending any files returns 400 Bad Request.
def test_upload_no_file(auth_client):
    rv = auth_client.post('/upload_files', data={}, content_type='multipart/form-data')
    assert rv.status_code == 400

# Brief comment: Asserts that uploading files without being logged in returns 401 Unauthorized.
def test_upload_files_logged_out(client):
    data = {
        'file': (io.BytesIO(b"content"), 'test.txt')
    }
    rv = client.post('/upload_files', data=data, content_type='multipart/form-data')
    assert rv.status_code == 401

# Brief comment: Asserts that uploading files when user is not approved (on waitlist) returns 403 Forbidden.
def test_upload_files_unapproved(client):
    from app import get_db
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (99, "unapproved@gmail.com", "Unapproved", "user", 0)')
        db.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = 99
        sess['username'] = "unapproved@gmail.com"
        sess['is_approved'] = False

    data = {
        'file': (io.BytesIO(b"content"), 'test.txt')
    }
    rv = client.post('/upload_files', data=data, content_type='multipart/form-data')
    assert rv.status_code == 403

# Brief comment: Asserts that uploading files returns 500 when session initialization fails (no context_id).
@patch('app.get_file_context_id', return_value=None)
def test_upload_files_session_failure(mock_get_context, auth_client):
    data = {
        'file': (io.BytesIO(b"content"), 'test.txt')
    }
    rv = auth_client.post('/upload_files', data=data, content_type='multipart/form-data')
    assert rv.status_code == 500
    res = json.loads(rv.data)
    assert 'Session initialization failed' in res['error']

# Brief comment: Asserts that downloading a file with path traversal attempts returns 400.
def test_download_invalid_path(auth_client):
    # Test client request with path traversal
    response = auth_client.get('/download/../test.txt')
    assert response.status_code == 400
    assert b"Invalid path" in response.data

    # Test filename starting with slash directly on the view function
    from app import download_file
    with auth_client.application.test_request_context():
        res, status = download_file('/absolute/path/test.txt')
        assert status == 400
        assert res == "Invalid path"

# Brief comment: Asserts that downloading a non-existent file returns 404.
def test_download_file_not_found(auth_client):
    with patch('os.path.exists', return_value=False):
        response = auth_client.get('/download/nonexistent.txt')
        assert response.status_code == 404
        data = json.loads(response.data)
        assert data['status'] == 'Failed: File not found'

# Brief comment: Asserts that downloading a file that resolves outside outputs/ returns 403.
def test_download_access_denied(auth_client):
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isfile', return_value=True), \
         patch('os.path.realpath', side_effect=lambda x: x), \
         patch('os.path.commonpath', return_value="/root/Stellar"):
        response = auth_client.get('/download/outside.txt')
        assert response.status_code == 403
        assert b"Access denied" in response.data

# Brief comment: Asserts that downloading a valid file correctly calls send_from_directory.
@patch('app.send_from_directory')
def test_download_file_success(mock_send_from_dir, auth_client):
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isfile', return_value=True), \
         patch('os.path.realpath', side_effect=lambda x: x), \
         patch('os.path.commonpath', return_value="/root/Stellar/outputs"):
        mock_send_from_dir.return_value = "file_content"
        response = auth_client.get('/download/test_report.pdf')
        assert response.status_code == 200
        assert response.data == b"file_content"
        mock_send_from_dir.assert_called_once()
        args, kwargs = mock_send_from_dir.call_args
        assert kwargs.get('as_attachment') is True

# Brief comment: Asserts that viewing a file with path traversal attempts returns 400.
def test_view_invalid_path(auth_client):
    # Test client request with path traversal
    response = auth_client.get('/view/../test.txt')
    assert response.status_code == 400
    assert b"Invalid path" in response.data

    # Test filename starting with slash directly on the view function
    from app import view_file
    with auth_client.application.test_request_context():
        res, status = view_file('/absolute/path/test.txt')
        assert status == 400
        assert res == "Invalid path"

# Brief comment: Asserts that viewing a non-existent file returns 404.
def test_view_file_not_found(auth_client):
    with patch('os.path.exists', return_value=False):
        response = auth_client.get('/view/nonexistent.txt')
        assert response.status_code == 404
        assert b"File not found" in response.data

# Brief comment: Asserts that viewing a file that resolves outside outputs/ returns 403.
def test_view_access_denied(auth_client):
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isfile', return_value=True), \
         patch('os.path.realpath', side_effect=lambda x: x), \
         patch('os.path.commonpath', return_value="/root/Stellar"):
        response = auth_client.get('/view/outside.txt')
        assert response.status_code == 403
        assert b"Access denied" in response.data

# Brief comment: Asserts that viewing files with different extensions correctly assigns mimetypes.
@patch('app.send_from_directory')
def test_view_mimetypes(mock_send_from_dir, auth_client):
    mimetype_test_cases = [
        ('test.html', 'text/html'),
        ('test.htm', 'text/html'),
        ('test.md', 'text/markdown'),
        ('test.css', 'text/css'),
        ('test.js', 'application/javascript'),
        ('test.png', 'image/png'),
        ('test.jpg', 'image/png'),
        ('test.jpeg', 'image/png'),
        ('test.mp4', 'video/mp4'),
        ('test.m4v', 'video/mp4'),
        ('test.webm', 'video/webm'),
        ('test.ogg', 'video/ogg'),
        ('test.mov', 'video/quicktime'),
        ('test.mkv', 'video/x-matroska'),
        ('test.mp3', 'audio/mpeg'),
        ('test.wav', 'audio/wav'),
        ('test.pdf', 'application/pdf'),
        ('test.zip', 'application/octet-stream'),
        ('test.tar', 'application/octet-stream'),
        ('test.gz', 'application/octet-stream'),
        ('test.7z', 'application/octet-stream'),
        ('test.rar', 'application/octet-stream'),
        ('test.json', 'application/json'),
        ('test.jsonl', 'application/json'),
        ('test.csv', 'text/csv'),
        ('test.tsv', 'text/csv'),
        ('test.unknown', 'text/plain')
    ]

    for filename, expected_mimetype in mimetype_test_cases:
        mock_send_from_dir.reset_mock()
        with patch('os.path.exists', return_value=True), \
             patch('os.path.isfile', return_value=True), \
             patch('os.path.realpath', side_effect=lambda x: x), \
             patch('os.path.commonpath', return_value="/root/Stellar/outputs"):
            
            mock_send_from_dir.return_value = "content"
            response = auth_client.get(f'/view/{filename}')
            assert response.status_code == 200
            mock_send_from_dir.assert_called_once()
            args, kwargs = mock_send_from_dir.call_args
            assert kwargs.get('mimetype') == expected_mimetype
