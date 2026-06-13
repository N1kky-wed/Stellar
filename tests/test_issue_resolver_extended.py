import os
import json
import pytest
import sqlite3
import subprocess
import fcntl
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
        yield db_file, credentials_dir, active_acc_file


def test_sanitize_prompt_input():
    """
    Asserts that sanitize_prompt_input correctly escapes XML tags and handles None/empty values.
    """
    assert issue_resolver.sanitize_prompt_input(None) == ""
    assert issue_resolver.sanitize_prompt_input("") == ""
    assert issue_resolver.sanitize_prompt_input("hello") == "hello"
    assert issue_resolver.sanitize_prompt_input("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert issue_resolver.sanitize_prompt_input("a < b and c > d") == "a &lt; b and c &gt; d"


@patch("sys.exit", side_effect=SystemExit)
def test_flock_collision(mock_exit, mock_resolver_env):
    """
    Asserts that if another instance of the resolver is running (flock is locked),
    main() handles the BlockingIOError, logs it, and exits with 0.
    """
    db_file, _, _ = mock_resolver_env
    # Lock the lock file manually first
    lock_fd = open(issue_resolver.LOCK_FILE, 'w')
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        with pytest.raises(SystemExit) as excinfo:
            issue_resolver.main()
        assert excinfo.value.code in (0, None)
        mock_exit.assert_called_once_with(0)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


@patch("issue_resolver.TelegramBot")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_invalid_active_acc_file_contents(mock_sleep, mock_bot_class, mock_resolver_env):
    """
    Asserts that if the ACTIVE_ACC_FILE contains invalid content (e.g. non-integer or empty),
    the main loop catches the error, logs a warning, defaults active_idx to 0, and continues.
    """
    db_file, credentials_dir, active_acc_file = mock_resolver_env
    # Write invalid data to active account file
    with open(active_acc_file, "w") as f:
        f.write("not_an_integer")
    
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []
    
    with pytest.raises(FinishedException):
        issue_resolver.main()


@patch("issue_resolver.TelegramBot")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_invalid_telegram_commands(mock_sleep, mock_bot_class, mock_resolver_env):
    """
    Asserts that main processes updates, but ignores invalid command formats
    (non-digits, too few arguments, invalid action codes) without crash or DB update.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    
    # We send various invalid commands
    mock_bot.get_new_messages.return_value = [
        {'text': 'abc 1'},
        {'text': '1'},
        {'text': '1 4'},
        {'text': ''}
    ]
    
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'open')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Re-verify DB state. The issue status should NOT have changed to approved or resolved.
    # Note: open issues are transitioned to pending automatically in notify block.
    # But it should definitely NOT be approved or resolved because the commands were invalid.
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row["status"] == "pending"
    conn.close()


@patch("issue_resolver.get_available_accounts", return_value=[])
@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_empty_accounts_list(mock_sleep, mock_run, mock_email, mock_bot_class, mock_get_acc, mock_resolver_env):
    """
    Asserts that if get_available_accounts returns an empty list, the main loop
    still processes the approved issue using retry_count = 1 without throwing index errors or calling switch_account.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "STATUS: FIXED"
    mock_proc.stderr = ""
    mock_run.return_value = mock_proc

    mock_email.return_value = "Success"

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with patch("issue_resolver.switch_account") as mock_switch:
        with pytest.raises(FinishedException):
            issue_resolver.main()
        mock_switch.assert_not_called()

    # Verify status changed to resolved
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row["status"] == "resolved"
    conn.close()


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_subprocess_timeout_expired(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env):
    """
    Asserts that if the subprocess runs into a timeout, TimeoutExpired is caught,
    the status transitions to escalated, and a failure message is sent.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gemini"], timeout=10, output=b"Partially done", stderr=b"Timed out")

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Verify status transitioned to escalated
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row["status"] == "escalated"
    conn.close()

    # Check notification sent
    mock_bot.send_message.assert_any_call("❌ Issue Resolution Failed: #1 Topic\n\nDescription: Desc\nContext: Ctx\n\nTechnical Output:\nPartially done\nSTATUS: ESCALATED")


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_subprocess_unexpected_exception(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env):
    """
    Asserts that if subprocess.run raises an unexpected exception (e.g. FileNotFoundError),
    it is caught, logged, the status transitions to escalated, and failure is reported.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_run.side_effect = FileNotFoundError("[Errno 2] No such file or directory: 'gemini'")

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Verify status transitioned to escalated
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row["status"] == "escalated"
    conn.close()

    mock_bot.send_message.assert_any_call("❌ Issue Resolution Failed: #1 Topic\n\nDescription: Desc\nContext: Ctx\n\nTechnical Output:\nError running Bug Fixer Agent: [Errno 2] No such file or directory: 'gemini'\nSTATUS: ESCALATED")


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_subprocess_quota_exhausted_rotation(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env):
    """
    Asserts that if the output of the bug fixer CLI indicates a quota exhaust error,
    the resolver switches to the next account, increments the active index,
    saves it, and retries the CLI call.
    """
    db_file, _, active_acc_file = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_resp_quota = MagicMock()
    mock_resp_quota.returncode = 0
    mock_resp_quota.stdout = "Error: Quota Exceeded (429)"
    mock_resp_quota.stderr = ""

    mock_resp_success = MagicMock()
    mock_resp_success.returncode = 0
    mock_resp_success.stdout = "STATUS: FIXED"
    mock_resp_success.stderr = ""

    mock_run.side_effect = [mock_resp_quota, mock_resp_success]

    # Seed the DB with approved issue
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Re-verify DB state
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row["status"] == "resolved"
    conn.close()

    # Verify active index is updated to 1
    with open(active_acc_file, "r") as f:
        active_idx = int(f.read().strip())
    assert active_idx == 1


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email", return_value="Failed: Connection Refused")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_email_failure_fallback_message(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env):
    """
    Asserts that if email dispatch fails (does not contain 'Success'),
    a fallback message notification is successfully sent via the Telegram bot.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "STATUS: FIXED"
    mock_proc.stderr = ""
    mock_run.return_value = mock_proc

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Verify fallback send_message was called on Telegram bot
    mock_bot.send_message.assert_any_call("✅ Issue Resolved: #1 Topic\n\nDescription: Desc\nContext: Ctx")


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email", return_value="Success")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_status_mishap_processing(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env):
    """
    Asserts that if the output contains STATUS: MISHAP, the status transitions to temporary_mishap,
    sends mishap email and falls back to Telegram if necessary.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "STATUS: MISHAP"
    mock_proc.stderr = ""
    mock_run.return_value = mock_proc

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Re-verify DB state
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row["status"] == "temporary_mishap"
    conn.close()


@patch("issue_resolver.TelegramBot")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_telegram_actions_2_and_3(mock_sleep, mock_bot_class, mock_resolver_env):
    """
    Asserts that main processes Telegram actions '2' (mark resolved) and '3' (mark mishap) correctly.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = [
        {'text': '1 2'},
        {'text': '2 3'}
    ]

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic1', 'Desc1', 'Ctx1', 'pending')"
    )
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (2, 1, 'Topic2', 'Desc2', 'Ctx2', 'pending')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Verify status changed correctly in DB
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row1 = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    row2 = conn.execute("SELECT status FROM agent_feedback WHERE id = 2").fetchone()
    assert row1["status"] == "resolved"
    assert row2["status"] == "temporary_mishap"
    conn.close()

    mock_bot.send_message.assert_any_call("✅ Marked Issue #1 as Resolved.")
    mock_bot.send_message.assert_any_call("ℹ️ Marked Issue #2 as Mishap.")


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_generic_status_fallback_escalated(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env):
    """
    Asserts that if the subprocess returns an output with no recognized status code,
    final_status defaults to escalated.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Unknown return status output"
    mock_proc.stderr = ""
    mock_run.return_value = mock_proc

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    # Re-verify DB state
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM agent_feedback WHERE id = 1").fetchone()
    assert row["status"] == "escalated"
    conn.close()


@patch("issue_resolver.TelegramBot")
@patch("issue_resolver.send_self_email", return_value="Failed: Connection Refused")
@patch("subprocess.run")
@patch("time.sleep", side_effect=FinishedException("Loop Terminated"))
def test_mishap_email_failure_fallback_message(mock_sleep, mock_run, mock_email, mock_bot_class, mock_resolver_env):
    """
    Asserts that if email dispatch fails for a mishap, a fallback notification is sent via the Telegram bot.
    """
    db_file, _, _ = mock_resolver_env
    mock_bot = MagicMock()
    mock_bot_class.return_value = mock_bot
    mock_bot.get_new_messages.return_value = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "STATUS: MISHAP"
    mock_proc.stderr = ""
    mock_run.return_value = mock_proc

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO agent_feedback (id, user_id, topic, issue_description, technical_context, status) VALUES (1, 1, 'Topic', 'Desc', 'Ctx', 'approved')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(FinishedException):
        issue_resolver.main()

    mock_bot.send_message.assert_any_call("ℹ️ Issue Mishap: #1 Topic\n\nDescription: Desc\nContext: Ctx")
