import os
import json
import sqlite3
import tempfile
import sys
import pytest
from unittest.mock import patch, MagicMock
from app import get_db, agent_group_chat_stream

# Add a brief comment above each test explaining what behavior it asserts and why.

# Save original sqlite3.connect to avoid recursion in mocks
_real_sqlite3_connect = sqlite3.connect

class StopLoopException(Exception):
    """Custom exception to stop the infinite stream loop in tests."""
    pass

def test_agent_group_chat_page_anonymous(client):
    """
    Asserts that anonymous visitors are denied access to the agent group chat page with 401.
    """
    response = client.get('/agent-group-chat')
    assert response.status_code == 401

def test_agent_group_chat_page_non_admin(client):
    """
    Asserts that approved non-admin users get a 403 Forbidden for the agent group chat page.
    """
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (2, "user@gmail.com", "Normal User", "user", 1)')
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 2
        sess['username'] = "user@gmail.com"
        sess['display_name'] = "Normal User"
        sess['role'] = "user"
        sess['is_approved'] = True

    response = client.get('/agent-group-chat')
    assert response.status_code == 403

def test_agent_group_chat_page_admin(auth_client):
    """
    Asserts that approved admin users can successfully load the agent group chat page (200 OK)
    and that the response content type is HTML.
    """
    response = auth_client.get('/agent-group-chat')
    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert b"Agent Group Chat" in response.data

def test_get_agent_group_chat_history_non_admin(client):
    """
    Asserts that non-admin users cannot retrieve agent group chat history (403 Forbidden).
    """
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (2, "user@gmail.com", "Normal User", "user", 1)')
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 2
        sess['username'] = "user@gmail.com"
        sess['display_name'] = "Normal User"
        sess['role'] = "user"
        sess['is_approved'] = True

    response = client.get('/api/admin/agent_group_chat/history')
    assert response.status_code == 403

def test_get_agent_group_chat_history_no_db(auth_client):
    """
    Asserts that if the orchestrator database file does not exist,
    the history endpoint returns an empty JSON list.
    """
    with patch('os.path.exists', return_value=False):
        response = auth_client.get('/api/admin/agent_group_chat/history')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data == []

