import pytest
import os
import tempfile
import json
from datetime import datetime
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
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch('orchestrator.config.HOST_AGENTS_DIR', temp_dir):
            with patch('orchestrator.engine.MemoryDB') as mock_mem:
                with patch('orchestrator.engine.container') as mock_container:
                    yield mock_mem, mock_container

def test_check_and_trigger_mercury_no_run_or_no_failures(temp_db, mock_dependencies):
    """Asserts that _check_and_trigger_mercury returns False when an agent is running or no failures exist."""
    mock_mem, mock_container = mock_dependencies
    
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        
        # 1. Agent currently running: should return False immediately
        engine.current_process = MagicMock()
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is False

        # 2. No running agent, but no pending PRs: should return False
        engine.current_process = None
        engine.state_db.get_pending_prs = MagicMock(return_value=[])
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is False

def test_check_and_trigger_mercury_success_or_pending(temp_db, mock_dependencies):
    """Asserts that _check_and_trigger_mercury returns False when PR checks are successful or pending."""
    mock_mem, mock_container = mock_dependencies
    
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        engine.state_db.get_pending_prs = MagicMock(return_value=[
            {'pr_number': 87, 'branch_name': 'agent/bolt/foo', 'model': 'Gemini 3.5 Flash'}
        ])
        
        # 1. PR checks successful: should return False
        mock_container.check_pr_ci_status.return_value = {'status': 'success', 'run_id': None, 'raw_output': 'all checks passed'}
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is False
        
        # 2. PR checks pending: should return False
        mock_container.check_pr_ci_status.return_value = {'status': 'pending', 'run_id': 12345, 'raw_output': 'checks running'}
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is False

def test_check_and_trigger_mercury_failure_triggers_mercury(temp_db, mock_dependencies):
    """Asserts that _check_and_trigger_mercury triggers Mercury when a PR CI/CD run fails."""
    mock_mem, mock_container = mock_dependencies
    
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        
        # Setup pending PR that failed CI/CD
        engine.state_db.get_pending_prs = MagicMock(return_value=[
            {'pr_number': 87, 'branch_name': 'agent/bolt/foo', 'model': 'Gemini 3.5 Flash'}
        ])
        mock_container.check_pr_ci_status.return_value = {'status': 'failure', 'run_id': 12345, 'raw_output': 'failed verify step'}
        mock_container.get_run_failed_log.return_value = 'failing test: test_db_connection failed'
        
        # Mock container run_agent and database methods
        mock_container.run_agent.return_value = MagicMock()
        engine.state_db.start_run = MagicMock(return_value=42)
        
        # 1. Trigger Mercury (should return True and start Mercury)
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is True
        
        # Verify database state was set to avoid double triggers
        assert engine.state_db.get_state("mercury_healed_run_12345") == "triggered"
        
        # Verify group chat message was added
        engine.memory_db.add_message.assert_called_once()
        args, kwargs = engine.memory_db.add_message.call_args
        assert kwargs['sender_id'] == 'orchestrator'
        assert 'Reliability Engineer' in kwargs['content']
        assert 'PR #87' in kwargs['content']
        
        # Verify run_agent was called for Mercury
        mock_container.run_agent.assert_called_once()
        run_args, run_kwargs = mock_container.run_agent.call_args
        assert run_kwargs['agent_id'] == 'mercury'
        assert run_kwargs['branch_name'] == 'agent/bolt/foo'
        assert run_kwargs['prompt_file'] == 'mercury_temp.md'

        # 2. Running it again for the same run_id: should skip and return False
        mock_container.run_agent.reset_mock()
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is False
        mock_container.run_agent.assert_not_called()

def test_check_and_trigger_mercury_conflict_triggers_rebase(temp_db, mock_dependencies):
    """Asserts that _check_and_trigger_mercury triggers Mercury for a merge conflict."""
    mock_mem, mock_container = mock_dependencies
    
    with patch('orchestrator.config.DB_PATH', temp_db):
        engine = OrchestratorEngine()
        
        # Setup pending PR that has merge conflict
        engine.state_db.get_pending_prs = MagicMock(return_value=[
            {'pr_number': 87, 'branch_name': 'agent/bolt/foo', 'model': 'Gemini 3.5 Flash'}
        ])
        mock_container.check_pr_ci_status.return_value = {
            'status': 'conflict', 
            'run_id': None, 
            'head_sha': 'abc123sha', 
            'raw_output': 'Merge conflict detected'
        }
        
        # Mock container run_agent and database methods
        mock_container.run_agent.return_value = MagicMock()
        engine.state_db.start_run = MagicMock(return_value=43)
        
        # 1. Trigger Mercury (should return True and start Mercury)
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is True
        
        # Verify database state was set using head_sha
        assert engine.state_db.get_state("mercury_healed_conflict_abc123sha") == "triggered"
        
        # Verify group chat message was added
        engine.memory_db.add_message.assert_called_once()
        args, kwargs = engine.memory_db.add_message.call_args
        assert 'Merge conflict' in kwargs['content']
        assert 'PR #87' in kwargs['content']
        
        # Verify run_agent was called for Mercury
        mock_container.run_agent.assert_called_once()
        run_args, run_kwargs = mock_container.run_agent.call_args
        assert run_kwargs['agent_id'] == 'mercury'
        assert run_kwargs['branch_name'] == 'agent/bolt/foo'

        # 2. Running again for the same head_sha: should skip and return False
        mock_container.run_agent.reset_mock()
        assert engine._check_and_trigger_mercury(datetime.now(IST)) is False
        mock_container.run_agent.assert_not_called()

