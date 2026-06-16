# test_orchestrator_engine.py
import pytest
import os
import json
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import orchestrator.config as config
from orchestrator.engine import OrchestratorEngine, IST
from orchestrator.state import StateDB
from orchestrator.memory import MemoryDB

@pytest.fixture
def temp_dbs():
    """Fixture to set up clean, isolated SQLite databases for StateDB and MemoryDB tests."""
    fd1, path1 = tempfile.mkstemp(suffix=".db")
    fd2, path2 = tempfile.mkstemp(suffix=".db")
    
    # Pre-patch config paths so engine instances connect to these temp files
    with patch('orchestrator.config.DB_PATH', path1), \
         patch('orchestrator.config.MEMORY_DB_PATH', path2):
        yield path1, path2
        
    os.close(fd1)
    os.close(fd2)
    for p in (path1, path2):
        if os.path.exists(p):
            os.unlink(p)

# Brief comment: Asserts that _check_quota_error correctly identifies and parses different reset formats from logs, returning proper datetime offsets.
def test_check_quota_error_parses_various_formats(temp_dbs):
    engine = OrchestratorEngine()
    
    # Case 1: No quota error keyword in log
    assert engine._check_quota_error("Normal execution log output") is None
    
    # Case 2: Quota error present but reset time cannot be parsed -> returns 4 hours fallback
    now = datetime.now(IST)
    res = engine._check_quota_error("RESOURCE_EXHAUSTED: quota exceeded")
    assert res is not None
    # Verify fallback is roughly 4 hours
    diff = res - now
    assert abs(diff.total_seconds() - 4 * 3600) < 10
    
    # Case 3: Parse compact reset time 'Resets in 3h39m35s'
    res = engine._check_quota_error("RESOURCE_EXHAUSTED: Resets in 3h39m35s")
    assert res is not None
    expected_delta = timedelta(hours=3, minutes=39, seconds=35)
    expected_time = now + expected_delta + timedelta(minutes=2)
    assert abs((res - expected_time).total_seconds()) < 10

    # Case 4: Parse partial reset time 'Resets in 15m'
    res = engine._check_quota_error("Error 429: Resets in 15m")
    assert res is not None
    expected_delta = timedelta(minutes=15)
    expected_time = now + expected_delta + timedelta(minutes=2)
    assert abs((res - expected_time).total_seconds()) < 10

# Brief comment: Asserts that _get_next_pipeline_agent finds the next non-event-based agent in execution order.
def test_get_next_pipeline_agent_returns_correct_successor(temp_dbs):
    engine = OrchestratorEngine()
    
    # 'bolt' is first. The next non-event-based should be 'sentinel'
    next_agent = engine._get_next_pipeline_agent("bolt")
    assert next_agent is not None
    assert next_agent["id"] == "sentinel"

    # 'proton' is last non-event-based. The next should loop back to 'bolt'
    next_agent = engine._get_next_pipeline_agent("proton")
    assert next_agent is not None
    assert next_agent["id"] == "bolt"

# Brief comment: Asserts that _get_due_agent identifies due agents correctly based on the current schedule and previous run outcomes today.
def test_get_due_agent_checks_schedules_and_state(temp_dbs):
    engine = OrchestratorEngine()
    
    # Mock current time: 10:00 AM IST
    now = datetime.combine(datetime.today(), datetime.strptime("10:00", "%H:%M").time()).replace(tzinfo=IST)
    
    # Bolt (06:00) and Sentinel (09:00) are due. Palette (12:00) is not.
    # 1. No runs today: return first due (bolt)
    due = engine._get_due_agent(now)
    assert due is not None
    assert due["id"] == "bolt"
    
    # 2. Bolt completed successfully today: return next due (sentinel)
    engine.state_db.start_run("bolt", "branch", now.isoformat())
    last_run = engine.state_db.get_last_run_for_agent("bolt")
    engine.state_db.complete_run(last_run["id"], now.isoformat(), pr_status="PENDING")
    
    due = engine._get_due_agent(now)
    assert due is not None
    assert due["id"] == "sentinel"

    # 3. Sentinel failed today: still due for retry
    engine.state_db.start_run("sentinel", "branch", now.isoformat())
    last_run = engine.state_db.get_last_run_for_agent("sentinel")
    engine.state_db.fail_run(last_run["id"], now.isoformat(), "Error log")
    
    due = engine._get_due_agent(now)
    assert due is not None
    assert due["id"] == "sentinel"

    # 4. Sentinel currently running today: skipped
    engine.state_db.start_run("sentinel", "branch", now.isoformat())
    
    due = engine._get_due_agent(now)
    # Both bolt and sentinel are handled (bolt completed, sentinel running), so None is returned
    assert due is None

