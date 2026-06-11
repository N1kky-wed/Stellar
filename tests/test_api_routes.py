import json
import pytest
from unittest.mock import patch, MagicMock

def test_index(client):
    response = client.get('/')
    assert response.status_code == 200

def test_check_auth_logged_out(client):
    response = client.get('/check_auth')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['logged_in'] is False

def test_check_auth_logged_in(auth_client):
    response = auth_client.get('/check_auth')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['logged_in'] is True
    assert data['username'] == 'testuser@gmail.com'

def test_get_chats_unauthorized(client):
    response = client.get('/api/chats')
    assert response.status_code == 401

def test_get_chats_authorized(auth_client):
    response = auth_client.get('/api/chats')
    assert response.status_code == 200
    chats = json.loads(response.data)
    assert isinstance(chats, list)

def test_create_new_chat(auth_client):
    response = auth_client.post('/api/chats/new')
    assert response.status_code == 201
    data = json.loads(response.data)
    assert data['success'] is True
    assert 'chat_id' in data

def test_user_profile(auth_client):
    response = auth_client.get('/api/user/profile')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    assert data['username'] == 'testuser@gmail.com'

def test_change_display_name(auth_client):
    response = auth_client.post('/api/user/change_display_name', json={'new_display_name': 'New Nikhil'})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

def test_waitlist_info(auth_client):
    response = auth_client.get('/api/user/waitlist_info')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    assert data['form_submitted'] is False

def test_submit_waitlist_form(auth_client):
    response = auth_client.post('/api/user/submit_waitlist_form', json={
        'designation': 'Researcher',
        'source': 'Search Engine',
        'use_case': 'Testing backend logic'
    })
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Re-verify waitlist info reflects submission
    response = auth_client.get('/api/user/waitlist_info')
    data = json.loads(response.data)
    assert data['form_submitted'] is True

def test_pwa_vapid_key(auth_client):
    response = auth_client.get('/api/pwa/vapid_public_key')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    assert 'publicKey' in data

def test_pwa_subscribe(auth_client):
    response = auth_client.post('/api/pwa/subscribe', json={
        'subscription': {
            'endpoint': 'https://fcm.googleapis.com/fcm/send/fake_subscription',
            'keys': {
                'p256dh': 'fake_p256dh_key_base64_encoded_string',
                'auth': 'fake_auth_secret_base64'
            }
        }
    })
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

@patch('app.send_push_notification')
def test_pwa_test_push(mock_send_push, auth_client):
    mock_send_push.return_value = 1
    response = auth_client.post('/api/pwa/test_push')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    mock_send_push.assert_called_once()

def test_logout(auth_client):
    response = auth_client.post('/logout')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    response = auth_client.get('/check_auth')
    data = json.loads(response.data)
    assert data['logged_in'] is False

# --- Additional Route Tests ---

def test_admin_impersonate(auth_client):
    # Setup: Insert target user in db
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (10, "target@gmail.com", "Target User", "user", 1)')
        db.commit()

    # Success case
    response = auth_client.post('/api/admin/impersonate', json={'user_id': 10})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Verify session is updated
    with auth_client.session_transaction() as sess:
        assert sess['user_id'] == 10
        assert sess['username'] == "target@gmail.com"

    # Reset session back to admin
    with auth_client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = "testuser@gmail.com"
        sess['role'] = "admin"
        sess['is_approved'] = True

    # Bad request (missing user_id)
    response = auth_client.post('/api/admin/impersonate', json={})
    assert response.status_code == 400

    # User not found
    response = auth_client.post('/api/admin/impersonate', json={'user_id': 999})
    assert response.status_code == 404

    # Unauthorized role
    with auth_client.session_transaction() as sess:
        sess['role'] = 'user'
    response = auth_client.post('/api/admin/impersonate', json={'user_id': 10})
    assert response.status_code == 403


