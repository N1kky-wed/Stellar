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

    mock_container.exec_run.side_effect = [
        mock_exec_res_syntax, # Syntax check for JS
        MagicMock(), # Stop process
        MagicMock(), # Stop process
        MagicMock(), # Stop process
        MagicMock(), # Spawn new process
        mock_exec_res_health # Health check curl
    ]

    # Mock Gemini Client API response
    mock_client_instance = MagicMock()
    mock_genai_client_class.return_value = mock_client_instance
    
    mock_response = MagicMock()
    # Mock return value JSON
    mock_response.text = json.dumps({
        "patches": [
            {
                "file_path": "main.js",
                "content": "const x = 42; console.log(x);",
                "explanation": "Declare variable x before logging it."
            }
        ],
        "root_cause": "x was referenced without declaration."
    })
    mock_client_instance.models.generate_content.return_value = mock_response

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
