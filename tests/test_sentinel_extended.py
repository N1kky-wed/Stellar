"""
test_sentinel_extended.py
=========================
Extended unit and integration tests for sentinel_healer.py, targeting previously untested paths:
  - get_working_api_key (selection, blocked key rotation, fallback)
  - detect_startup_command (server.js, package.json, app.py, fallbacks)
  - stop_application_server (python vs node vs npm execution, exception handling)
  - heal_application locking collision (Redis nx set fails)
  - heal_application error handling and rollback paths (missing DB error row, container not running, syntax errors, health checks, rollback failure)
  - start_sentinel_healer / stop_sentinel_healer / _healer_loop execution
"""

import os
import json
import sqlite3
import shutil
import tempfile
import time
import pytest
from unittest.mock import patch, MagicMock

import sentinel_healer


# ===========================================================================
# 1. get_working_api_key Tests
# ===========================================================================

def test_get_working_api_key_success():
    """
    Asserts that get_working_api_key returns the primary API key when it's not blocked.
    """
    mock_key_manager = MagicMock()
    mock_key_manager.is_key_blocked.return_value = (False, None)
    
    with patch('app.PRIMARY_API_KEY', 'primary-key-123'), \
         patch('app.BACKUP_API_KEYS', ['backup-1', 'backup-2']), \
         patch('app.KEY_MANAGER', mock_key_manager):
        key = sentinel_healer.get_working_api_key()
        assert key == 'primary-key-123'
        mock_key_manager.is_key_blocked.assert_called_with('primary-key-123', 'gemini-3.5-flash')


def test_get_working_api_key_fallback_on_blocked():
    """
    Asserts that when keys are blocked, it rotates and selects the first unblocked key.
    """
    mock_key_manager = MagicMock()
    # Mock return values for is_key_blocked: primary is blocked, first backup is not
    def is_blocked_side_effect(k, model):
        if k == 'primary-key-123':
            return (True, 'blocked')
        return (False, None)
    mock_key_manager.is_key_blocked.side_effect = is_blocked_side_effect
    
    with patch('app.PRIMARY_API_KEY', 'primary-key-123'), \
         patch('app.BACKUP_API_KEYS', ['backup-1', 'backup-2']), \
         patch('app.KEY_MANAGER', mock_key_manager):
        key = sentinel_healer.get_working_api_key()
        assert key == 'backup-1'


def test_get_working_api_key_fallback_all_blocked():
    """
    Asserts that if all keys are blocked, it falls back to the primary key.
    """
    mock_key_manager = MagicMock()
    mock_key_manager.is_key_blocked.return_value = (True, 'blocked')
    
    with patch('app.PRIMARY_API_KEY', 'primary-key-123'), \
         patch('app.BACKUP_API_KEYS', ['backup-1', 'backup-2']), \
         patch('app.KEY_MANAGER', mock_key_manager):
        key = sentinel_healer.get_working_api_key()
        assert key == 'primary-key-123'


def test_get_working_api_key_exception_fallback():
    """
    Asserts that if an exception is thrown in the block check, it returns the env var or fallback.
    """
    with patch('app.KEY_MANAGER', side_effect=Exception("DB Error")), \
         patch.dict(os.environ, {'PRIMARY_API_KEY': 'env-primary-key'}):
        key = sentinel_healer.get_working_api_key()
        assert key == 'env-primary-key'


# ===========================================================================
# 2. detect_startup_command Tests
# ===========================================================================

def test_detect_startup_command_server_js():
    """
    Asserts that if server.js exists in the directory, node server.js is returned.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create server.js
        with open(os.path.join(tmpdir, 'server.js'), 'w') as f:
            f.write('')
        cmd = sentinel_healer.detect_startup_command(tmpdir)
        assert cmd == "node server.js"


def test_detect_startup_command_package_json_with_start():
    """
    Asserts that if package.json has a scripts.start property, npm start is returned.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        package_json = {
            "scripts": {
                "start": "node app.js"
            }
        }
        with open(os.path.join(tmpdir, 'package.json'), 'w') as f:
            json.dump(package_json, f)
        cmd = sentinel_healer.detect_startup_command(tmpdir)
        assert cmd == "npm start"


def test_detect_startup_command_package_json_no_start():
    """
    Asserts that if package.json has no scripts.start, node server.js is returned.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        package_json = {"name": "test-project"}
        with open(os.path.join(tmpdir, 'package.json'), 'w') as f:
            json.dump(package_json, f)
        cmd = sentinel_healer.detect_startup_command(tmpdir)
        assert cmd == "node server.js"


def test_detect_startup_command_package_json_corrupt():
    """
    Asserts that if package.json is corrupt, it falls back to node server.js or check app.py.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, 'package.json'), 'w') as f:
            f.write('not-json')
        cmd = sentinel_healer.detect_startup_command(tmpdir)
        assert cmd == "node server.js"


