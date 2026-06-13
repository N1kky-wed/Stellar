# test_orchestrator_cooldown.py
import pytest
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import orchestrator.config as config
from orchestrator.engine import OrchestratorEngine, IST
from orchestrator.state import StateDB

@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    yield path
    os.close(fd)
    if os.path.exists(path):
        os.unlink(path)

@pytest.fixture
def mock_dependencies():
    with patch('orchestrator.engine.MemoryDB') as mock_mem:
        with patch('orchestrator.engine.container') as mock_container:
            yield mock_mem, mock_container

def test_model_specific_cooldown_restore(temp_db, mock_dependencies):
    """Asserts that model-specific and global cooldowns are restored correctly on startup."""
    state_db = StateDB(temp_db)
    now = datetime.now(IST)
    
    # Set mock cooldowns in database
    gemini_time = now + timedelta(hours=1)
    claude_time = now + timedelta(hours=2)
    global_time = now + timedelta(hours=3)
    
    state_db.set_state("gemini_cooldown_until", gemini_time.isoformat())
    state_db.set_state("claude_cooldown_until", claude_time.isoformat())
    state_db.set_state("quota_cooldown_until", global_time.isoformat())
    
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        assert engine.gemini_cooldown_until is not None
        assert engine.claude_cooldown_until is not None
        assert engine.quota_cooldown_until is not None

def test_get_active_model(temp_db, mock_dependencies):
    """Asserts model selection based on current model cooldown state."""
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        now = datetime.now(IST)
        
        # 1. No cooldowns: select Gemini
        assert engine._get_active_model(now) == config.MODEL_GEMINI
        
        # 2. Gemini blocked, Claude free: select Claude
        engine.gemini_cooldown_until = now + timedelta(minutes=30)
        assert engine._get_active_model(now) == config.MODEL_CLAUDE
        
        # 3. Both blocked: fallback to Gemini as safe default
        engine.claude_cooldown_until = now + timedelta(minutes=45)
        assert engine._get_active_model(now) == config.MODEL_GEMINI

def test_is_in_cooldown_logic(temp_db, mock_dependencies):
    """Asserts global cooldown logic: only block completely if both Gemini and Claude are exhausted."""
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        now = datetime.now(IST)
        
        # 1. No cooldowns: is_in_cooldown returns False
        assert engine._is_in_cooldown(now) is False
        
        # 2. Only Gemini in cooldown: is_in_cooldown returns False (can still run Claude)
        engine.gemini_cooldown_until = now + timedelta(minutes=30)
        assert engine._is_in_cooldown(now) is False
        
        # 3. Both in cooldown: is_in_cooldown returns True and schedules global reset to the earliest
        engine.claude_cooldown_until = now + timedelta(minutes=10) # Claude recovers earlier
        assert engine._is_in_cooldown(now) is True
        assert engine.quota_cooldown_until == engine.claude_cooldown_until
        
        # 4. Cleans up expired cooldowns
        future_now = now + timedelta(minutes=15)
        assert engine._is_in_cooldown(future_now) is False
        assert engine.claude_cooldown_until is None
        assert engine.quota_cooldown_until is None

@patch('orchestrator.engine.container')
def test_quota_error_triggers_claude_fallback(mock_container, temp_db, mock_dependencies):
    """Asserts that Gemini quota error automatically triggers a retry using Claude Sonnet."""
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        now = datetime.now(IST)
        
        # Setup mock running agent state using Gemini
        engine.current_agent_id = "bolt"
        engine.current_run_id = 1
        engine.agent_start_time = now - timedelta(minutes=5)
        engine.current_model = config.MODEL_GEMINI
        engine.branch_name = "agent/bolt/test"
        
        mock_process = MagicMock()
        mock_process.poll.return_value = 0 # process finished
        engine.current_process = mock_process
        
        # Mock drain_stdout to avoid fcntl type errors in test environment
        engine._drain_stdout = MagicMock()
        
        # Mock logs with resource exhausted error
        engine._get_agy_log_tail = MagicMock(return_value="RESOURCE_EXHAUSTED: Resets in 1h30m")
        engine._start_agent = MagicMock()
        
        engine._check_running_agent(now)
        
        # Assert Gemini cooldown was set in the database
        assert engine.gemini_cooldown_until is not None
        
        # Assert Claude fallback retry was triggered immediately
        engine._start_agent.assert_called_once()
        called_agent = engine._start_agent.call_args[0][0]
        assert called_agent["id"] == "bolt"
