import pytest
import os
import json
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch
import pytz

from orchestrator.engine import OrchestratorEngine
from orchestrator.memory import MemoryDB
from orchestrator.state import StateDB
import orchestrator.config as config

@pytest.fixture
def temp_dbs():
    # Create temp files for DBs
    state_fd, state_path = tempfile.mkstemp()
    memory_fd, memory_path = tempfile.mkstemp()
    
    # Store old config paths
    old_db_path = config.DB_PATH
    old_mem_path = config.MEMORY_DB_PATH
    
    config.DB_PATH = state_path
    config.MEMORY_DB_PATH = memory_path
    
    yield state_path, memory_path
    
    # Clean up
    os.close(state_fd)
    os.unlink(state_path)
    os.close(memory_fd)
    os.unlink(memory_path)
    
    config.DB_PATH = old_db_path
    config.MEMORY_DB_PATH = old_mem_path

@patch("orchestrator.engine.container.exec_in_container")
@patch("orchestrator.engine.os.environ", {
    "PRIMARY_API_KEY": "test-primary-key",
    "BACKUP_API_KEY_1": "test-backup-key-1"
})
def test_memory_summarization_flow(mock_exec, temp_dbs):
    mock_exec.return_value = (0, "", "")
    
    # Initialize databases
    state_db = StateDB(temp_dbs[0])
    memory_db = MemoryDB(temp_dbs[1])
    
    # Seed memories
    memory_db.add_memory(
        agent_id="Sentinel",
        run_id=1,
        memory_type="observation",
        content="Discovered a repeating infinite loop bug in retry logic.",
        scope="global",
        tags=["bug", "retry"]
    )
    memory_db.add_memory(
        agent_id="Bolt",
        run_id=2,
        memory_type="decision",
        content="Decided to cache API responses to prevent rate limiting.",
        scope="global",
        tags=["cache", "api"]
    )
    
    # Seed one existing active fact
    fact_id = memory_db.add_fact("Older active fact about database schemas", "architecture")
    
    # Initialize Engine
    engine = OrchestratorEngine()
    
    # Mock Gemini Client and API call
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "facts": [
            {
                "id": fact_id,
                "fact": "Updated fact: Older active fact about database schemas and query patterns",
                "category": "architecture"
            },
            {
                "id": None,
                "fact": "Infinite loops in retry logic must be avoided by setting max_retries = 3",
                "category": "bug_pattern"
            }
        ]
    })
    
    mock_client.models.generate_content.return_value = mock_resp
    
    # Run check/run memory summarization
    with patch("google.genai.Client", return_value=mock_client) as mock_genai_client_cls:
        now = datetime.now(pytz.timezone(config.TIMEZONE))
        engine._check_memory_summarization(now)
        
        # Verify generate_content was called
        mock_genai_client_cls.assert_called_once_with(api_key="test-primary-key", http_options={"api_version": "v1beta"})
        mock_client.models.generate_content.assert_called_once()
        
        # Check database updates
        active_facts = memory_db.get_active_facts()
        assert len(active_facts) == 2
        
        # Check that the old fact was updated/superseded
        with memory_db._get_conn() as conn:
            old_fact_row = conn.execute("SELECT * FROM agent_facts WHERE id = ?", (fact_id,)).fetchone()
            assert old_fact_row["archived"] == 1
            assert old_fact_row["superseded_by"] is not None
            
            # The new/updated fact should exist
            new_fact_id = old_fact_row["superseded_by"]
            updated_fact_row = conn.execute("SELECT * FROM agent_facts WHERE id = ?", (new_fact_id,)).fetchone()
            assert updated_fact_row["fact"] == "Updated fact: Older active fact about database schemas and query patterns"
            assert updated_fact_row["archived"] == 0
            
            # The brand new fact should exist
            new_fact_row = conn.execute("SELECT * FROM agent_facts WHERE category = 'bug_pattern'").fetchone()
            assert new_fact_row["fact"] == "Infinite loops in retry logic must be avoided by setting max_retries = 3"
            assert new_fact_row["archived"] == 0
            
        # Check that memories are archived (archived = 1)
        recent_memories = memory_db.get_recent_memories()
        assert len(recent_memories) == 0
        
        with memory_db._get_conn() as conn:
            archived_memories = conn.execute("SELECT COUNT(*) FROM agent_memories WHERE archived = 1").fetchone()[0]
            assert archived_memories == 2
            
        # Check that last_memory_summarization_time state is updated
        stored_time = state_db.get_state("last_memory_summarization_time")
        assert stored_time == now.isoformat()