def test_get_agent_group_chat_history_with_runs(auth_client):
    """
    Asserts that the history endpoint correctly parses agent runs from the sqlite database
    and formats system and agent messages as expected.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        conn = _real_sqlite3_connect(temp_db_path)
        conn.execute("""
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                pr_number INTEGER,
                pr_url TEXT,
                branch_name TEXT,
                error_message TEXT,
                summary_message TEXT
            )
        """)
        # Insert completed run
        conn.execute("""
            INSERT INTO agent_runs VALUES (
                1, 'researcher', '2026-06-10T16:00:00.000Z', '2026-06-10T16:05:00.000Z', 
                'COMPLETED', 42, 'https://github.com/Stellar/pull/42', 'test-branch-1', 
                NULL, 'Summary explanation.'
            )
        """)
        # Insert failed run
        conn.execute("""
            INSERT INTO agent_runs VALUES (
                2, 'coder', '2026-06-10T16:10:00.000Z', '2026-06-10T16:12:00.000Z', 
                'FAILED', NULL, NULL, 'test-branch-2', 
                'SyntaxError', NULL
            )
        """)
        # Insert timeout run
        conn.execute("""
            INSERT INTO agent_runs VALUES (
                3, 'reviewer', '2026-06-10T16:20:00.000Z', '2026-06-10T16:25:00.000Z', 
                'TIMEOUT', NULL, NULL, 'test-branch-3', 
                NULL, NULL
            )
        """)
        conn.commit()
        conn.close()

        def mock_exists(path):
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                return True
            return os.path.exists(path)

        def mock_connect(path):
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.get('/api/admin/agent_group_chat/history')
                assert response.status_code == 200
                messages = json.loads(response.data)
                
                # Researcher starting system message
                assert messages[0]['sender'] == 'Orchestrator'
                assert 'Starting agent **Researcher**' in messages[0]['content']
                assert messages[0]['type'] == 'system'
                
                # Researcher agent summary
                assert messages[1]['sender'] == 'Researcher (Agent)'
                assert messages[1]['content'] == 'Summary explanation.'
                assert messages[1]['type'] == 'agent'
                
                # Researcher success message
                assert messages[2]['sender'] == 'Orchestrator'
                assert 'completed successfully!' in messages[2]['content']
                assert '(PR #42)' in messages[2]['content']
                
                # Coder failure message
                assert messages[3]['sender'] == 'Orchestrator'
                assert 'Starting agent **Coder**' in messages[3]['content']
                assert messages[4]['sender'] == 'Orchestrator'
                assert 'run failed' in messages[4]['content']
                assert 'SyntaxError' in messages[4]['content']
                
                # Reviewer timeout message
                assert messages[5]['sender'] == 'Orchestrator'
                assert 'Starting agent **Reviewer**' in messages[5]['content']
                assert messages[6]['sender'] == 'Orchestrator'
                assert 'timed out' in messages[6]['content']

    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)

def test_agent_group_chat_stream_non_admin(client):
    """
    Asserts that non-admin users cannot access the stream (403 Forbidden).
    """
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (2, "user@gmail.com", "Normal User", "user", 1)')
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 2
        sess['username'] = "user@gmail.com"
        sess['display_name'] = "Normal User"
        sess['role'] = "user"
        sess['is_approved'] = True

    response = client.get('/api/admin/agent_group_chat/stream')
    assert response.status_code == 403

def test_agent_group_chat_stream_no_db(auth_client):
    """
    Asserts that if the orchestrator database file does not exist,
    the stream yields an empty dictionary or a default event immediately.
    """
    with patch('os.path.exists', return_value=False):
        response = auth_client.get('/api/admin/agent_group_chat/stream')
        assert response.status_code == 200
        assert response.is_streamed
        content = b"".join(response.response)
        assert b"data: {}" in content

def test_agent_group_chat_stream_with_new_runs(auth_client):
    """
    Asserts that the stream successfully yields database updates on new agent runs.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        conn = _real_sqlite3_connect(temp_db_path)
        conn.execute("""
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                pr_number INTEGER,
                pr_url TEXT,
                branch_name TEXT,
                error_message TEXT,
                summary_message TEXT
            )
        """)
        conn.execute("""
            INSERT INTO agent_runs VALUES (
                1, 'researcher', '2026-06-10T16:00:00.000Z', '2026-06-10T16:05:00.000Z', 
                'COMPLETED', 42, 'https://github.com/Stellar/pull/42', 'test-branch-1', 
                NULL, 'Summary explanation.'
            )
        """)
        conn.commit()
        conn.close()

        def mock_exists(path):
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                return True
            return os.path.exists(path)

        def mock_connect(path):
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        def mock_sleep(seconds):
            frame = sys._getframe(0)
            while frame:
                if frame.f_code.co_name == 'log_stream':
                    raise StopLoopException("Stop loop")
                frame = frame.f_back

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                with patch('time.sleep', side_effect=mock_sleep):
                    with auth_client.application.test_request_context():
                        from flask import session
                        session['user_id'] = 1
                        session['role'] = 'admin'
                        session['is_approved'] = True
                        
                        response = agent_group_chat_stream()
                        assert response.status_code == 200
                        assert response.is_streamed
                        
                        content = []
                        try:
                            for chunk in response.response:
                                content.append(chunk)
                        except StopLoopException:
                            pass
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)

