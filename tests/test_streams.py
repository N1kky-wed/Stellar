import pytest
import json
from unittest.mock import MagicMock, patch

def test_register_and_refine_stream(client):
    client.post('/register', json={'username': 'stream_user', 'password': 'pw'})
    client.post('/login', json={'username': 'stream_user', 'password': 'pw'})

    # 1. Register Query
    rv = client.post('/register_query', json={
        'query': 'research AI',
        'model_id': 'gemini-3-flash-preview',
        'mode': 'refine',
        'chat_id': 1
    })
    assert rv.status_code == 200
    query_id = json.loads(rv.data)['query_id']

    # 2. Refine Stream
    # Mock LLM response
    mock_response = [{'result': 'Refined query: AI research'}]

    with patch('app.gemini_generate', return_value=iter(mock_response)):
        # Stream response
        rv = client.get(f'/refine_stream?query_id={query_id}')
        assert rv.status_code == 200
        assert rv.is_streamed

        # Consume stream
        content = b"".join(rv.response)
        assert b'refined_ready' in content
        assert b'Refined query: AI research' in content

def test_search_stream(client):
    client.post('/register', json={'username': 'stream_user', 'password': 'pw'})
    client.post('/login', json={'username': 'stream_user', 'password': 'pw'})

    rv = client.post('/register_query', json={
        'query': 'search stuff',
        'model_id': 'gemini-3-flash-preview',
        'mode': 'search_tavily',
        'chat_id': 1
    })
    query_id = json.loads(rv.data)['query_id']

    # Mock Tavily and LLM
    mock_tavily_resp = json.dumps({'tool': 'tavily_search', 'data': {'answer': 'Summary', 'results': [{'url': 'http://example.com'}]}})

    with patch('agent_tools.web_search', return_value=mock_tavily_resp):
        with patch('app.scrape_url', return_value="Content"):
            with patch('app.gemini_generate', side_effect=[
                iter([{'result': 'Analysis'}]), # Analysis
                iter([{'result': 'Final Paper'}]) # Expansion
            ]):
                rv = client.get(f'/search_stream?query_id={query_id}')
                assert rv.status_code == 200
                content = b"".join(rv.response)
                assert b'display_result' in content
                assert b'Final Paper' in content