def test_detect_startup_command_app_py():
    """
    Asserts that if app.py exists, python app.py is returned.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, 'app.py'), 'w') as f:
            f.write('')
        cmd = sentinel_healer.detect_startup_command(tmpdir)
        assert cmd == "python app.py"


def test_detect_startup_command_listdir_fallbacks():
    """
    Asserts that if listdir works, it detects python app.py or node server.js.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # We don't write files directly, but mock listdir
        with patch('os.listdir', return_value=['app.py']):
            cmd = sentinel_healer.detect_startup_command(tmpdir)
            assert cmd == "python app.py"

        with patch('os.listdir', return_value=['server.js']):
            cmd = sentinel_healer.detect_startup_command(tmpdir)
            assert cmd == "node server.js"


def test_detect_startup_command_fallback():
    """
    Asserts that the ultimate fallback is python app.py.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Empty directory
        cmd = sentinel_healer.detect_startup_command(tmpdir)
        assert cmd == "python app.py"


# ===========================================================================
# 3. stop_application_server Tests
# ===========================================================================

def test_stop_application_server_python():
    """
    Asserts that stop_application_server executes python kill script and fallback pkills.
    """
    mock_container = MagicMock()
    sentinel_healer.stop_application_server(mock_container, "python app.py")
    
    # Check that exec_run was called several times
    calls = mock_container.exec_run.call_args_list
    assert len(calls) >= 6
    
    # First call should be python3 -c to run the procedural proc-walk kill script
    first_cmd = calls[0][0][0]
    assert first_cmd[0] == "python3"
    assert first_cmd[1] == "-c"
    assert "app.py" in first_cmd[2] or "python" in first_cmd[2]

    # Verify fallback pkills are sent
    flat_cmds = [" ".join(call[0][0]) if isinstance(call[0][0], list) else call[0][0] for call in calls]
    assert any("pkill -9 python" in cmd for cmd in flat_cmds)
    assert any("pkill -f 'python app.py'" in cmd for cmd in flat_cmds)


def test_stop_application_server_node():
    """
    Asserts that stop_application_server target keywords are adjusted for node applications.
    """
    mock_container = MagicMock()
    sentinel_healer.stop_application_server(mock_container, "node server.js")
    
    calls = mock_container.exec_run.call_args_list
    assert len(calls) >= 6
    first_cmd = calls[0][0][0]
    assert "node" in first_cmd[2] and "npm" in first_cmd[2]


def test_stop_application_server_exception():
    """
    Asserts that stop_application_server handles exceptions in the proc-walk script
    but still attempts and propagates exceptions on fallback pkill execs.
    """
    mock_container = MagicMock()
    # First call fails, rest succeed
    mock_container.exec_run.side_effect = [Exception("Docker disconnected"), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()]
    
    # Should not raise exception because the first one is caught in try/except
    sentinel_healer.stop_application_server(mock_container, "python app.py")


# ===========================================================================
# 4. heal_application Locking Collision Tests
# ===========================================================================

def test_heal_application_lock_collision():
    """
    Asserts that if lock cannot be acquired, it requeues the task and returns.
    """
    mock_redis = MagicMock()
    mock_redis.set.return_value = False  # Lock acquisition failed
    
    with patch('sentinel_healer.time.sleep') as mock_sleep:
        sentinel_healer.heal_application("process-1", 101, mock_redis)
        mock_sleep.assert_called_once_with(2)
        mock_redis.lpush.assert_called_once()
        # Verify it put correct payload in queue
        queued_payload = json.loads(mock_redis.lpush.call_args[0][1])
        assert queued_payload['process_id'] == 'process-1'
        assert queued_payload['error_id'] == 101


# ===========================================================================
# 5. heal_application Error & Rollback Cases
# ===========================================================================

@pytest.fixture
def mock_db_path():
    db_fd, db_path = tempfile.mkstemp()
    conn = sqlite3.connect(db_path)
    # Create tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sentinel_app_errors (
            id INTEGER PRIMARY KEY,
            process_id TEXT,
            error_type TEXT,
            error_message TEXT,
            stack_trace TEXT,
            affected_file TEXT,
            affected_line INTEGER,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sentinel_app_patches (
            id INTEGER PRIMARY KEY,
            error_id INTEGER,
            patch_diff TEXT,
            status TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repo_history (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            project_name TEXT,
            process_id TEXT,
            container_id TEXT,
            status TEXT,
            subdomain TEXT,
            files_snapshot TEXT
        )
    """)
    conn.commit()
    conn.close()
    yield db_path
    os.close(db_fd)
    try:
        os.unlink(db_path)
    except:
        pass


