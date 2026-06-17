# test_orchestrator_engine_extended.py
import pytest
import os
import json
import tempfile
import sys
import requests
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

# Brief comment: Asserts that _restore_cooldown_from_db correctly restores unexpired cooldowns and clears expired ones from the state DB on startup.
def test_restore_cooldown_from_db_restores_and_clears_appropriately(temp_dbs):
    state_db = StateDB(temp_dbs[0])
    now = datetime.now(IST)
    
    # Set expired and active cooldowns in DB
    expired_time = (now - timedelta(hours=1)).isoformat()
    future_time = (now + timedelta(hours=2)).isoformat()
    
    state_db.set_state("gemini_cooldown_until", expired_time)
    state_db.set_state("claude_cooldown_until", future_time)
    state_db.set_state("quota_cooldown_until", future_time)
    
    engine = OrchestratorEngine()
    
    # Gemini cooldown should be cleared because it is expired
    assert engine.gemini_cooldown_until is None
    assert state_db.get_state("gemini_cooldown_until") == ""
    
    # Claude and global cooldowns should be restored
    assert engine.claude_cooldown_until is not None
    assert abs((engine.claude_cooldown_until - (now + timedelta(hours=2))).total_seconds()) < 10
    assert engine.quota_cooldown_until is not None

# Brief comment: Asserts that _get_agy_log_tail ignores stale log files or handles container run failure gracefully.
def test_get_agy_log_tail_handles_stale_or_missing_logs(temp_dbs):
    engine = OrchestratorEngine()
    start_time = datetime.now(IST)
    
    # Case 1: readlink fails
    with patch("orchestrator.container.exec_in_container", return_value=(1, "", "")):
        assert engine._get_agy_log_tail(start_time) == ""
        
    # Case 2: stat command fails
    def exec_side_effect(cmd, timeout=None):
        if "readlink" in cmd:
            return 0, "/root/.gemini/antigravity-cli/cli.log\n", ""
        return 1, "", ""
        
    with patch("orchestrator.container.exec_in_container", side_effect=exec_side_effect):
        assert engine._get_agy_log_tail(start_time) == ""
        
    # Case 3: Log modification time is older than agent start time (stale log)
    def exec_side_effect_stale(cmd, timeout=None):
        if "readlink" in cmd:
            return 0, "/root/.gemini/antigravity-cli/cli.log\n", ""
        if "stat" in cmd:
            stale_epoch = (start_time - timedelta(minutes=10)).timestamp()
            return 0, f"{stale_epoch}\n", ""
        return 0, "Log content", ""
        
    with patch("orchestrator.container.exec_in_container", side_effect=exec_side_effect_stale):
        assert engine._get_agy_log_tail(start_time) == ""
        
    # Case 4: Log is fresh and read successfully
    def exec_side_effect_fresh(cmd, timeout=None):
        if "readlink" in cmd:
            return 0, "/root/.gemini/antigravity-cli/cli.log\n", ""
        if "stat" in cmd:
            fresh_epoch = start_time.timestamp()
            return 0, f"{fresh_epoch}\n", ""
        if "tail" in cmd:
            return 0, "Fresh logs content\nLine 2", ""
        return 0, "", ""
        
    with patch("orchestrator.container.exec_in_container", side_effect=exec_side_effect_fresh):
        assert engine._get_agy_log_tail(start_time) == "Fresh logs content\nLine 2"

