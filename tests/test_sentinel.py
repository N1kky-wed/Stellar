import json
import os
import pytest
from unittest.mock import patch, MagicMock
from app import get_db
import sqlite3

@pytest.fixture
def setup_repo_history(client):
    # Register a mock subdomain mapping in repo_history
    with patch('app.get_db') as mock_get_db:
        db = get_db()
        db.execute(
            "INSERT INTO repo_history (user_id, project_name, process_id, container_id, status, subdomain) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "Test Sentinel App", "test-process-123", "test-container-123", "running", "mysubdomain")
        )
        db.commit()
    yield
    # Clean up after test
    db = get_db()
    db.execute("DELETE FROM repo_history WHERE process_id = 'test-process-123'")
    db.execute("DELETE FROM sentinel_app_errors WHERE process_id = 'test-process-123'")
    db.commit()

@patch('app.redis_client')
def test_log_error_invalid_subdomain(mock_redis, client):
    # Test error logging without mapping
    payload = {
        "url": "https://unknown.stellarai.live/some/path",
        "error": {
            "type": "js_error",
            "message": "Uncaught ReferenceError: x is not defined",
            "stack": "ReferenceError: x is not defined at main.js:10",
            "source": "main.js",
            "line": 10
        }
    }
    # Mock redis lpush
    mock_redis.lpush.return_value = 1

    response = client.post('/api/sentinel/log_error', json=payload)
    assert response.status_code == 404
    data = json.loads(response.data)
    assert "No deployment mapping found" in data['error']

@patch('app.redis_client')
def test_log_error_success(mock_redis, setup_repo_history, client):
    # Test error logging with valid mapping
    payload = {
        "url": "https://mysubdomain.stellarai.live/some/path",
        "error": {
            "type": "js_error",
            "message": "Uncaught ReferenceError: x is not defined",
            "stack": "ReferenceError: x is not defined at main.js:10",
            "source": "main.js",
            "line": 10
        }
    }
    
    # Mock redis lpush
    mock_redis.lpush.return_value = 1

    with client.session_transaction() as sess:
        sess['user_id'] = 1

    response = client.post('/api/sentinel/log_error', json=payload)
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'success'
    assert 'error_id' in data
    
    # Verify DB entry
    db = get_db()
    cursor = db.execute("SELECT * FROM sentinel_app_errors WHERE process_id = 'test-process-123'")
    row = cursor.fetchone()
    assert row is not None
    assert row['error_type'] == 'js_error'
    assert row['error_message'] == 'Uncaught ReferenceError: x is not defined'
    assert row['affected_file'] == 'main.js'
    assert row['affected_line'] == 10
    assert row['status'] == 'open'

    # Check redis queue push was called
    mock_redis.lpush.assert_called_once()


@patch('app.redis_client')
def test_log_error_non_owner(mock_redis, setup_repo_history, client):
    # Test error logging with valid mapping but visitor is a different user (not owner)
    payload = {
        "url": "https://mysubdomain.stellarai.live/some/path",
        "error": {
            "type": "js_error",
            "message": "Uncaught ReferenceError: x is not defined",
            "stack": "ReferenceError: x is not defined at main.js:10",
            "source": "main.js",
            "line": 10
        }
    }
    
    # Authenticate as a different user (user_id = 999, owner is 1)
    with client.session_transaction() as sess:
        sess['user_id'] = 999

    response = client.post('/api/sentinel/log_error', json=payload)
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'success'
    
    # Check that redis queue push was NOT called (healing skipped)
    mock_redis.lpush.assert_not_called()


@patch('app.redis_client')
def test_log_error_anonymous(mock_redis, setup_repo_history, client):
    # Test error logging with valid mapping but visitor is anonymous (no session)
    payload = {
        "url": "https://mysubdomain.stellarai.live/some/path",
        "error": {
            "type": "js_error",
            "message": "Uncaught ReferenceError: x is not defined",
            "stack": "ReferenceError: x is not defined at main.js:10",
            "source": "main.js",
            "line": 10
        }
    }
    
    # Ensure no session is active (anonymous visitor)
    with client.session_transaction() as sess:
        sess.clear()

    response = client.post('/api/sentinel/log_error', json=payload)
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'success'
    
    # Check that redis queue push was NOT called (healing skipped)
    mock_redis.lpush.assert_not_called()




