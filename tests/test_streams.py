import pytest
import json
from unittest.mock import MagicMock, patch

def test_register_and_refine_stream(auth_client):
    # Insert chat
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (1, 1, "Chat 1")')
        db.commit()

    # 1. Register Query
    rv = auth_client.post('/register_query', json={
        'query': 'research AI',
        'model_id': 'gemini-3-flash-preview',
        'mode': 'refine',
        'chat_id': 1
    })
    assert rv.status_code == 200
    query_id = json.loads(rv.data)['query_id']

    # 2. Mock Stream Data in Redis to test event_stream
    from app import redis_client
    redis_client.store[f"stream_started:{query_id}"] = "1"
    redis_client.store[f"stream_history:{query_id}"] = [
        "data: " + json.dumps({'result': 'Refined query: AI research'}) + "\n\n",
        "__STREAM_END__"
    ]

    # Stream response
    rv = auth_client.get(f'/refine_stream?query_id={query_id}')
    assert rv.status_code == 200
    assert rv.is_streamed

    # Consume stream
    content = b"".join(rv.response)
    assert b'Stream ended.' in content
    assert b'Refined query: AI research' in content


def test_register_query_missing_params(auth_client):
    """
    Asserts that register_query returns 400 Bad Request when required parameter is missing.
    """
    rv = auth_client.post('/register_query', json={
        'query': 'research AI',
        'model_id': 'gemini-3-flash-preview',
        'mode': 'refine'
    })
    assert rv.status_code == 400
    data = json.loads(rv.data)
    assert 'Missing required data' in data['error']


def test_register_query_invalid_chat_id(auth_client):
    """
    Asserts that register_query returns 400 Bad Request when chat_id has an invalid format.
    """
    rv = auth_client.post('/register_query', json={
        'query': 'research AI',
        'model_id': 'gemini-3-flash-preview',
        'mode': 'refine',
        'chat_id': 'abc'
    })
    assert rv.status_code == 400
    data = json.loads(rv.data)
    assert 'Invalid chat_id format' in data['error']


def test_register_query_unauthorized_chat(auth_client):
    """
    Asserts that register_query returns 403 Forbidden when user is not authorized to access the chat.
    """
    rv = auth_client.post('/register_query', json={
        'query': 'research AI',
        'model_id': 'gemini-3-flash-preview',
        'mode': 'refine',
        'chat_id': 999
    })
    assert rv.status_code == 403
    data = json.loads(rv.data)
    assert 'Unauthorized or chat not found' in data['error']


def test_refine_stream_missing_query_id(auth_client):
    """
    Asserts that refine_stream returns 400 Bad Request stream when query_id argument is missing.
    """
    rv = auth_client.get('/refine_stream')
    assert rv.status_code == 400
    assert rv.is_streamed
    content = b"".join(rv.response)
    assert b'Missing query identifier' in content


def test_refine_stream_expired_query(auth_client):
    """
    Asserts that refine_stream returns 404 Not Found stream when query has expired or is invalid.
    """
    rv = auth_client.get('/refine_stream?query_id=expired-123')
    assert rv.status_code == 404
    assert rv.is_streamed
    content = b"".join(rv.response)
    assert b'Query session expired or invalid' in content


def test_refine_stream_unauthorized_ownership(auth_client):
    """
    Asserts that refine_stream returns 403 Forbidden stream when query ownership mismatches user session.
    """
    from app import redis_client
    query_id = 'unauth-query-123'
    query_data = {
        'query': 'other user query',
        'model_id': 'gemini-3-flash-preview',
        'mode': 'refine',
        'chat_id': 1,
        'user_id': 999
    }
    redis_client.store[f"query_args:{query_id}"] = json.dumps(query_data)
    
    rv = auth_client.get(f'/refine_stream?query_id={query_id}')
    assert rv.status_code == 403
    assert rv.is_streamed
    content = b"".join(rv.response)
    assert b'Query ownership mismatch' in content


