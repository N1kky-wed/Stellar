import os
import json
import pytest
import sqlite3
import time
import docker
import paramiko
from unittest.mock import patch, MagicMock
from rich.console import Console

# Import the module under test
import ssh_gateway

@pytest.fixture
def mock_db(tmp_path):
    """
    Sets up a temporary SQLite database and patches the DATABASE_PATH in ssh_gateway.
    """
    db_file = tmp_path / "test_stellar_local.db"
    
    # Initialize the schema
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE repo_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            project_name TEXT,
            process_id TEXT,
            container_id TEXT,
            status TEXT,
            subdomain TEXT,
            created_at TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            app_type TEXT
        )
    """)
    conn.commit()
    conn.close()

    with patch("ssh_gateway.DATABASE_PATH", str(db_file)):
        yield db_file


def test_get_console():
    """
    Asserts that get_console returns a rich Console instance with a valid width.
    """
    console = ssh_gateway.get_console(80)
    assert isinstance(console, Console)
    assert isinstance(console.width, int)
    assert console.width > 0


def test_send_raw():
    """
    Asserts that send_raw translates newlines and sends bytes down the SSH channel.
    """
    mock_channel = MagicMock()
    ssh_gateway.send_raw(mock_channel, "Hello\nWorld\r\n")
    mock_channel.sendall.assert_called_once_with(b"Hello\r\nWorld\r\n")


def test_get_user_repos(mock_db):
    """
    Asserts that get_user_repos correctly queries the database and formats repo metadata.
    """
    conn = sqlite3.connect(str(mock_db))
    conn.execute(
        "INSERT INTO repo_history (id, user_id, project_name, process_id, container_id, status, subdomain, created_at, app_type) "
        "VALUES (10, 5, 'MyProject', 'proc123', 'cont123', 'running', 'sub.domain.com', '2026-06-12 12:00:00', 'web')"
    )
    conn.commit()
    conn.close()

    repos = ssh_gateway.get_user_repos(5)
    assert len(repos) == 1
    repo = repos[0]
    assert repo['id'] == 10
    assert repo['name'] == 'MyProject'
    assert repo['process_id'] == 'proc123'
    assert repo['container_id'] == 'cont123'
    assert repo['status'] == 'running'
    assert repo['subdomain'] == 'sub.domain.com'
    assert repo['created'] == '2026-06-12'
    assert repo['app_type'] == 'web'


def test_verify_auth_code_success():
    """
    Asserts that verify_auth_code successfully reads, parses, invalidates the code from Redis,
    and decrements the user active code counter.
    """
    mock_redis = MagicMock()
    mock_redis.get.side_effect = lambda key: (
        '{"user_id": 42, "username": "test@gmail.com"}' if "ssh_auth_code:ABCDEF" in key
        else "1" if "ssh_auth_code:user:42" in key
        else None
    )

    with patch("ssh_gateway.redis_client", mock_redis), \
         patch("ssh_gateway.audit") as mock_audit:
        result = ssh_gateway.verify_auth_code("ABC-DEF")
        
        assert result == {"user_id": 42, "username": "test@gmail.com"}
        mock_redis.delete.assert_any_call("ssh_auth_code:ABCDEF")
        mock_redis.decr.assert_called_once_with("ssh_auth_code:user:42")


def test_verify_auth_code_not_found():
    """
    Asserts that verify_auth_code returns None if code is missing from Redis.
    """
    mock_redis = MagicMock()
    mock_redis.get.return_value = None

    with patch("ssh_gateway.redis_client", mock_redis):
        result = ssh_gateway.verify_auth_code("NONEXI")
        assert result is None


def test_verify_auth_code_invalid_length():
    """
    Asserts that verify_auth_code returns None early if the code is invalid.
    """
    result = ssh_gateway.verify_auth_code("ABC")
    assert result is None


def test_load_and_save_theme(tmp_path):
    """
    Asserts that load_theme and save_theme successfully save and retrieve user preferences from disk.
    """
    user_id = "test_theme_user"
    dir_path = os.path.dirname(os.path.abspath(ssh_gateway.__file__))
    theme_path = os.path.join(dir_path, f'.ssh_theme_{user_id}.json')
    
    # Clean up before testing
    if os.path.exists(theme_path):
        os.remove(theme_path)

    try:
        # Default theme when file doesn't exist
        theme = ssh_gateway.load_theme(user_id)
        assert theme == {"theme_idx": 0, "border_idx": 0}

        # Save theme
        ssh_gateway.save_theme(user_id, 3, 2)
        assert os.path.exists(theme_path)

        # Load updated theme
        theme = ssh_gateway.load_theme(user_id)
        assert theme == {"theme_idx": 3, "border_idx": 2}
    finally:
        # Clean up
        if os.path.exists(theme_path):
            os.remove(theme_path)


@patch("ssh_gateway.get_docker_client")
@patch("ssh_gateway.get_container")
@patch("ssh_gateway.invalidate_container_cache")
def test_container_lifecycle_actions(mock_invalidate, mock_get_container, mock_get_docker_client):
    """
    Asserts that restart_container, stop_container, and start_container transition container statuses
    and update cached state correctly.
    """
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.name = "stellar-web-proc123"
    
    mock_get_docker_client.return_value = mock_client
    mock_get_container.return_value = mock_container

    # 1. Restart
    res = ssh_gateway.restart_container("proc123", "web", 5)
    assert "restarted successfully" in res
    mock_container.restart.assert_called_once_with(timeout=10)
    assert ssh_gateway._container_statuses_cache["stellar-web-proc123"] == "running"
    
    # 2. Stop
    res = ssh_gateway.stop_container("proc123", "web", 5)
    assert "stopped" in res
    mock_container.stop.assert_called_once_with(timeout=10)
    assert ssh_gateway._container_statuses_cache["stellar-web-proc123"] == "exited"

    # 3. Start
    res = ssh_gateway.start_container("proc123", "web", 5)
    assert "started" in res
    mock_container.start.assert_called_once()
    assert ssh_gateway._container_statuses_cache["stellar-web-proc123"] == "running"


@patch("ssh_gateway.get_docker_client")
@patch("ssh_gateway.get_container")
def test_container_not_found(mock_get_container, mock_get_docker_client):
    """
    Asserts that lifecycle actions handle NotFound exceptions gracefully.
    """
    mock_get_container.side_effect = docker.errors.NotFound("Not found")
    
    res = ssh_gateway.restart_container("proc123", "web", 5)
    assert "Container not found" in res


def test_ssh_server_auth_interface():
    """
    Asserts that StellarSSHServer's authentication overrides act securely and allow passwordless login.
    """
    server = ssh_gateway.StellarSSHServer("127.0.0.1")
    
    # check_auth_none should allow all usernames to connect (real auth in TUI code page)
    assert server.check_auth_none("any_user") == paramiko.AUTH_SUCCESSFUL
    
    # check_auth_password and check_auth_publickey should reject connection attempts
    assert server.check_auth_password("user", "pass") == paramiko.AUTH_FAILED
    assert server.check_auth_publickey("user", None) == paramiko.AUTH_FAILED
    
    # check_channel_request and checking forwarding should return denied/false
    assert server.check_port_forward_request("127.0.0.1", 80) is False
