import os
import json
import pytest
import sqlite3
import shutil
import threading
from unittest.mock import patch, MagicMock
import docker

import sentinel_healer

# A custom sqlite3 connection that prevents auto-closing by heal_application
class NonClosingConnection(sqlite3.Connection):
    def close(self):
        pass
    def real_close(self):
        super().close()

# ---------------------------------------------------------------------------
# get_working_api_key Tests
# ---------------------------------------------------------------------------

def test_get_working_api_key_all_blocked():
    """
    Asserts that get_working_api_key returns PRIMARY_API_KEY when all keys are blocked.
    """
    with patch('app.PRIMARY_API_KEY', 'prim-123'), \
         patch('app.BACKUP_API_KEYS', ['bk-1', 'bk-2']), \
         patch('app.KEY_MANAGER') as mock_mgr:
        
        # Mock is_key_blocked to return True for everything
        mock_mgr.is_key_blocked.return_value = (True, "blocked")
        
        key = sentinel_healer.get_working_api_key("gemini-3.5-flash")
        assert key == 'prim-123'


def test_get_working_api_key_import_failure(monkeypatch):
    """
    Asserts that get_working_api_key falls back to reading from environment variables
    if import from app module fails.
    """
    # Force import error by patching sys.modules or raising exception inside imports
    with patch.dict('sys.modules', {'app': None}):
        monkeypatch.setenv("PRIMARY_API_KEY", "env-primary-key")
        key = sentinel_healer.get_working_api_key("gemini-3.5-flash")
        assert key == 'env-primary-key'


# ---------------------------------------------------------------------------
# detect_startup_command Tests
# ---------------------------------------------------------------------------

def test_detect_startup_command_exception():
    """
    Asserts that detect_startup_command falls back to 'python app.py' when directory listing
    or file reading raises an exception (e.g. non-existent path).
    """
    # Trigger Exception path by passing a non-existent directory
    cmd = sentinel_healer.detect_startup_command("/non/existent/directory/path/123")
    assert cmd == "python app.py"


# ---------------------------------------------------------------------------
# heal_application Error & Failure Path Tests
# ---------------------------------------------------------------------------

