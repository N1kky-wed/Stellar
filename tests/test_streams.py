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