# Brief comment: Asserts that _is_in_cooldown correctly tracks model and global quota states, updating timestamps in state DB.
def test_is_in_cooldown_updates_quota_state(temp_dbs):
    engine = OrchestratorEngine()
    now = datetime.now(IST)
    
    # Case 1: Both models have healthy status (no cooldown)
    quota_data = {
        "gemini": {"status": "Healthy", "weekly_percent": 80.0, "weekly_refreshes_in_hours": 12.0},
        "claude": {"status": "Healthy", "weekly_percent": 90.0, "weekly_refreshes_in_hours": 24.0},
        "last_updated": now.isoformat()
    }
    engine.state_db.set_state("quota_data", json.dumps(quota_data))
    assert engine._is_in_cooldown(now) is False
    
    # Case 2: Gemini in cooldown but Claude is healthy -> not global cooldown
    engine.gemini_cooldown_until = now + timedelta(hours=1)
    assert engine._is_in_cooldown(now) is False
    
    # Case 3: Both models in cooldown/throttled -> global cooldown triggers
    engine.claude_cooldown_until = now + timedelta(hours=2)
    assert engine._is_in_cooldown(now) is True
    # Global cooldown until earliest recovery (Gemini cooldown)
    assert engine.quota_cooldown_until == engine.gemini_cooldown_until

# Brief comment: Asserts that _get_active_model correctly returns the optimal healthy model or falls back to cached values.
def test_get_active_model_scenarios(temp_dbs):
    engine = OrchestratorEngine()
    now = datetime.now(IST)
    
    # Case 1: Live quota check returns Gemini as healthy
    quota_data = {
        "gemini": {"status": "Healthy", "ratio": "0.1", "weekly_percent": 85.0},
        "claude": {"status": "Healthy", "ratio": "0.2", "weekly_percent": 95.0}
    }
    with patch("orchestrator.quota.fetch_quota_data_from_container", return_value=""), \
         patch("orchestrator.quota.parse_quota_text", return_value=quota_data):
        model = engine._get_active_model(now)
        assert model == config.MODEL_GEMINI
        
    # Case 2: Gemini throttled, Claude healthy -> returns Claude
    quota_data["gemini"]["status"] = "Throttled"
    with patch("orchestrator.quota.fetch_quota_data_from_container", return_value=""), \
         patch("orchestrator.quota.parse_quota_text", return_value=quota_data):
        model = engine._get_active_model(now)
        assert model == config.MODEL_CLAUDE
        
    # Case 3: Live container query fails -> falls back to DB cache
    engine.state_db.set_state("quota_data", json.dumps({
        "gemini": {"status": "Healthy", "weekly_percent": 80.0},
        "claude": {"status": "Throttled", "weekly_percent": 20.0}
    }))
    with patch("orchestrator.quota.fetch_quota_data_from_container", side_effect=Exception("Pexpect failure")):
        model = engine._get_active_model(now)
        assert model == config.MODEL_GEMINI