def test_heal_application_no_error_row():
    """
    Asserts that heal_application returns early (does nothing) if the database
    does not contain a row for the specified error_id.
    """
    mock_redis = MagicMock()
    
    # We patch get_db_conn to return an in-memory SQLite connection without seed data
    db_conn = sqlite3.connect(":memory:", factory=NonClosingConnection)
    db_conn.row_factory = sqlite3.Row
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sentinel_app_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process_id TEXT,
            error_type TEXT,
            error_message TEXT,
            stack_trace TEXT,
            affected_file TEXT,
            affected_line INTEGER,
            status TEXT
        )
    """)
    db_conn.commit()

    with patch('sentinel_healer.get_db_conn', return_value=db_conn):
        sentinel_healer.heal_application("process-123", 999, mock_redis)
    
    db_conn.real_close()


@patch('sentinel_healer.docker.from_env')
def test_heal_application_container_not_found(mock_docker_env):
    """
    Asserts that heal_application sets the error status back to 'open' when the container
    associated with the process cannot be found.
    """
    mock_redis = MagicMock()
    
    # Seed DB with the correct table schema
    db_conn = sqlite3.connect(":memory:", factory=NonClosingConnection)
    db_conn.row_factory = sqlite3.Row
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sentinel_app_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process_id TEXT,
            error_type TEXT,
            error_message TEXT,
            stack_trace TEXT,
            affected_file TEXT,
            affected_line INTEGER,
            status TEXT
        )
    """)
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sentinel_app_patches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_id INTEGER,
            patch_diff TEXT,
            status TEXT
        )
    """)
    db_conn.execute(
        "INSERT INTO sentinel_app_errors (process_id, error_type, error_message, stack_trace, affected_file, affected_line, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ('proc-123', 'js_error', 'ReferenceError: x is not defined', 'traceback', 'main.js', 10, 'open')
    )
    db_conn.commit()
    
    error_id = 1

    # Mock docker client containers.get to raise NotFound
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    class LocalNotFound(Exception):
        pass

    with patch.object(docker.errors, 'NotFound', LocalNotFound):
        mock_client.containers.get.side_effect = LocalNotFound("No container")

        with patch('sentinel_healer.get_db_conn', return_value=db_conn):
            sentinel_healer.heal_application("proc-123", error_id, mock_redis)

    # Status must be set to open
    row = db_conn.execute("SELECT status FROM sentinel_app_errors WHERE id = 1").fetchone()
    assert row['status'] == 'open'
    db_conn.real_close()


@patch('sentinel_healer.docker.from_env')
def test_heal_application_container_not_running(mock_docker_env):
    """
    Asserts that heal_application raises a ValueError and rolls back the error status to 'open'
    if the container exists but is not currently running.
    """
    mock_redis = MagicMock()
    
    # Seed DB
    db_conn = sqlite3.connect(":memory:", factory=NonClosingConnection)
    db_conn.row_factory = sqlite3.Row
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sentinel_app_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process_id TEXT,
            error_type TEXT,
            error_message TEXT,
            stack_trace TEXT,
            affected_file TEXT,
            affected_line INTEGER,
            status TEXT
        )
    """)
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sentinel_app_patches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_id INTEGER,
            patch_diff TEXT,
            status TEXT
        )
    """)
    db_conn.execute(
        "INSERT INTO sentinel_app_errors (process_id, error_type, error_message, stack_trace, affected_file, affected_line, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ('proc-123', 'js_error', 'ReferenceError: x is not defined', 'traceback', 'main.js', 10, 'open')
    )
    db_conn.commit()
    
    error_id = 1

    # Mock container status as exited
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    mock_container = MagicMock()
    mock_container.status = 'exited'
    mock_container.name = 'stellar-repo-proc-123'
    mock_client.containers.get.return_value = mock_container

    with patch('sentinel_healer.get_db_conn', return_value=db_conn):
        sentinel_healer.heal_application("proc-123", error_id, mock_redis)

    # Status must roll back to open
    row = db_conn.execute("SELECT status FROM sentinel_app_errors WHERE id = 1").fetchone()
    assert row['status'] == 'open'
    db_conn.real_close()


# ---------------------------------------------------------------------------
# Healer Loop & Threading Tests
# ---------------------------------------------------------------------------

@patch('redis.StrictRedis')
def test_healer_loop_exception_handling(mock_redis_class):
    """
    Asserts that _healer_loop continues executing and does not crash when Redis operations,
    payload decoding, or heal_application itself raise exceptions.
    """
    mock_redis = MagicMock()
    mock_redis_class.return_value = mock_redis
    
    # brpop yields one invalid payload first (causes JSON decode exception),
    # then one valid payload but heal_application raises exception,
    # then we terminate by signaling the stop event.
    
    payloads = [
        ("sentinel:queue", "invalid-json-string"),
        ("sentinel:queue", '{"process_id": "p1", "error_id": 1}')
    ]
    
    def mock_brpop(*args, **kwargs):
        if payloads:
            return payloads.pop(0)
        # Stop loop on third check
        sentinel_healer._stop_event.set()
        return None
        
    mock_redis.brpop.side_effect = mock_brpop

    # Mock heal_application to raise exception
    with patch('sentinel_healer.heal_application', side_effect=Exception("Healing failed")):
        sentinel_healer._stop_event.clear()
        sentinel_healer._healer_loop()
        
        # Verify the loop completed iterations and stopped cleanly
        assert sentinel_healer._stop_event.is_set()


def test_start_sentinel_healer():
    """
    Asserts that start_sentinel_healer successfully creates and starts the worker thread.
    """
    mock_thread = MagicMock()
    with patch('threading.Thread', return_value=mock_thread) as mock_thread_class:
        sentinel_healer._healer_thread = None
        sentinel_healer.start_sentinel_healer()
        mock_thread_class.assert_called_once()
        mock_thread.start.assert_called_once()