# Brief comment: Asserts that _recover_state handles clean startup recovery, detecting active containers and failing inactive ones.
def test_recover_state_handles_active_and_dead_runs(temp_dbs):
    # Setup database with a running run
    state_db = StateDB(temp_dbs[0])
    run_id = state_db.start_run("bolt", "agent/bolt/dev", "2026-06-13T12:00:00", model="Gemini 3.5 Flash")
    
    # Case 1: Recovery when agy process is active inside container
    with patch("orchestrator.container.exec_in_container", return_value=(0, "agy process running", "")):
        engine = OrchestratorEngine()
        assert engine.current_agent_id == "bolt"
        assert engine.current_run_id == run_id
        assert engine.branch_name == "agent/bolt/dev"
        assert engine.current_model == "Gemini 3.5 Flash"
        assert engine.current_process is not None
        
        # Test the RecoveredProcess behavior
        # Process still active
        with patch("orchestrator.container.exec_in_container", return_value=(0, "agy process running", "")):
            assert engine.current_process.poll() is None
            
        # Process completed
        with patch("orchestrator.container.exec_in_container", return_value=(1, "", "")):
            assert engine.current_process.poll() == 0
            
        # Process kill invocation
        with patch("orchestrator.container.exec_in_container") as mock_exec:
            engine.current_process.kill()
            mock_exec.assert_called_with("pkill -f agy")
            
    # Case 2: Recovery when agy process is NOT active (should fail run and clean up)
    state_db.start_run("sentinel", "agent/sentinel/dev", "2026-06-13T13:00:00")
    with patch("orchestrator.container.exec_in_container", return_value=(1, "", "")) as mock_exec:
        with patch("orchestrator.container.unload_agent_prompt") as mock_unload:
            engine = OrchestratorEngine()
            
            # Should have failed the run
            last_run = engine.state_db.get_last_run_for_agent("sentinel")
            assert last_run["status"] == "FAILED"
            assert "restarted" in last_run["error_message"]
            mock_unload.assert_called_once()
            assert engine.current_agent_id is None

# Brief comment: Asserts that _process_agent_memory_outbox successfully copies, parses, and persists tasks, memories, messages, and facts.
def test_process_agent_memory_outbox_success(temp_dbs):
    engine = OrchestratorEngine()
    
    # Setup database records for resolution/superseding checks
    # Seed active tasks to be resolved
    task_id = engine.memory_db.create_task("Task to resolve", "desc", "admin", assigned_to="bolt") # ID will be 1
    # Seed active fact to be superseded
    fact_id = engine.memory_db.add_fact("Old Fact", "sentinel", category="architecture") # ID will be 1

    # Create mock outbox contents
    mock_outbox_data = {
        "memories": [
            {"type": "observation", "content": "Memory observation content", "scope": "global", "tags": ["tag1"]},
            {"type": "decision", "content": "Memory decision content", "scope": "bolt", "tags": ["tag2"]}
        ],
        "messages": [
            {"channel": "group", "content": "Group chat announcement"},
            {"channel": "dm", "to": "palette", "content": "DM message to palette", "thread_id": "thread_1", "message_type": "text"}
        ],
        "tasks_resolved": [task_id],
        "tasks_created": [
            {"title": "Task Title", "description": "Task Desc", "assigned_to": "palette", "priority": "high", "tags": ["tagA"]}
        ],
        "facts": [
            {"fact": "New Fact Content", "category": "convention"}
        ],
        "facts_updated": [
            {"id": fact_id, "fact": "Superseded Fact Content", "category": "architecture"}
        ]
    }
    
    # Write mock data to the host path where container copy extracts it
    orchestrator_dir = os.path.dirname(temp_dbs[0])
    host_outbox_path = os.path.join(orchestrator_dir, "memory_outbox.json")
    
    with open(host_outbox_path, "w") as f:
        json.dump(mock_outbox_data, f)
        
    with patch("orchestrator.container.read_memory_outbox_from_container", return_value=True):
        engine._process_agent_memory_outbox("bolt", 100, "Finished work summary")
        
        # Verify memories were added
        mems = engine.memory_db.get_recent_memories(limit=10)
        # 2 from outbox + 1 for run summary
        assert len(mems) == 3
        # Ensure final summary is outcome
        summaries = [m for m in mems if m["memory_type"] == "outcome"]
        assert len(summaries) == 1
        assert "Finished work summary" in summaries[0]["content"]
        
        # Verify group and DM messages
        group_msgs = engine.memory_db.get_recent_group_messages(hours=1)
        # 1 from outbox + 1 for run summary message
        assert len(group_msgs) == 2
        
        # Verify task updates: task 1 updated to fix_submitted (since assignee bolt resolved it)
        with engine.memory_db._get_conn() as conn:
            task = conn.execute("SELECT status FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
            assert task["status"] == "fix_submitted"
            
            # Verify new task was created
            new_task = conn.execute("SELECT * FROM agent_tasks WHERE title = 'Task Title'").fetchone()
            assert new_task is not None
            assert new_task["assigned_to"] == "palette"
            assert new_task["priority"] == "high"
            
            # Verify new fact was created
            new_fact = conn.execute("SELECT * FROM agent_facts WHERE fact = 'New Fact Content'").fetchone()
            assert new_fact is not None
            
            # Verify old fact was superseded
            old_fact = conn.execute("SELECT * FROM agent_facts WHERE id = ?", (fact_id,)).fetchone()
            assert old_fact["archived"] == 1
            assert old_fact["superseded_by"] is not None

# Brief comment: Asserts that _check_memory_summarization schedules memory summarization correctly after 12 hours.
def test_check_memory_summarization_scheduling(temp_dbs):
    engine = OrchestratorEngine()
    engine._run_memory_summarization = MagicMock()
    
    now = datetime.now(IST)
    
    # Case 1: First time run (no stored time) -> triggers summarization
    engine._check_memory_summarization(now)
    engine._run_memory_summarization.assert_called_once()
    assert engine.state_db.get_state("last_memory_summarization_time") == now.isoformat()
    
    engine._run_memory_summarization.reset_mock()
    
    # Case 2: Run within 12 hours -> skips
    engine._check_memory_summarization(now + timedelta(hours=5))
    engine._run_memory_summarization.assert_not_called()
    
    # Case 3: Run after 12 hours -> triggers
    future_now = now + timedelta(hours=13)
    engine._check_memory_summarization(future_now)
    engine._run_memory_summarization.assert_called_once()
    assert engine.state_db.get_state("last_memory_summarization_time") == future_now.isoformat()