def test_agent_group_chat_stream_transition(auth_client):
    """
    Asserts that the stream logs transition from RUNNING to COMPLETED/FAILED/TIMEOUT.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        conn = _real_sqlite3_connect(temp_db_path)
        conn.execute("""
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                pr_number INTEGER,
                pr_url TEXT,
                branch_name TEXT,
                error_message TEXT,
                summary_message TEXT
            )
        """)
        conn.execute("""
            INSERT INTO agent_runs VALUES (
                1, 'researcher', '2026-06-10T16:00:00.000Z', NULL, 
                'RUNNING', NULL, NULL, 'test-branch-1', 
                NULL, NULL
            )
        """)
        conn.commit()
        conn.close()

        def mock_exists(path):
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                return True
            return os.path.exists(path)

        query_count = 0
        def mock_connect(path):
            nonlocal query_count
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                conn = _real_sqlite3_connect(temp_db_path)
                if query_count > 0:
                    conn.execute("""
                        UPDATE agent_runs 
                        SET status = 'COMPLETED', finished_at = '2026-06-10T16:05:00.000Z', 
                            pr_number = 43, pr_url = 'https://github.com/Stellar/pull/43',
                            summary_message = 'All tests passed!'
                        WHERE id = 1
                    """)
                    conn.commit()
                query_count += 1
                return conn
            return _real_sqlite3_connect(path)

        def mock_sleep(seconds):
            frame = sys._getframe(0)
            while frame:
                if frame.f_code.co_name == 'log_stream' and query_count >= 2:
                    raise StopLoopException("Stop loop")
                frame = frame.f_back

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                with patch('time.sleep', side_effect=mock_sleep):
                    with auth_client.application.test_request_context():
                        from flask import session
                        session['user_id'] = 1
                        session['role'] = 'admin'
                        session['is_approved'] = True
                        
                        response = agent_group_chat_stream()
                        assert response.status_code == 200
                        assert response.is_streamed
                        
                        content_chunks = []
                        try:
                            for chunk in response.response:
                                content_chunks.append(chunk)
                        except StopLoopException:
                            pass
                        
                        content = "".join(content_chunks)
                        assert "All tests passed!" in content
                        assert "completed successfully!" in content
                        assert "PR #43" in content
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


def test_orchestrator_status_anonymous(client):
    """
    Asserts that anonymous visitors are denied access to the orchestrator status route with 401.
    """
    response = client.get('/api/admin/orchestrator/status')
    assert response.status_code == 401


def test_orchestrator_status_non_admin(client):
    """
    Asserts that approved non-admin users get a 403 Forbidden for the orchestrator status route.
    """
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (2, "user@gmail.com", "Normal User", "user", 1)')
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 2
        sess['username'] = "user@gmail.com"
        sess['display_name'] = "Normal User"
        sess['role'] = "user"
        sess['is_approved'] = True

    response = client.get('/api/admin/orchestrator/status')
    assert response.status_code == 403


def test_orchestrator_status_no_db(auth_client):
    """
    Asserts that if the database doesn't exist, the route returns the default status.
    """
    with patch('sqlite3.connect', side_effect=sqlite3.OperationalError("Mock DB doesn't exist")):
        response = auth_client.get('/api/admin/orchestrator/status')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['cooldown'] == {'active': False}
        assert data['running_agent'] is None


def test_orchestrator_status_with_cooldown_and_running(auth_client):
    """
    Asserts that the orchestrator status route correctly reads the active cooldown and the running agent details from the DB.
    """
    import datetime
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        conn = _real_sqlite3_connect(temp_db_path)
        # Create tables
        conn.execute("CREATE TABLE orchestrator_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                pr_number INTEGER,
                pr_url TEXT,
                branch_name TEXT,
                error_message TEXT,
                summary_message TEXT
            )
        """)
        
        # Set cooldown in future in IST timezone
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        future_dt = datetime.datetime.now(IST) + datetime.timedelta(hours=2)
        conn.execute("INSERT INTO orchestrator_state VALUES ('quota_cooldown_until', ?)", (future_dt.isoformat(),))
        
        # Set running agent
        conn.execute("""
            INSERT INTO agent_runs VALUES (
                1, 'researcher', '2026-06-10T16:00:00.000Z', NULL, 
                'RUNNING', NULL, NULL, 'test-branch-1', 
                NULL, NULL
            )
        """)
        conn.commit()
        conn.close()

        def mock_exists(path):
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                return True
            return os.path.exists(path)

        def mock_connect(path):
            if path == '/home/stellaradmin/my_app/orchestrator/orchestrator.db':
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.get('/api/admin/orchestrator/status')
                assert response.status_code == 200
                data = json.loads(response.data)
                
                # Verify cooldown is active
                assert data['cooldown']['active'] is True
                assert 'remaining_seconds' in data['cooldown']
                assert data['cooldown']['remaining_seconds'] > 0
                
                # Verify running agent is retrieved
                assert data['running_agent'] is not None
                assert data['running_agent']['agent_id'] == 'researcher'
                assert data['running_agent']['started_at'] == '2026-06-10T16:00:00.000Z'
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)