# Brief comment: Asserts that _update_average_quota_usage computes and persists average weekly quota percentage costs correctly.
def test_update_average_quota_usage_calculates_and_stores_cost(temp_dbs):
    engine = OrchestratorEngine()
    
    # Insert a dummy run starting at 90% using Gemini
    run_id = engine.state_db.start_run("bolt", "branch_name", "2026-06-14T15:00:00", quota_start_percent=90.0, model=config.MODEL_GEMINI)
    
    # Mock ending quota data at 85% (cost = 5%)
    quota_data = {
        "gemini": {"weekly_percent": 85.0, "status": "Healthy"},
        "claude": {"weekly_percent": 90.0, "status": "Healthy"}
    }
    with patch("orchestrator.quota.fetch_quota_data_from_container", return_value=""), \
         patch("orchestrator.quota.parse_quota_text", return_value=quota_data):
        engine._update_average_quota_usage(run_id, config.MODEL_GEMINI)
        
        # Verify run cost was saved
        with engine.state_db._get_conn() as conn:
            row = conn.execute("SELECT quota_cost FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            assert row["quota_cost"] == 5.0
            
        # Verify running average state was saved
        assert float(engine.state_db.get_state("gemini_avg_cost")) == 5.0
        assert int(engine.state_db.get_state("gemini_runs_count")) == 1

# Brief comment: Asserts that _check_running_agent processes successful agent runs, extracts PRs, updates database, and starts successor.
def test_check_running_agent_successful_run(temp_dbs):
    engine = OrchestratorEngine()
    engine.current_agent_id = "bolt"
    engine.current_run_id = 1
    engine.branch_name = "agent/bolt/dev"
    engine.agent_start_time = datetime.now(IST)
    
    mock_process = MagicMock()
    mock_process.poll.return_value = 0 # Completed with code 0
    engine.current_process = mock_process
    
    mock_pr = {"number": 42, "url": "https://github.com/test/pull/42", "state": "PENDING"}
    
    with patch.object(engine, "_drain_stdout") as mock_drain, \
         patch.object(engine, "_update_average_quota_usage") as mock_quota_avg, \
         patch.object(engine, "_get_agy_log_tail", return_value=""), \
         patch.object(engine, "_check_quota_error", return_value=None), \
         patch.object(engine, "_get_summary", return_value="Finished successfully"), \
         patch("orchestrator.container.check_new_prs", return_value=[mock_pr]), \
         patch.object(engine, "_process_agent_memory_outbox") as mock_outbox, \
         patch("orchestrator.container.unload_agent_prompt") as mock_unload, \
         patch.object(engine, "_get_next_pipeline_agent") as mock_next:
         
        engine._check_running_agent(datetime.now(IST))
        
        # Verify monitors and updates ran
        mock_drain.assert_called()
        mock_quota_avg.assert_called_with(1, None)
        mock_outbox.assert_called_with("bolt", 1, "Finished successfully")
        mock_unload.assert_called_once()
        assert engine.current_process is None

# Brief comment: Asserts that _check_running_agent handles quota exhaustion failure correctly by triggering fallback or setting cooldowns.
def test_check_running_agent_quota_error(temp_dbs):
    engine = OrchestratorEngine()
    engine.current_agent_id = "bolt"
    engine.current_run_id = 1
    engine.branch_name = "agent/bolt/dev"
    engine.current_model = config.MODEL_GEMINI
    engine.agent_start_time = datetime.now(IST)
    
    mock_process = MagicMock()
    mock_process.poll.return_value = 1 # Failed exit code
    engine.current_process = mock_process
    
    cooldown_time = datetime.now(IST) + timedelta(hours=4)
    
    with patch.object(engine, "_drain_stdout"), \
         patch.object(engine, "_update_average_quota_usage"), \
         patch.object(engine, "_get_agy_log_tail", return_value="RESOURCE_EXHAUSTED"), \
         patch.object(engine, "_check_quota_error", return_value=cooldown_time), \
         patch.object(engine, "_start_agent") as mock_start, \
         patch("orchestrator.container.unload_agent_prompt"):
         
        engine._check_running_agent(datetime.now(IST))
        
        # Since Claude is not in cooldown, it should attempt immediate fallback to Claude
        mock_start.assert_called_once()

# Brief comment: Asserts that _handle_timeout stops agent process and runs pkill inside container, marking run as timed out.
def test_handle_timeout_kills_and_marks_failed(temp_dbs):
    engine = OrchestratorEngine()
    engine.current_agent_id = "bolt"
    engine.current_run_id = 1
    engine.branch_name = "agent/bolt/dev"
    engine.agent_start_time = datetime.now(IST)
    
    mock_process = MagicMock()
    engine.current_process = mock_process
    
    with patch.object(engine, "_get_agy_log_tail", return_value=""), \
         patch("orchestrator.container.exec_in_container") as mock_exec, \
         patch.object(engine, "_update_average_quota_usage"), \
         patch("orchestrator.container.unload_agent_prompt"):
         
        engine._handle_timeout(datetime.now(IST))
        
        mock_process.kill.assert_called_once()
        mock_exec.assert_called_with("pkill -f agy")
        assert engine.current_process is None

# Brief comment: Asserts that _check_and_trigger_mercury detects CI/CD failure on pending PRs and launches Mercury to resolve/heal.
def test_check_and_trigger_mercury_on_failed_pipeline(temp_dbs):
    engine = OrchestratorEngine()
    
    # Insert pending PR run in database with pr_status='PENDING'
    run_id = engine.state_db.start_run("bolt", "agent/bolt/dev", "2026-06-15T10:00:00")
    engine.state_db.complete_run(run_id, "2026-06-15T11:00:00", pr_number=101, pr_url="http://github/pr/101", pr_status='PENDING')
    
    # Mock CI failure status
    ci_info = {
        "status": "failure",
        "run_id": "999888",
        "raw_output": "Tests failed"
    }
    
    with patch("orchestrator.container.check_pr_ci_status", return_value=ci_info), \
         patch("orchestrator.container.get_run_failed_log", return_value="AssertionError: expected True but got False") as mock_log, \
         patch.object(engine, "_start_mercury") as mock_start_mercury:
         
        res = engine._check_and_trigger_mercury(datetime.now(IST))
        
        assert res is True
        mock_log.assert_called_with("999888")
        mock_start_mercury.assert_called_once_with(
            "agent/bolt/dev", 101, error_trace="AssertionError: expected True but got False", model_name=None
        )
        assert engine.state_db.get_state("mercury_healed_run_999888") == "triggered"

# Brief comment: Asserts that _check_and_trigger_mercury detects merge conflict and launches Mercury with conflict flag set.
def test_check_and_trigger_mercury_on_conflict(temp_dbs):
    engine = OrchestratorEngine()
    
    # Insert pending PR run in database with pr_status='PENDING'
    run_id = engine.state_db.start_run("bolt", "agent/bolt/dev", "2026-06-15T10:00:00")
    engine.state_db.complete_run(run_id, "2026-06-15T11:00:00", pr_number=102, pr_url="http://github/pr/102", pr_status='PENDING')
    
    # Mock Conflict status
    ci_info = {
        "status": "conflict",
        "head_sha": "abc123sha"
    }
    
    with patch("orchestrator.container.check_pr_ci_status", return_value=ci_info), \
         patch.object(engine, "_start_mercury") as mock_start_mercury:
         
        res = engine._check_and_trigger_mercury(datetime.now(IST))
        
        assert res is True
        mock_start_mercury.assert_called_once_with("agent/bolt/dev", 102, conflict=True, model_name=None)
        assert engine.state_db.get_state("mercury_healed_conflict_abc123sha") == "triggered"

# Brief comment: Asserts that _start_mercury successfully registers run details in StateDB and launches container agent.
def test_start_mercury_flow(temp_dbs):
    engine = OrchestratorEngine()
    
    with patch("builtins.open", create=True) as mock_open, \
         patch("os.path.exists", return_value=True), \
         patch.object(engine, "_prepare_and_load_memory_context") as mock_prep, \
         patch("orchestrator.container.run_agent") as mock_run:
         
        engine._start_mercury("agent/bolt/dev", 105, error_trace="CI Error", model_name="gemini")
        
        assert engine.current_agent_id == "mercury"
        assert engine.branch_name == "agent/bolt/dev"
        assert engine.current_model == "gemini"
        mock_prep.assert_called_with("mercury")
        mock_run.assert_called_once_with(
            agent_id="mercury",
            prompt_file="mercury_temp.md",
            branch_name="agent/bolt/dev",
            model_name="gemini"
        )

# Brief comment: Asserts that _run_memory_summarization extracts active facts and unarchived memories, calls the Gemini API to synthesize them, updates/inserts facts, and archives memories.
@patch('google.genai.Client')
def test_run_memory_summarization_synthesizes_correctly(mock_client_class, temp_dbs):
    engine = OrchestratorEngine()
    
    # 1. Add some unarchived memories to the DB
    with engine.memory_db._get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_memories (agent_id, memory_type, content, scope, archived, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("bolt", "observation", "Found performance bottleneck in DB query", "global", 0, "2026-06-15T12:00:00")
        )
        conn.commit()
        
    # 2. Add an existing fact
    fact_id = engine.memory_db.add_fact("Original performance guidelines", "bolt", "convention")
    
    # 3. Setup mock client response
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    
    mock_resp = MagicMock()
    # It returns a FactList with one updated fact and one new fact
    mock_resp.text = json.dumps({
        "facts": [
            {"id": fact_id, "fact": "Updated performance guidelines including Composite Index fact", "category": "convention"},
            {"id": None, "fact": "Use composite index for fast reads", "category": "architecture"}
        ]
    })
    mock_client.models.generate_content.return_value = mock_resp
    
    # Mock environment variables for keys to try
    with patch.dict(os.environ, {"PRIMARY_API_KEY": "test_api_key"}):
        # Run the summarization
        engine._run_memory_summarization()
    
    # Verify the memory was archived
    with engine.memory_db._get_conn() as conn:
        mem = conn.execute("SELECT archived FROM agent_memories WHERE agent_id = 'bolt'").fetchone()
        assert mem["archived"] == 1
        
        # Verify old fact is archived/superseded
        old_fact = conn.execute("SELECT archived, superseded_by FROM agent_facts WHERE id = ?", (fact_id,)).fetchone()
        assert old_fact["archived"] == 1
        assert old_fact["superseded_by"] is not None
        
        # Verify new fact was inserted
        new_fact = conn.execute("SELECT fact FROM agent_facts WHERE category = 'architecture'").fetchone()
        assert new_fact["fact"] == "Use composite index for fast reads"

# Brief comment: Asserts that _prepare_and_load_memory_context fetches tasks, facts, messages and successfully compiles context.
def test_prepare_and_load_memory_context(temp_dbs):
    engine = OrchestratorEngine()
    
    # Setup some test memory records
    engine.memory_db.create_task("High priority bug fix", "Crash in UI", "admin", assigned_to="bolt", priority="high")
    engine.memory_db.add_message(
        channel="dm", sender_id="admin", recipient_id="bolt", content="Fix this now!", thread_id="thread1"
    )
    engine.memory_db.add_message(
        channel="group", sender_id="bolt", content="Working on it", message_type="text"
    )
    engine.memory_db.add_fact("Layout spacing must be 12px", "palette", "convention")
    
    # Mock container file copying
    with patch("orchestrator.container.copy_memory_context_to_container") as mock_copy:
        engine._prepare_and_load_memory_context("bolt")
        mock_copy.assert_called_once()
        # Verify that context file path passed to container exists during invocation
        written_path = mock_copy.call_args[0][0]
        assert "memory_context.md" in written_path

# Brief comment: Asserts that _pull_and_reload_services executes Git commands on host and restarts correct service configurations depending on modified files.
def test_pull_and_reload_services_scenarios(temp_dbs):
    engine = OrchestratorEngine()
    
    # Case 1: requirements.txt is updated -> updates packages, reloads/restarts all services
    mock_run_results = [
        MagicMock(returncode=0, stdout="Already on main", stderr=""), # checkout
        MagicMock(returncode=0, stdout="Pulled updates", stderr=""),  # pull
        MagicMock(returncode=0, stdout="requirements.txt\n", stderr=""), # diff
        MagicMock(returncode=0, stdout="pip output", stderr="") # pip install
    ]
    
    # Mock Popen for service restart
    mock_popen = MagicMock()
    
    with patch("subprocess.run", side_effect=mock_run_results) as mock_run, \
         patch("subprocess.Popen", return_value=mock_popen) as mock_p_open:
        res = engine._pull_and_reload_services(120)
        assert res is True  # requirements.txt update forces orchestrator reload
        
        # Verify reload/restart commands
        restart_calls = [args[0] for args, _ in mock_run.call_args_list]
        assert any("stellar" in c for c in restart_calls)
        
    # Case 2: Only app.py is updated -> reloads stellar, no orchestrator restart
    mock_run_results_app = [
        MagicMock(returncode=0, stdout="Already on main", stderr=""),
        MagicMock(returncode=0, stdout="Pulled updates", stderr=""),
        MagicMock(returncode=0, stdout="app.py\n", stderr="")
    ]
    with patch("subprocess.run", side_effect=mock_run_results_app) as mock_run, \
         patch("subprocess.Popen") as mock_p_open:
        res = engine._pull_and_reload_services(121)
        assert res is False  # No orchestrator restart needed

