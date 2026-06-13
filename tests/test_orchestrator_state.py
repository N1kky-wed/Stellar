# test_orchestrator_state.py
import pytest
import os
import tempfile
from orchestrator.state import StateDB

@pytest.fixture
def temp_db():
    """Fixture to create and clean up a temporary SQLite database for StateDB testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    yield path
    os.close(fd)
    if os.path.exists(path):
        os.unlink(path)

def test_init_db(temp_db):
    """Asserts that StateDB initializes agent_runs and orchestrator_state tables and executes WAL pragma."""
    state_db = StateDB(temp_db)
    with state_db._get_conn() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        assert "agent_runs" in table_names
        assert "orchestrator_state" in table_names

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

        # Check self-healing column addition works (no operational error, summary_message exists)
        info = conn.execute("PRAGMA table_info(agent_runs)").fetchall()
        column_names = [col[1] for col in info]
        assert "summary_message" in column_names

def test_agent_runs_lifecycle(temp_db):
    """Asserts the lifecycle of agent runs (start, complete, fail, timeout, interrupt, and PR updates)."""
    state_db = StateDB(temp_db)
    
    # 1. Start run
    run_id = state_db.start_run("bolt", "agent/bolt/test", "2026-06-13T12:00:00")
    assert run_id > 0
    
    current = state_db.get_current_run()
    assert current is not None
    assert current["agent_id"] == "bolt"
    assert current["status"] == "RUNNING"
    assert current["branch_name"] == "agent/bolt/test"
    
    # 2. Complete run
    state_db.complete_run(run_id, "2026-06-13T12:05:00", pr_number=45, pr_url="https://github.com/pr/45", pr_status="PENDING", summary_message="Completed successfully")
    
    assert state_db.get_current_run() is None
    
    last_run = state_db.get_last_run_for_agent("bolt")
    assert last_run["status"] == "COMPLETED"
    assert last_run["pr_number"] == 45
    assert last_run["summary_message"] == "Completed successfully"
    
    # 3. Fail run
    run_id2 = state_db.start_run("sentinel", "agent/sentinel/test", "2026-06-13T13:00:00")
    state_db.fail_run(run_id2, "2026-06-13T13:02:00", error_message="Build failure", summary_message="Failed task")
    
    last_run2 = state_db.get_last_run_for_agent("sentinel")
    assert last_run2["status"] == "FAILED"
    assert last_run2["error_message"] == "Build failure"
    assert last_run2["summary_message"] == "Failed task"
    
    # 4. Timeout run
    run_id3 = state_db.start_run("palette", "agent/palette/test", "2026-06-13T14:00:00")
    state_db.timeout_run(run_id3, "2026-06-13T14:30:00", summary_message="Timed out waiting")
    
    last_run3 = state_db.get_last_run_for_agent("palette")
    assert last_run3["status"] == "TIMEOUT"
    assert last_run3["summary_message"] == "Timed out waiting"
    
    # 5. Interrupt run
    run_id4 = state_db.start_run("newton", "agent/newton/test", "2026-06-13T15:00:00")
    state_db.interrupt_run(run_id4, "2026-06-13T15:05:00", error_message="Orchestrator killed", summary_message="Interrupted")
    
    last_run4 = state_db.get_last_run_for_agent("newton")
    assert last_run4["status"] == "INTERRUPTED"
    assert last_run4["error_message"] == "Orchestrator killed"
    assert last_run4["summary_message"] == "Interrupted"

def test_pr_management(temp_db):
    """Asserts updates and queries on pending pull requests."""
    state_db = StateDB(temp_db)
    
    run_id = state_db.start_run("bolt", "branch1", "started")
    state_db.complete_run(run_id, "finished", pr_number=100, pr_url="pr_url", pr_status="PENDING")
    
    pending = state_db.get_pending_prs()
    assert len(pending) == 1
    assert pending[0]["pr_number"] == 100
    
    state_db.set_pr_info(run_id, 101, "new_pr_url", "PENDING")
    pending = state_db.get_pending_prs()
    assert len(pending) == 1
    assert pending[0]["pr_number"] == 101
    
    state_db.update_pr_status(run_id, "MERGED")
    assert len(state_db.get_pending_prs()) == 0

def test_get_last_run_nonexistent(temp_db):
    """Asserts that querying non-existent agent runs returns None."""
    state_db = StateDB(temp_db)
    assert state_db.get_last_run_for_agent("nonexistent") is None

def test_orchestrator_state_kv(temp_db):
    """Asserts that orchestrator state K-V store handles inserts and updates on conflict."""
    state_db = StateDB(temp_db)
    
    assert state_db.get_state("my_key") is None
    
    state_db.set_state("my_key", "value1")
    assert state_db.get_state("my_key") == "value1"
    
    state_db.set_state("my_key", "value2")
    assert state_db.get_state("my_key") == "value2"