def test_heal_application_missing_error_row(mock_db_path):
    """
    Asserts that if the error record is missing in SQLite, a ValueError is raised and logged.
    """
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    
    with patch('sentinel_healer.DATABASE_NAME', mock_db_path):
        # We don't insert any error row
        sentinel_healer.heal_application("process-missing", 999, mock_redis)
        
        # Verify status became failed
        mock_redis.publish.assert_any_call(
            "sentinel:logs:process-missing",
            '{"event": "failed", "message": "Healing execution failed: Error 999 not found in database.", "stage": "Healing Suspended"}'
        )


@patch('sentinel_healer.docker.from_env')
def test_heal_application_container_not_running(mock_docker_env, mock_db_path):
    """
    Asserts that if the container exists but is not running, healing fails and logs.
    """
    # Setup DB
    conn = sqlite3.connect(mock_db_path)
    conn.execute(
        "INSERT INTO sentinel_app_errors (id, process_id, error_type, error_message, stack_trace, affected_file, affected_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (10, "process-10", "runtime", "some error", "traceback", "app.py", 10)
    )
    conn.commit()
    conn.close()

    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    # Setup Docker mock: Container not running
    mock_docker_client = MagicMock()
    mock_docker_env.return_value = mock_docker_client
    mock_container = MagicMock()
    mock_container.status = "stopped"
    mock_docker_client.containers.get.return_value = mock_container

    with patch('sentinel_healer.DATABASE_NAME', mock_db_path):
        sentinel_healer.heal_application("process-10", 10, mock_redis)
        
        # Verify failed status published
        calls = mock_redis.publish.call_args_list
        assert any("is not running" in call[0][1] for call in calls)


@patch('sentinel_healer.docker.from_env')
@patch('sentinel_healer.genai.Client')
def test_heal_application_syntax_validation_failure(mock_genai_client_class, mock_docker_env, mock_db_path):
    """
    Asserts that when the generated patch has syntax errors, healing fails, rolls back, and restores files.
    """
    # Setup DB
    conn = sqlite3.connect(mock_db_path)
    conn.execute(
        "INSERT INTO sentinel_app_errors (id, process_id, error_type, error_message, stack_trace, affected_file, affected_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (20, "process-20", "runtime", "x is not defined", "traceback", "app.py", 10)
    )
    conn.execute(
        "INSERT INTO repo_history (process_id, status) VALUES (?, ?)",
        ("process-20", "running")
    )
    conn.commit()
    conn.close()

    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    # Docker mock
    mock_docker_client = MagicMock()
    mock_docker_env.return_value = mock_docker_client
    mock_container = MagicMock()
    mock_container.status = "running"
    
    with tempfile.TemporaryDirectory() as host_dir:
        with open(os.path.join(host_dir, "app.py"), "w") as f:
            f.write("print('baseline')")

        mock_container.attrs = {
            "Mounts": [{"Source": host_dir, "Destination": "/app"}]
        }
        mock_docker_client.containers.get.return_value = mock_container

        # Mock exec_run for syntax compile: fail with exit_code 1
        mock_exec_res = MagicMock()
        mock_exec_res.exit_code = 1
        mock_exec_res.output = b"SyntaxError: invalid syntax"
        mock_container.exec_run.return_value = mock_exec_res

        # Mock Gemini
        mock_client_instance = MagicMock()
        mock_genai_client_class.return_value = mock_client_instance
        mock_chat = MagicMock()
        mock_client_instance.chats.create.return_value = mock_chat
        
        mock_response = MagicMock()
        mock_part = MagicMock()
        mock_part.text = json.dumps({
            "patches": [{
                "file_path": "app.py",
                "full_content": "invalid python code :",
                "explanation": "Add syntax error"
            }],
            "root_cause": "test syntax error"
        })
        mock_response.candidates = [MagicMock(content=MagicMock(parts=[mock_part]))]
        mock_chat.send_message.return_value = mock_response

        # Run healing
        with patch('sentinel_healer.DATABASE_NAME', mock_db_path), \
             patch('sentinel_healer.SANDBOX_DIR', tempfile.gettempdir()):
            sentinel_healer.heal_application("process-20", 20, mock_redis)

        # Assert baseline was restored due to syntax check failure
        with open(os.path.join(host_dir, "app.py"), "r") as f:
            restored_content = f.read()
        assert restored_content == "print('baseline')"

        # Check DB status remains open
        conn = sqlite3.connect(mock_db_path)
        row = conn.execute("SELECT status FROM sentinel_app_errors WHERE id = 20").fetchone()
        assert row[0] == 'open'
        
        # Verify failed patch logged in patches table
        patch_row = conn.execute("SELECT status FROM sentinel_app_patches WHERE error_id = 20").fetchone()
        assert patch_row[0] == 'failed_test'
        conn.close()


