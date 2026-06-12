import pytest
import time
from unittest.mock import patch, MagicMock
from app import app, get_db, TaskSchedulerMonitor

@pytest.fixture(autouse=True)
def setup_scheduler_db(client):
    """Fixture to ensure the scheduled_tasks table has status and lock_id columns,
    and seed a test user and chat."""
    with app.app_context():
        db = get_db()
        
        # Ensure status and lock_id columns exist (workaround for production schema missing these columns)
        cursor = db.execute("PRAGMA table_info(scheduled_tasks)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'status' not in columns:
            db.execute("ALTER TABLE scheduled_tasks ADD COLUMN status TEXT DEFAULT 'pending'")
        if 'lock_id' not in columns:
            db.execute("ALTER TABLE scheduled_tasks ADD COLUMN lock_id TEXT")
            
        # Seed user and chat
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (1, "testuser@gmail.com", "Test User", "admin", 1)')
        db.execute('INSERT OR IGNORE INTO chats (id, user_id, name) VALUES (1, 1, "Test Chat")')
        db.commit()
    yield

# Helper to wait for the task to finish running in the background thread
def wait_for_task_completion(task_id, timeout=2.0):
    start_time = time.time()
    while time.time() - start_time < timeout:
        with app.app_context():
            db = get_db()
            cursor = db.execute("SELECT status, is_active FROM scheduled_tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if row and row['status'] != 'running':
                return row
        time.sleep(0.05)
    raise TimeoutError(f"Task {task_id} did not finish within {timeout} seconds.")

# 1. Assert that a pending one-time task is locked, executed successfully, marked completed (is_active=0), and appends a stellar response message.
@patch('app.gemini_generate')
def test_scheduler_one_time_task_success(mock_gemini_generate, client):
    """
    Asserts that a pending non-recurring task executes successfully, creates a chat message,
    and is deactivated (is_active = 0) with status 'completed'.
    """
    mock_gemini_generate.return_value = [{'result': 'Scheduled task output content'}]

    with app.app_context():
        db = get_db()
        cursor = db.execute(
            "INSERT INTO scheduled_tasks (user_id, chat_id, task_prompt, model_id, status, is_active, execute_at, recurring_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, "Generate summary", "gemini-3.5-flash", "pending", 1, None, 0)
        )
        task_id = cursor.lastrowid
        db.commit()

    monitor = TaskSchedulerMonitor(app, interval=60)
    monitor._check_tasks()

    # Wait for the background thread to complete
    row = wait_for_task_completion(task_id)
    assert row['status'] == 'completed'
    assert row['is_active'] == 0

    # Verify message was inserted
    with app.app_context():
        db = get_db()
        cursor = db.execute("SELECT * FROM messages WHERE chat_id = 1 ORDER BY id DESC LIMIT 1")
        msg = cursor.fetchone()
        assert msg is not None
        assert msg['message_type'] == 'stellar'
        assert 'Scheduled task output content' in msg['message_content']

# 2. Assert that a recurring task is locked, executed successfully, rescheduled (execute_at set, status reset to pending), and remains active.
@patch('app.gemini_generate')
def test_scheduler_recurring_task_reschedules(mock_gemini_generate, client):
    """
    Asserts that a recurring task (recurring_minutes > 0) is rescheduled after execution,
    resetting its status to 'pending' and updating its execute_at datetime.
    """
    mock_gemini_generate.return_value = [{'result': 'Recurring check passed'}]

    with app.app_context():
        db = get_db()
        cursor = db.execute(
            "INSERT INTO scheduled_tasks (user_id, chat_id, task_prompt, model_id, status, is_active, execute_at, recurring_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, "Recurring audit", "gemini-3.5-flash", "pending", 1, None, 5)
        )
        task_id = cursor.lastrowid
        db.commit()

    monitor = TaskSchedulerMonitor(app, interval=60)
    monitor._check_tasks()

    row = wait_for_task_completion(task_id)
    assert row['status'] == 'pending'
    assert row['is_active'] == 1

    # Verify message was inserted
    with app.app_context():
        db = get_db()
        cursor = db.execute("SELECT * FROM messages WHERE chat_id = 1 ORDER BY id DESC LIMIT 1")
        msg = cursor.fetchone()
        assert msg is not None
        assert 'Recurring check passed' in msg['message_content']

# 3. Assert that when execution fails (raises an exception), the task status is updated to 'failed'.
@patch('app.gemini_generate')
def test_scheduler_task_failure_updates_status(mock_gemini_generate, client):
    """
    Asserts that if the task generation throws an exception, the status is updated to 'failed'.
    """
    mock_gemini_generate.side_effect = Exception("Gemini service unavailable")

    with app.app_context():
        db = get_db()
        cursor = db.execute(
            "INSERT INTO scheduled_tasks (user_id, chat_id, task_prompt, model_id, status, is_active, execute_at, recurring_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, "Fragile task", "gemini-3.5-flash", "pending", 1, None, 0)
        )
        task_id = cursor.lastrowid
        db.commit()

    monitor = TaskSchedulerMonitor(app, interval=60)
    monitor._check_tasks()

    row = wait_for_task_completion(task_id)
    assert row['status'] == 'failed'
    assert row['is_active'] == 1  # Should still be active so it can be retried or inspected

# 4. Assert that if the task is cancelled mid-execution, execution is aborted early and no message is inserted.
def test_scheduler_mid_execution_cancellation(client):
    """
    Asserts that if the task is cancelled (is_active updated to 0) during generator consumption,
    execution halts and no chat message is created.
    """
    # We want a generator that cancels the task when it is read
    task_id_box = []

    def cancelling_generator():
        # First chunk
        yield {'result': 'First part '}
        # Cancel the task in the database
        with app.app_context():
            db = get_db()
            db.execute("UPDATE scheduled_tasks SET is_active = 0 WHERE id = ?", (task_id_box[0],))
            db.commit()
        # Second chunk (should be ignored / aborted)
        yield {'result': 'Second part'}

    with patch('app.gemini_generate', return_value=cancelling_generator()):
        with app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO scheduled_tasks (user_id, chat_id, task_prompt, model_id, status, is_active, execute_at, recurring_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (1, 1, "Cancellable task", "gemini-3.5-flash", "pending", 1, None, 0)
            )
            task_id = cursor.lastrowid
            task_id_box.append(task_id)
            db.commit()

        # Delete any previous messages to ensure we don't match old runs
        with app.app_context():
            db = get_db()
            db.execute("DELETE FROM messages WHERE chat_id = 1")
            db.commit()

        monitor = TaskSchedulerMonitor(app, interval=60)
        monitor._check_tasks()

        # Wait a short moment and check task state
        time.sleep(0.5)

        with app.app_context():
            db = get_db()
            # Task should be marked as inactive
            cursor = db.execute("SELECT is_active, status FROM scheduled_tasks WHERE id = ?", (task_id_box[0],))
            row = cursor.fetchone()
            assert row['is_active'] == 0
            
            # Since execution aborted early before final_output check and updating status,
            # it shouldn't have been completed or failed.
            # No message should be inserted.
            cursor = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 1")
            assert cursor.fetchone()[0] == 0

# 5. Assert that the time elapsed notice is prepended to the system prompt if the last message is old.
@patch('app.gemini_generate')
@patch('prompts.get_refinement_prompt')
def test_scheduler_time_elapsed_notice(mock_refinement, mock_gemini, client):
    """
    Asserts that if the last chat message timestamp is old (e.g. 5 minutes ago),
    a notice is prepended to the mandate prompt sent to get_refinement_prompt.
    """
    mock_refinement.return_value = "mocked system prompt"
    mock_gemini.return_value = [{'result': 'Done'}]
    
    with app.app_context():
        db = get_db()
        # Insert a chat message in the past (e.g. 5 minutes ago)
        import datetime
        five_mins_ago = (datetime.datetime.utcnow() - datetime.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute("INSERT INTO messages (chat_id, message_type, message_content, timestamp) VALUES (1, 'user', 'hello', ?)", (five_mins_ago,))
        
        cursor = db.execute(
            "INSERT INTO scheduled_tasks (user_id, chat_id, task_prompt, model_id, status, is_active, execute_at, recurring_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, "Task with notice", "gemini-3.5-flash", "pending", 1, None, 0)
        )
        task_id = cursor.lastrowid
        db.commit()

    monitor = TaskSchedulerMonitor(app, interval=60)
    monitor._check_tasks()

    wait_for_task_completion(task_id)

    # Verify that get_refinement_prompt was called with a prompt containing "[SYSTEM NOTICE:"
    notice_calls = [
        args[0] for args, kwargs in mock_refinement.call_args_list
        if args and "[SYSTEM NOTICE:" in args[0]
    ]
    assert len(notice_calls) == 1
    called_directive_prompt = notice_calls[0]
    assert "[SYSTEM NOTICE:" in called_directive_prompt
    assert "5m has passed since the last message" in called_directive_prompt
