import os
import json
import pytest
import sqlite3
import subprocess
from unittest.mock import patch, MagicMock
import issue_resolver

# Custom exception to break the infinite main loop in tests
class FinishedException(Exception):
    pass

@pytest.fixture
def mock_resolver_env(tmp_path):
    """
    Sets up a temporary SQLite database, lock file, credentials directory,
    and resolver home directory to isolate tests from production resources.
    """
    db_file = tmp_path / "test_stellar_local.db"
    lock_file = tmp_path / "test_gemini_resolver.lock"
    active_acc_file = tmp_path / "test_active_resolver_account"
    credentials_dir = tmp_path / "credentials"
    resolver_home = tmp_path / "resolver_home"
    
    # Create credentials directories for testing account discovery
    credentials_dir.mkdir()
    (credentials_dir / "account_1").mkdir()
    (credentials_dir / "account_2").mkdir()
    (credentials_dir / "not_an_account").mkdir()
    
    # Write a dummy json key in account_1 to test switch_account
    with open(credentials_dir / "account_1" / "key.json", "w") as f:
        f.write('{"key": "val"}')
    
    # Initialize schema in the test database
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE agent_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            topic TEXT,
            issue_description TEXT,
            technical_context TEXT,
            status TEXT
        )
    """)
    conn.commit()
    conn.close()

    # Patch paths/constants in issue_resolver
    with patch("issue_resolver.DB_PATH", str(db_file)), \
         patch("issue_resolver.LOCK_FILE", str(lock_file)), \
         patch("issue_resolver.ACTIVE_ACC_FILE", str(active_acc_file)), \
         patch("issue_resolver.CREDENTIALS_BASE_DIR", str(credentials_dir)), \
         patch("issue_resolver.RESOLVER_HOME", str(resolver_home)):
        yield db_file, credentials_dir


def test_get_db(mock_resolver_env):
    """
    Asserts that get_db successfully connects to the SQLite database
    and returns a sqlite3.Connection with Row row_factory.
    """
    conn = issue_resolver.get_db()
    assert isinstance(conn, sqlite3.Connection)
    assert conn.row_factory == sqlite3.Row
    conn.close()


def test_get_available_accounts(mock_resolver_env):
    """
    Asserts that get_available_accounts lists and returns only sorted directories
    starting with 'account_'.
    """
    accounts = issue_resolver.get_available_accounts()
    assert accounts == ["account_1", "account_2"]


def test_switch_account(mock_resolver_env):
    """
    Asserts that switch_account successfully copies JSON credentials files
    to the active resolver home directory.
    """
    issue_resolver.switch_account("account_1")
    target_path = os.path.join(issue_resolver.RESOLVER_HOME, ".gemini", "key.json")
    assert os.path.exists(target_path)
    with open(target_path, "r") as f:
        data = json.load(f)
    assert data["key"] == "val"


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_main_loop_processing(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env, client):
    """
    Asserts that main:
    1. Processes incoming Telegram commands to update issue status.
    2. Notifies on 'open' issues and transitions status to 'pending'.
    3. Triggers the Gemini CLI on 'approved' issues and transitions status to 'resolved' on success.
    """
    # Set up Telegram bot instance mock
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    
    # Telegram command to transition issue 1 to approved
    mock_bot.get_new_messages.return_value = [{'text': '1 1'}]
    
    # Mock subprocess.run for Gemini CLI success
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Successful execution\nSTATUS: FIXED"
    mock_proc.stderr = ""
    mock_run.return_value = mock_proc

    mock_email.return_value = "Success: Email Sent"

    # Seed initial test data in test db
    db_file, _ = mock_resolver_env
    conn = sqlite3.connect(str(db_file))
    # Issue 1: open issue, should transition to pending then approved via Telegram command
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Bug in tools', 'Description', 'Context', 'open')"
    )
    # Issue 2: approved issue, should process and transition to resolved via Gemini CLI
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (2, 1, 'Crashes', 'Description', 'Context', 'approved')"
    )
    conn.commit()
    conn.close()

    # Run the main loop; it should execute one pass and then raise FinishedException from mock_sleep
    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Re-verify DB states
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    
    # Issue 1 should have been processed via Telegram command and set to 'approved' (then subsequently resolved)
    row1 = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row1["status"] == "resolved"

    # Issue 2 was approved initially, but since loop only processes one issue per pass, it remains 'approved'
    row2 = conn.execute("SELECT status FROM agent_feedback WHERE id = 2").fetchone()
    assert row2["status"] == "approved"
    conn.close()

    # Verify notifications were sent
    mock_bot.send_message.assert_any_call("🔍 Investigating Issue #1...")
    mock_email.assert_called_once()
    assert "Issue Resolved: #1" in mock_email.call_args[0][0]