@patch('sentinel_healer.docker.from_env')
@patch('sentinel_healer.genai.Client')
def test_heal_application_health_check_failure(mock_genai_client_class, mock_docker_env, mock_db_path):
    """
    Asserts that if syntax passes but health check returns HTTP 500, rollback is triggered.
    """
    conn = sqlite3.connect(mock_db_path)
    conn.execute(
        "INSERT INTO sentinel_app_errors (id, process_id, error_type, error_message, stack_trace, affected_file, affected_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (30, "process-30", "runtime", "exception", "traceback", "app.py", 10)
    )
    conn.commit()
    conn.close()

    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    mock_docker_client = MagicMock()
    mock_docker_env.return_value = mock_docker_client
    mock_container = MagicMock()
    mock_container.status = "running"
    
    with tempfile.TemporaryDirectory() as host_dir:
        with open(os.path.join(host_dir, "app.py"), "w") as f:
            f.write("print('baseline')")

        mock_container.attrs = {
            "Mounts": [{"Source": host_dir, "Destination": "/app"}]
        }
        mock_docker_client.containers.get.return_value = mock_container

        # Mock exec_run: syntax passes (exit_code 0) but health check (curl) returns HTTP 500
        mock_exec_syntax = MagicMock()
        mock_exec_syntax.exit_code = 0
        
        mock_exec_health = MagicMock()
        mock_exec_health.exit_code = 0
        mock_exec_health.output = b"500"  # HTTP 500
        
        mock_exec_logs = MagicMock()
        mock_exec_logs.exit_code = 0
        mock_exec_logs.output = b"Server crash log dump..."

        def dynamic_exec(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and "py_compile" in " ".join(cmd):
                return mock_exec_syntax
            if isinstance(cmd, str) and "curl" in cmd:
                return mock_exec_health
            if isinstance(cmd, str) and "cat app.log" in cmd:
                return mock_exec_logs
            return MagicMock()
        mock_container.exec_run.side_effect = dynamic_exec

        # Mock Gemini
        mock_client_instance = MagicMock()
        mock_genai_client_class.return_value = mock_client_instance
        mock_chat = MagicMock()
        mock_client_instance.chats.create.return_value = mock_chat
        
        mock_response = MagicMock()
        mock_part = MagicMock()
        mock_part.text = json.dumps({
            "patches": [{
                "file_path": "app.py",
                "full_content": "print('patched')",
                "explanation": "Apply fix"
            }],
            "root_cause": "test health check error"
        })
        mock_response.candidates = [MagicMock(content=MagicMock(parts=[mock_part]))]
        mock_chat.send_message.return_value = mock_response

        # Run healing
        with patch('sentinel_healer.DATABASE_NAME', mock_db_path), \
             patch('sentinel_healer.SANDBOX_DIR', tempfile.gettempdir()):
            sentinel_healer.heal_application("process-30", 30, mock_redis)

        # Assert baseline was restored
        with open(os.path.join(host_dir, "app.py"), "r") as f:
            restored_content = f.read()
        assert restored_content == "print('baseline')"


# ===========================================================================
# 6. start_sentinel_healer & stop_sentinel_healer Tests
# ===========================================================================

def test_start_stop_sentinel_healer():
    """
    Asserts that start_sentinel_healer is a no-op when Sentinel Healer is disabled.
    """
    sentinel_healer._healer_thread = None
    sentinel_healer.start_sentinel_healer()
    assert sentinel_healer._healer_thread is None
    sentinel_healer.stop_sentinel_healer()
    assert sentinel_healer._stop_event.is_set()


# ===========================================================================
# 7. Healer Loop Tests
# ===========================================================================

@patch('redis.StrictRedis')
@patch('sentinel_healer.heal_application')
def test_healer_loop_iteration(mock_heal, mock_redis_class):
    """
    Asserts that _healer_loop returns immediately when Sentinel Healer is disabled.
    """
    sentinel_healer._stop_event.clear()
    sentinel_healer._healer_loop()
    mock_heal.assert_not_called()

