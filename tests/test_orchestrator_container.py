# test_orchestrator_container.py
import pytest
import subprocess
from unittest.mock import MagicMock, patch
import orchestrator.container as container

def test_is_container_running():
    """Asserts that is_container_running correctly parses docker inspect response and handles errors."""
    with patch("subprocess.run") as mock_run:
        mock_res = MagicMock()
        mock_res.stdout = "true\n"
        mock_run.return_value = mock_res
        assert container.is_container_running() is True
        mock_run.assert_called_once()
        
    with patch("subprocess.run") as mock_run:
        mock_res = MagicMock()
        mock_res.stdout = "false\n"
        mock_run.return_value = mock_res
        assert container.is_container_running() is False
        
    with patch("subprocess.run", side_effect=Exception("Failed command")):
        assert container.is_container_running() is False

def test_exec_in_container():
    """Asserts that exec_in_container correctly handles process execution, timeouts, and exceptions."""
    with patch("subprocess.run") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "out"
        mock_res.stderr = "err"
        mock_run.return_value = mock_res
        rc, stdout, stderr = container.exec_in_container("echo hi", timeout=10)
        assert rc == 0
        assert stdout == "out"
        assert stderr == "err"
        
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 10)):
        rc, stdout, stderr = container.exec_in_container("echo hi", timeout=10)
        assert rc == -1
        assert stdout == ""
        assert stderr == "TimeoutExpired"
        
    with patch("subprocess.run", side_effect=ValueError("Some value error")):
        rc, stdout, stderr = container.exec_in_container("echo hi", timeout=10)
        assert rc == -1
        assert stdout == ""
        assert stderr == str(ValueError("Some value error"))

def test_copy_and_remove():
    """Asserts copy_to_container and remove_from_container invoke subprocess.run with correct CLI commands."""
    with patch("subprocess.run") as mock_run:
        container.copy_to_container("host/path", "container/path")
        assert mock_run.call_count == 2
        
    with patch("subprocess.run") as mock_run:
        container.remove_from_container("container/path")
        mock_run.assert_called_once()

def test_load_agent_prompt():
    """Asserts load_agent_prompt loads instructions and code-reviewer plugin configuration into the container."""
    with patch("os.path.exists", return_value=False):
        with pytest.raises(FileNotFoundError):
            container.load_agent_prompt("agent1", "prompt1.md")
            
    with patch("os.path.exists", return_value=True), \
         patch("subprocess.run") as mock_run, \
         patch("orchestrator.container.exec_in_container", return_value=(0, "success", "")):
         
        container.load_agent_prompt("agent1", "prompt1.md")
        assert mock_run.call_count > 0

def test_unload_agent_prompt():
    """Asserts unload_agent_prompt removes temporary files, uninstalls plugins, and kills leftovers inside container."""
    with patch("subprocess.run") as mock_run, \
         patch("orchestrator.container.exec_in_container", return_value=(0, "success", "")):
        container.unload_agent_prompt()
        assert mock_run.call_count > 0

def test_copy_memory_context():
    """Asserts copy_memory_context_to_container issues the correct docker cp/exec command sequence."""
    with patch("subprocess.run") as mock_run:
        container.copy_memory_context_to_container("host/path")
        assert mock_run.call_count > 0

def test_read_memory_outbox():
    """Asserts read_memory_outbox_from_container checks file existence and copies it from container to host."""
    with patch("orchestrator.container.exec_in_container", return_value=(1, "", "")):
        assert container.read_memory_outbox_from_container("host/path") is False
        
    with patch("orchestrator.container.exec_in_container", return_value=(0, "", "")), \
         patch("subprocess.run") as mock_run:
        assert container.read_memory_outbox_from_container("host/path") is True
        mock_run.assert_called_once()
        
    with patch("orchestrator.container.exec_in_container", return_value=(0, "", "")), \
         patch("subprocess.run", side_effect=Exception("copy error")):
        assert container.read_memory_outbox_from_container("host/path") is False

def test_restart_container():
    """Asserts restart_container restarts docker container, falling back to start on failure."""
    with patch("subprocess.run") as mock_run:
        container.restart_container()
        mock_run.assert_called_once()
        
    with patch("subprocess.run", side_effect=[Exception("restart failed"), MagicMock()]) as mock_run:
        container.restart_container()
        assert mock_run.call_count == 2

def test_run_agent():
    """Asserts run_agent restarts the container, loads instructions, runs git commands, and spawns the background Popen process."""
    with patch("orchestrator.container.restart_container") as mock_restart, \
         patch("orchestrator.container.load_agent_prompt") as mock_load, \
         patch("orchestrator.container.exec_in_container", return_value=(0, "", "")), \
         patch("subprocess.Popen") as mock_popen:
         
        container.run_agent("agent_id", "prompt_file", "branch_name")
        mock_restart.assert_called_once()
        mock_load.assert_called_once()
        mock_popen.assert_called_once()

def test_check_new_prs():
    """Asserts check_new_prs queries PR list and parses JSON output correctly, handling exceptions."""
    with patch("orchestrator.container.exec_in_container", return_value=(0, '[{"number": 1, "url": "url"}]', "")):
        prs = container.check_new_prs("branch")
        assert len(prs) == 1
        assert prs[0]["number"] == 1
        
    with patch("orchestrator.container.exec_in_container", return_value=(0, "invalid json", "")):
        prs = container.check_new_prs("branch")
        assert prs == []
        
    with patch("orchestrator.container.exec_in_container", return_value=(1, "", "gh command failed")):
        prs = container.check_new_prs("branch")
        assert prs == []

def test_check_pr_status():
    """Asserts check_pr_status queries single PR status, defaulting to OPEN on error."""
    with patch("orchestrator.container.exec_in_container", return_value=(0, '{"state": "MERGED"}', "")):
        state = container.check_pr_status(1)
        assert state == "MERGED"
        
    with patch("orchestrator.container.exec_in_container", return_value=(0, 'invalid json', "")):
        state = container.check_pr_status(1)
        assert state == "OPEN"
        
    with patch("orchestrator.container.exec_in_container", return_value=(1, '', "error")):
        state = container.check_pr_status(1)
        assert state == "OPEN"

def test_get_agent_final_summary():
    """Asserts get_agent_final_summary finds the latest transcript file and extracts the last completed PLANNER_RESPONSE."""
    with patch("orchestrator.container.exec_in_container", return_value=(1, "", "")):
        assert container.get_agent_final_summary() is None
        
    with patch("orchestrator.container.exec_in_container", side_effect=[(0, "/path/to/transcript.jsonl", ""), (1, "", "")]):
        assert container.get_agent_final_summary() is None
        
    jsonl_content = (
        '{"type": "USER_INPUT", "content": "hello"}\n'
        '{"type": "PLANNER_RESPONSE", "status": "RUNNING", "content": "running summary"}\n'
        '{"type": "PLANNER_RESPONSE", "status": "DONE", "content": "final completion summary"}\n'
        '{"type": "SYSTEM", "content": "finished"}\n'
    )
    with patch("orchestrator.container.exec_in_container", side_effect=[(0, "/path/to/transcript.jsonl", ""), (0, jsonl_content, "")]):
        summary = container.get_agent_final_summary()
        assert summary == "final completion summary"
