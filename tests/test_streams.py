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