# Brief comment: Asserts that get_active_stream returns 403 when user doesn't own the chat.
def test_get_active_stream_unauthorized(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        # Insert chat owned by user 999
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (2, 999, "Other Chat")')
        db.commit()
    
    response = auth_client.get('/api/chats/2/active_stream')
    assert response.status_code == 403
    data = json.loads(response.data)
    assert data['error'] == 'Unauthorized'

# Brief comment: Asserts that get_active_stream returns 403 for a non-existent chat.
def test_get_active_stream_not_found(auth_client):
    response = auth_client.get('/api/chats/999/active_stream')
    assert response.status_code == 403
    data = json.loads(response.data)
    assert data['error'] == 'Unauthorized'

# Brief comment: Asserts that get_active_stream returns empty JSON when no stream is active.
def test_get_active_stream_empty(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (3, 1, "My Chat")')
        db.commit()
    
    # Ensure redis key is empty
    from app import redis_client
    redis_client.store.pop("chat_active_query:3", None)

    response = auth_client.get('/api/chats/3/active_stream')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data == {}

# Brief comment: Asserts that inject_message returns 400 when no JSON payload is received.
def test_inject_message_missing_json(auth_client):
    # Case A: content-type application/json but empty body raises BadRequest, which app catches and returns 500
    response = auth_client.post('/api/inject_message', content_type='application/json')
    assert response.status_code == 500

    # Case B: empty JSON dict passes get_json() but fails truthy check, returning 400
    response = auth_client.post('/api/inject_message', json={})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'No JSON data received' in data['error']

# Brief comment: Asserts that inject_message returns 400 when chat_id or message is missing.
def test_inject_message_missing_params(auth_client):
    response = auth_client.post('/api/inject_message', json={'message': 'hello'})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Missing chat_id or message' in data['error']

    response = auth_client.post('/api/inject_message', json={'chat_id': 1})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Missing chat_id or message' in data['error']

# Brief comment: Asserts that inject_message returns 400 when chat_id format is invalid.
def test_inject_message_invalid_chat_id(auth_client):
    response = auth_client.post('/api/inject_message', json={'chat_id': 'abc', 'message': 'hello'})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Invalid chat_id format' in data['error']

# Brief comment: Asserts that inject_message returns 403 when user doesn't own the chat.
def test_inject_message_unauthorized(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (4, 999, "Other Chat")')
        db.commit()

    response = auth_client.post('/api/inject_message', json={'chat_id': 4, 'message': 'hello'})
    assert response.status_code == 403
    data = json.loads(response.data)
    assert 'Unauthorized or chat not found' in data['error']

# Brief comment: Asserts that inject_message returns 409 when no active stream is running for the chat.
def test_inject_message_no_active_stream(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (5, 1, "My Chat")')
        db.commit()

    from app import redis_client
    redis_client.store.pop("chat_active_query:5", None)

    response = auth_client.post('/api/inject_message', json={'chat_id': 5, 'message': 'hello'})
    assert response.status_code == 409
    data = json.loads(response.data)
    assert 'No active stream to inject into' in data['error']

# Brief comment: Asserts that generative_ui_finish returns 400 when interaction_id is missing.
def test_generative_ui_finish_missing_id(auth_client):
    response = auth_client.post('/api/generative_ui/finish', json={'data': {}})
    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Missing interaction_id' in data['error']

# Brief comment: Asserts that generative_ui_finish returns 500 when Redis connection fails.
@patch('redis.Redis')
def test_generative_ui_finish_redis_error(mock_redis_class, auth_client):
    # Mock Redis instance to raise an exception on setex
    mock_redis_inst = MagicMock()
    mock_redis_inst.setex.side_effect = Exception("Connection refused")
    mock_redis_class.return_value = mock_redis_inst

    response = auth_client.post('/api/generative_ui/finish', json={'interaction_id': 'abc', 'data': {}})
    assert response.status_code == 500
    data = json.loads(response.data)
    assert 'Connection refused' in data['error']