@patch('app.send_approval_email')
def test_approve_user(mock_approve, auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (11, "waitlist@gmail.com", "Waitlist User", "user", 0)')
        db.commit()

    # Success case
    response = auth_client.post('/api/admin/approve', json={'user_id': 11})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Verify approved in DB
    with auth_client.application.app_context():
        db = get_db()
        row = db.execute('SELECT is_approved FROM users WHERE id = 11').fetchone()
        assert row['is_approved'] == 1

    # Non-existent
    response = auth_client.post('/api/admin/approve', json={'user_id': 999})
    assert response.status_code == 404

    # Missing user_id
    response = auth_client.post('/api/admin/approve', json={})
    assert response.status_code == 400


@patch('app.send_approval_email')
@patch('app.send_revocation_email')
def test_toggle_user_access(mock_revoke, mock_approve, auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (12, "active@gmail.com", "Active User", "user", 1)')
        db.commit()

    # Disable access
    response = auth_client.post('/api/admin/toggle_access', json={'user_id': 12, 'is_approved': False})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    with auth_client.application.app_context():
        db = get_db()
        row = db.execute('SELECT is_approved FROM users WHERE id = 12').fetchone()
        assert row['is_approved'] == 0

    # Cannot disable admin
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (13, "admin_user@gmail.com", "Admin User", "admin", 1)')
        db.commit()
    response = auth_client.post('/api/admin/toggle_access', json={'user_id': 13, 'is_approved': False})
    assert response.status_code == 400

    # Missing parameters
    response = auth_client.post('/api/admin/toggle_access', json={})
    assert response.status_code == 400


def test_get_admin_waitlist(auth_client):
    response = auth_client.get('/api/admin/waitlist')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert isinstance(data, list)


@patch('requests.get')
def test_api_check_url(mock_get, auth_client):
    # Success case
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    response = auth_client.get('/api/utils/check_url?url=http://example.com')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 200

    # Exception case
    mock_get.side_effect = Exception("Connection error")
    response = auth_client.get('/api/utils/check_url?url=http://example.com')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 500

    # Missing URL
    response = auth_client.get('/api/utils/check_url')
    assert response.status_code == 400


@patch('app.genai.Client')
def test_api_count_tokens(mock_client_class, auth_client):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_resp = MagicMock()
    mock_resp.total_tokens = 42
    mock_client.models.count_tokens.return_value = mock_resp

    # Empty text list
    response = auth_client.post('/api/utils/count_tokens', json={'text_list': []})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['token_count'] == 0

    # Non-empty text list
    response = auth_client.post('/api/utils/count_tokens', json={'text_list': ['hello']})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['token_count'] == 42


def test_api_logs_preferences(auth_client):
    # GET preferences (empty initial)
    response = auth_client.get('/api/logs_preferences')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert isinstance(data['logs'], list)

    # POST preferences
    response = auth_client.post('/api/logs_preferences', json={'logs': ['pref1', 'pref2']})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # GET preferences (should now contain values)
    response = auth_client.get('/api/logs_preferences')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert 'pref1' in data['logs']

    # DELETE preference
    response = auth_client.delete('/api/logs_preferences?index=0')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # DELETE out of bounds
    response = auth_client.delete('/api/logs_preferences?index=99')
    assert response.status_code == 400


def test_clear_history(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (20, 1, "Chat 20")')
        db.execute('INSERT OR IGNORE INTO messages (id, chat_id, message_type, message_content) VALUES (200, 20, "user", "hi")')
        db.commit()

    # Success case
    with auth_client.session_transaction() as sess:
        sess['current_chat_id'] = 20

    response = auth_client.post('/clear_history')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'Success'

    # Verify messages cleared
    with auth_client.application.app_context():
        db = get_db()
        row = db.execute('SELECT COUNT(*) FROM messages WHERE chat_id = 20').fetchone()
        assert row[0] == 0


def test_create_temp_chat(auth_client):
    response = auth_client.post('/api/chats/new_temp')
    assert response.status_code == 201
    data = json.loads(response.data)
    assert data['success'] is True
    assert 'chat_id' in data

    # Verify it is_temp in DB
    chat_id = data['chat_id']
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        row = db.execute('SELECT is_temp FROM chats WHERE id = ?', (chat_id,)).fetchone()
        assert row['is_temp'] == 1


def test_set_active_chat(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (21, 1, "Chat 21")')
        db.commit()

    # Success
    response = auth_client.post('/api/set_active_chat', json={'chat_id': 21})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Bad request (missing chat_id)
    response = auth_client.post('/api/set_active_chat', json={})
    assert response.status_code == 400

    # Unauthorized
    response = auth_client.post('/api/set_active_chat', json={'chat_id': 999})
    assert response.status_code == 403


def test_delete_chat(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (22, 1, "Chat 22")')
        db.commit()

    # Success
    response = auth_client.delete('/api/chats/22/delete')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Unauthorized
    response = auth_client.delete('/api/chats/999/delete')
    assert response.status_code == 403


@patch('app.genai.Client')
def test_update_chat_name(mock_client_class, auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (23, 1, "Old Name")')
        db.commit()

    # Mock API call
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_chat = MagicMock()
    mock_resp = MagicMock()
    # Mock return candidates structure
    part = MagicMock()
    part.text = "New Descriptive Name"
    content = MagicMock()
    content.parts = [part]
    candidate = MagicMock()
    candidate.content = content
    mock_resp.candidates = [candidate]
    mock_chat.send_message.return_value = mock_resp
    mock_client.chats.create.return_value = mock_chat

    response = auth_client.post('/api/chats/23/name', json={'first_message_content': 'Hello, how does this work?'})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    assert data['name'] == "New Descriptive Name"


def test_get_chat_tokens(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name, token_count) VALUES (24, 1, "Chat 24", 120)')
        db.commit()

    response = auth_client.get('/api/chats/24/tokens')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['token_count'] == 120

    # Unauthorized
    response = auth_client.get('/api/chats/999/tokens')
    assert response.status_code == 403


def test_delete_message(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (25, 1, "Chat 25")')
        db.execute('INSERT OR IGNORE INTO messages (id, chat_id, message_type, message_content) VALUES (300, 25, "user", "delete me")')
        db.commit()

    # Success
    response = auth_client.post('/api/messages/delete', json={'message_id': 300})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Bad request (invalid format)
    response = auth_client.post('/api/messages/delete', json={'message_id': 'abc'})
    assert response.status_code == 400

    # Unauthorized
    response = auth_client.post('/api/messages/delete', json={'message_id': 999})
    assert response.status_code == 403


def test_delete_messages_after(auth_client):
    from app import get_db
    import time
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (26, 1, "Chat 26")')
        db.execute('INSERT OR IGNORE INTO messages (id, chat_id, message_type, message_content, timestamp) VALUES (301, 26, "user", "keep", 1000)')
        db.execute('INSERT OR IGNORE INTO messages (id, chat_id, message_type, message_content, timestamp) VALUES (302, 26, "user", "delete", 2000)')
        db.commit()

    # Success
    response = auth_client.post('/api/messages/delete_after', json={'chat_id': 26, 'message_id': 302})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Verify messages count
    with auth_client.application.app_context():
        db = get_db()
        row = db.execute('SELECT COUNT(*) FROM messages WHERE chat_id = 26').fetchone()
        assert row[0] == 1


def test_get_history_route(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (27, 1, "Chat 27")')
        db.commit()

    response = auth_client.get('/get_history?chat_id=27')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert 'history' in data


def test_update_message_route(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (28, 1, "Chat 28")')
        db.execute('INSERT OR IGNORE INTO messages (id, chat_id, message_type, message_content) VALUES (303, 28, "user", "original")')
        db.commit()

    # Success
    response = auth_client.post('/update_message', json={'id': 303, 'content': 'updated'})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'Success'


@patch('app.id_token.verify_firebase_token')
def test_login_google(mock_verify, client):
    mock_verify.return_value = {
        'email': 'newuser@gmail.com',
        'iss': 'https://securetoken.google.com/stellarai-live',
        'name': 'New User',
        'picture': 'http://pic.com'
    }

    # First user should become admin
    response = client.post('/login/google', json={'id_token': 'fake_token'})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True


def test_manage_api_keys(auth_client):
    response = auth_client.post('/api/user/api_keys', json={
        'api_keys': {
            'OPENAI_API_KEY': 'sk-12345'
        }
    })
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True


@patch('app.gemini_generate')
def test_generate_visualization(mock_gen, auth_client):
    mock_gen.return_value = [{'result': '```html\n<h1>Visual</h1>\n```'}]

    response = auth_client.post('/api/visualize', json={'content': 'Quantum computing', 'message_id': 1})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    assert 'Visual' in data['html']


def test_generative_ui_finish(auth_client):
    response = auth_client.post('/api/generative_ui/finish', json={'interaction_id': 'abc', 'data': {'test': 'data'}})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'success'


def test_get_active_stream(auth_client):
    from app import redis_client
    redis_client.store["chat_active_query:1"] = '{"query_id": "test_q"}'

    # Access authorized
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (1, 1, "Chat 1")')
        db.commit()

    response = auth_client.get('/api/chats/1/active_stream')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['query_id'] == 'test_q'


def test_inject_message(auth_client):
    from app import redis_client
    redis_client.store["chat_active_query:1"] = '{"query_id": "test_q"}'

    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (1, 1, "Chat 1")')
        db.commit()

    response = auth_client.post('/api/inject_message', json={'chat_id': 1, 'message': 'New prompt'})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True


@patch('requests.get')
@patch('app.is_safe_hostname')
def test_image_proxy(mock_is_safe, mock_get, auth_client):
    mock_is_safe.return_value = (True, 'Safe')
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {'Content-Type': 'image/png'}
    mock_resp.raw.headers = {}
    mock_resp.iter_content.return_value = [b'fakeimg']
    mock_get.return_value = mock_resp

    response = auth_client.get('/image-proxy?url=https://images.unsplash.com/photo-1579546929518-9e396f3cc809')
    assert response.status_code == 200
    assert response.data == b'fakeimg'


def test_download_view_file(client):
    import os
    # Create outputs folder if not exists
    os.makedirs('outputs', exist_ok=True)
    with open('outputs/dummy.txt', 'w') as f:
        f.write('hello')

    # View
    response = client.get('/view/dummy.txt')
    assert response.status_code == 200
    assert b'hello' in response.data

    # Download
    response = client.get('/download/dummy.txt')
    assert response.status_code == 200
    assert b'hello' in response.data

    # Test symlink vulnerability
    with open('test_outside.txt', 'w') as f:
        f.write('secret_data')
    try:
        os.symlink('../test_outside.txt', 'outputs/symlink.txt')

        response_view = client.get('/view/symlink.txt')
        assert response_view.status_code == 403

        response_dl = client.get('/download/symlink.txt')
        assert response_dl.status_code == 403
    finally:
        try:
            os.unlink('outputs/symlink.txt')
        except:
            pass
        try:
            os.unlink('test_outside.txt')
        except:
            pass

    # Clean up
    try:
        os.unlink('outputs/dummy.txt')
    except:
        pass


def test_static_routes(client):
    # Service worker
    response = client.get('/service-worker.js')
    assert response.status_code == 200

    # Manifest
    response = client.get('/manifest.json')
    assert response.status_code == 200

    # Favicon
    response = client.get('/favicon.ico')
    assert response.status_code == 200

    # Other css/js assets
    for asset in ['/default.min.css', '/custom_select.css', '/custom_select.js', '/highlight.min.js', '/marked.min.js', '/turndown.js']:
        response = client.get(asset)
        assert response.status_code == 200


def test_repo_history(auth_client):
    # Add repo history entry
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT OR IGNORE INTO repo_history (id, user_id, project_name, process_id, status, files_snapshot) VALUES (1, 1, 'Proj1', 'proc1', 'running', '[]')")
        db.commit()

    # GET
    response = auth_client.get('/api/repo/history')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert len(data['history']) > 0

    # Resume
    response = auth_client.post('/api/repo/history/1/resume')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True

    # Delete
    response = auth_client.delete('/api/repo/history/1')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True


@patch('app.client')
def test_stop_container(mock_docker_client, auth_client):
    # Unsuccessful check owner
    response = auth_client.post('/api/stop_container', json={'container_id': 'fake_cid'})
    assert response.status_code == 403


def test_stop_generation(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (1, 1, "Test Chat")')
        db.commit()
    response = auth_client.post('/api/stop_generation', json={'query_id': 'fake_q', 'chat_id': 1})
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True


def test_search_messages_route(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name, is_temp) VALUES (29, 1, "AI Research Chat", 0)')
        db.execute('INSERT OR IGNORE INTO messages (id, chat_id, message_type, message_content) VALUES (304, 29, "user", "let us learn python")')
        db.commit()

    response = auth_client.get('/api/chats/search_messages?search_term=python')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert '29' in data['results']
    assert 'python' in data['results']['29']['snippet']


@patch('app.redis_client.pubsub')
def test_user_global_events(mock_pubsub_func, auth_client):
    mock_pubsub = MagicMock()
    mock_pubsub.get_message.side_effect = GeneratorExit()
    mock_pubsub_func.return_value = mock_pubsub

    response = auth_client.get('/api/user/events')
    assert response.status_code == 200
    assert response.is_streamed


@patch('app.client')
def test_run_code(mock_docker_client, auth_client):
    mock_container = MagicMock()
    mock_container.id = 'fake_container_id_123'
    mock_container.logs.return_value = [b'hello stdout']
    mock_docker_client.containers.run.return_value = mock_container

    # Test run non-server python code
    response = auth_client.post('/api/run_code', json={'code': 'print("hello")', 'language': 'python'})
    assert response.status_code == 200
    assert response.is_streamed
    # Consume stream
    content = b"".join(response.response)
    assert b'fake_container_id_123' in content
    assert b'hello stdout' in content


def test_get_admin_keys(auth_client):
    response = auth_client.get('/api/admin/keys')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert isinstance(data, list)
    if len(data) > 0:
        key_data = data[0]
        assert 'label' in key_data
        assert 'masked' in key_data
        assert 'blocks' in key_data
        assert 'global' in key_data['blocks']