@patch('sentinel_healer.docker.from_env')
@patch('sentinel_healer.genai.Client')
def test_healer_workflow_success(mock_genai_client_class, mock_docker_env, setup_repo_history, client):
    # Create an error DB entry first
    db = get_db()
    cursor = db.execute(
        "INSERT INTO sentinel_app_errors (process_id, error_type, error_message, stack_trace, affected_file, affected_line) VALUES (?, ?, ?, ?, ?, ?)",
        ("test-process-123", "js_error", "ReferenceError: x is not defined", "traceback", "main.js", 10)
    )
    db.commit()
    error_id = cursor.lastrowid

    # Mock Docker SDK objects
    mock_docker_client = MagicMock()
    mock_docker_env.return_value = mock_docker_client
    
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.attrs = {
        "Mounts": [
            {
                "Source": "/tmp/mock_sandbox_mount",
                "Destination": "/app"
            }
        ]
    }
    mock_docker_client.containers.get.return_value = mock_container

    # Set up mock host mount directory on filesystem
    import shutil
    mock_host_dir = "/tmp/mock_sandbox_mount"
    os.makedirs(mock_host_dir, exist_ok=True)
    with open(os.path.join(mock_host_dir, "main.js"), "w") as f:
        f.write("console.log(x);")
    
    # Mock exec checks: syntax compilation should exit with 0 (success)
    mock_exec_res_syntax = MagicMock()
    mock_exec_res_syntax.exit_code = 0
    
    # Health check curl should return HTTP 200
    mock_exec_res_health = MagicMock()
    mock_exec_res_health.exit_code = 0
    mock_exec_res_health.output = b"200"

    def dynamic_exec_run(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "node" in cmd_str and "-c" in cmd_str:
            return mock_exec_res_syntax
        if "python3" in cmd_str and "py_compile" in cmd_str:
            return mock_exec_res_syntax
        if "curl" in cmd_str:
            return mock_exec_res_health
        return MagicMock()
    mock_container.exec_run.side_effect = dynamic_exec_run

    # Mock Gemini Client API response
    mock_client_instance = MagicMock()
    mock_genai_client_class.return_value = mock_client_instance
    
    mock_chat = MagicMock()
    mock_client_instance.chats.create.return_value = mock_chat
    
    mock_response = MagicMock()
    mock_chat.send_message.return_value = mock_response
    
    mock_part = MagicMock()
    mock_part.text = json.dumps({
        "patches": [
            {
                "file_path": "main.js",
                "full_content": "const x = 42; console.log(x);",
                "explanation": "Declare variable x before logging it."
            }
        ],
        "root_cause": "x was referenced without declaration."
    })
    mock_candidate = MagicMock()
    mock_candidate.content.parts = [mock_part]
    mock_response.candidates = [mock_candidate]

    # Mock Redis client
    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    # Call heal_application
    from sentinel_healer import heal_application
    with patch('sentinel_healer.get_db_conn') as mock_healer_db:
        import app
        def get_test_conn():
            conn = sqlite3.connect(app.DATABASE_NAME)
            conn.row_factory = sqlite3.Row
            return conn
        mock_healer_db.side_effect = get_test_conn
        
        # Patch SANDBOX_DIR to point to /tmp
        with patch('sentinel_healer.SANDBOX_DIR', '/tmp'):
            heal_application("test-process-123", error_id, mock_redis)

    # Verify patches updated the host file content
    with open(os.path.join(mock_host_dir, "main.js"), "r") as f:
        patched_content = f.read()
    assert patched_content == "const x = 42; console.log(x);"

    # Verify DB error status was updated to healed
    db = get_db()
    cursor = db.execute("SELECT status FROM sentinel_app_errors WHERE id = ?", (error_id,))
    row = cursor.fetchone()
    assert row['status'] == 'healed'

    # Verify patch was logged in sentinel_app_patches
    cursor = db.execute("SELECT status FROM sentinel_app_patches WHERE error_id = ?", (error_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row['status'] == 'applied'

    # Clean up mock directories
    shutil.rmtree(mock_host_dir, ignore_errors=True)


# --- Sentinel Route Tests ---

@patch('app.redis_client')
def test_sentinel_status_no_url(mock_redis, client):
    """
    Asserts that sentinel status route returns healing: False if url param is missing or empty.
    """
    response = client.get('/api/sentinel/status')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is False

@patch('app.redis_client')
def test_sentinel_status_no_subdomain(mock_redis, client):
    """
    Asserts that sentinel status route returns healing: False if subdomain cannot be parsed.
    """
    response = client.get('/api/sentinel/status?url=invalid_url')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is False

@patch('app.redis_client')
def test_sentinel_status_no_mapping(mock_redis, client):
    """
    Asserts that sentinel status route returns healing: False if subdomain does not map
    to any process in repo_history.
    """
    response = client.get('/api/sentinel/status?url=https://unknown.stellarai.live')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is False

@patch('app.redis_client')
def test_sentinel_status_not_healing(mock_redis, setup_repo_history, client):
    """
    Asserts that sentinel status route returns healing: False if subdomain has a process
    but Redis key "sentinel:healing:<process_id>" is not set.
    """
    mock_redis.get.return_value = None
    response = client.get('/api/sentinel/status?url=https://mysubdomain.stellarai.live')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is False
    mock_redis.get.assert_called_with("sentinel:healing:test-process-123")

@patch('app.redis_client')
def test_sentinel_status_healing(mock_redis, setup_repo_history, client):
    """
    Asserts that sentinel status route returns healing: True if subdomain has a process
    and Redis key "sentinel:healing:<process_id>" is set.
    """
    mock_redis.get.return_value = b"1"
    response = client.get('/api/sentinel/status?url=https://mysubdomain.stellarai.live')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is True
    mock_redis.get.assert_called_with("sentinel:healing:test-process-123")

@patch('app.redis_client')
def test_sentinel_stream_flow(mock_redis, client):
    """
    Asserts that the sentinel stream SSE endpoint connects, replays history from Redis,
    drains live events from pubsub, and terminates when a healed/failed event is received.
    """
    # Mock Redis lrange (history replay)
    mock_redis.lrange.return_value = [
        json.dumps({'event': 'started', 'message': 'Healing started'}).encode('utf-8'),
        json.dumps({'event': 'patching', 'message': 'Applying patch...'}).encode('utf-8')
    ]

    # Mock Redis pubsub
    mock_pubsub = MagicMock()
    mock_redis.pubsub.return_value = mock_pubsub
    
    # Mock pubsub listen to yield one live status event and then a terminal healed event
    mock_pubsub.listen.return_value = [
        {
            'type': 'message',
            'data': json.dumps({'event': 'testing', 'message': 'Running build tests'}).encode('utf-8')
        },
        {
            'type': 'message',
            'data': json.dumps({'event': 'healed', 'message': 'Healed successfully!'}).encode('utf-8')
        }
    ]

    response = client.get('/api/sentinel/stream/test-process-123')
    assert response.status_code == 200
    assert response.is_streamed

    # Consume the SSE stream
    content = b"".join(response.response)

    # Check that connected event, history, and live events are all in the stream output
    assert b"Connected to Sentinel Healer" in content
    assert b"Healing started" in content
    assert b"Applying patch..." in content
    assert b"Running build tests" in content
    assert b"Healed successfully!" in content

    # Verify pubsub lifecycle methods were called
    mock_pubsub.subscribe.assert_called_once_with("sentinel:logs:test-process-123")
    mock_pubsub.unsubscribe.assert_called_once_with("sentinel:logs:test-process-123")
    mock_pubsub.close.assert_called_once()

def test_test_sentinel_overlay_route(client):
    """
    Asserts that the test sentinel overlay route renders correctly (200 OK)
    and contains correct app details.
    """
    response = client.get('/test-sentinel-overlay')
    assert response.status_code == 200
    assert b"TestApp" in response.data
    assert b"test-id" in response.data


