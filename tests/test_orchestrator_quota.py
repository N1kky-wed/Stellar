# test_orchestrator_quota.py
import pytest
from unittest.mock import MagicMock, patch
import orchestrator.quota as quota

# Brief comment: Asserts that parse_quota_text correctly parses Gemini and Claude weekly/sprint percentages and refresh times under normal circumstances.
def test_parse_quota_text_normal_parsing_success():
    raw_output = """
    Account: testagent@stellarai.site
    
    GEMINI MODELS:
    Weekly Limit
    [====================] 100.0%
    Refreshes in 12h 30m
    
    Five Hour Limit
    [====================] 85.5%
    Refreshes in 2h 15m
    
    CLAUDE AND GPT MODELS:
    Weekly Limit
    [==========          ] 50.0%
    Refreshes in 4h 0m
    
    Five Hour Limit
    Disabled: Currently not active.
    """
    
    parsed = quota.parse_quota_text(raw_output)
    
    # Verify Gemini parsing
    gemini = parsed["gemini"]
    assert gemini["account"] == "testagent@stellarai.site"
    assert gemini["weekly_percent"] == 100.0
    assert gemini["weekly_refreshes_in_hours"] == 12.5
    assert gemini["sprint_percent"] == 85.5
    assert gemini["sprint_refreshes_in_hours"] == 2.25
    assert gemini["sprint_disabled"] is False
    assert gemini["status"] == "Healthy"
    assert gemini["error"] is None
    
    # Verify Claude parsing
    claude = parsed["claude"]
    assert claude["account"] == "testagent@stellarai.site"
    assert claude["weekly_percent"] == 50.0
    assert claude["weekly_refreshes_in_hours"] == 4.0
    assert claude["sprint_percent"] == 0.0
    assert claude["sprint_disabled"] is True
    assert claude["status"] == "Sprint Disabled"
    assert claude["error"] is None

# Brief comment: Asserts that parse_quota_text handles empty or completely invalid input by recovering with default values and marking status as Healthy/default.
def test_parse_quota_text_empty_input_defaults():
    parsed = quota.parse_quota_text("")
    assert parsed["gemini"]["account"] == "Unknown"
    assert parsed["gemini"]["weekly_percent"] == 100.0
    assert parsed["gemini"]["status"] == "Healthy"
    assert parsed["claude"]["account"] == "Unknown"
    assert parsed["claude"]["weekly_percent"] == 100.0

# Brief comment: Asserts that parse_quota_text handles exceptions internally, logs the error, and returns structured error details.
def test_parse_quota_text_exception_logged_as_error():
    # Force a TypeError by passing a non-string object that regex functions fail on
    parsed = quota.parse_quota_text(None)
    assert parsed["gemini"]["error"] is not None
    assert parsed["gemini"]["status"] == "Error"
    assert parsed["claude"]["error"] is not None
    assert parsed["claude"]["status"] == "Error"

# Brief comment: Asserts that parse_quota_text handles exhausted weekly limits and marks the status as Exhausted.
def test_parse_quota_text_exhausted_weekly_status():
    raw_output = """
    Account: testagent@stellarai.site
    
    GEMINI MODELS:
    Weekly Limit
    [                    ] 0.0%
    Refreshes in 1h
    """
    parsed = quota.parse_quota_text(raw_output)
    assert parsed["gemini"]["status"] == "Exhausted"

# Brief comment: Asserts that parse_quota_text handles sprint exhausted state (sprint limit < 10% and refresh > 0) and sets status to Sprint Exhausted.
def test_parse_quota_text_sprint_exhausted_status():
    raw_output = """
    Account: testagent@stellarai.site
    
    GEMINI MODELS:
    Weekly Limit
    [====================] 100.0%
    Refreshes in 0h
    
    Five Hour Limit
    [                    ] 5.0%
    Refreshes in 1h 30m
    """
    parsed = quota.parse_quota_text(raw_output)
    assert parsed["gemini"]["status"] == "Sprint Exhausted"

# Brief comment: Asserts that fetch_quota_data_from_container spawns pexpect with the correct docker exec command and performs interactive reading.
def test_fetch_quota_data_from_container_success():
    with patch("pexpect.spawn") as mock_spawn:
        mock_child = MagicMock()
        mock_spawn.return_value = mock_child
        
        # Mock successive read_nonblocking outputs to simulate CLI response
        mock_child.read_nonblocking.side_effect = [
            "Models & Quota data line 1\n",
            "Models & Quota data line 2\n",
            b"" # Triggers EOF or empty return to stop the read loop
        ]
        
        result = quota.fetch_quota_data_from_container(model="TestModel")
        
        # Verify the docker command passed to pexpect
        called_cmd = mock_spawn.call_args[0][0]
        assert "stellar-persistent" in called_cmd
        assert "agy" in called_cmd
        assert "TestModel" in called_cmd
        
        # Verify interactive interactions were called
        mock_child.expect.assert_any_call(r'\? for shortcuts', timeout=60)
        mock_child.send.assert_any_call('/usage\r')
        mock_child.expect.assert_any_call(r'Models & Quota', timeout=30)
        mock_child.close.assert_called_once()

# Brief comment: Asserts that fetch_quota_data_from_container handles errors inside the interactive expect/reading loop, catching them and returning them formatted.
def test_fetch_quota_data_from_container_loop_failure():
    with patch("pexpect.spawn") as mock_spawn:
        mock_child = MagicMock()
        mock_spawn.return_value = mock_child
        mock_child.expect.side_effect = Exception("Interactive timeout")
        
        result = quota.fetch_quota_data_from_container()
        assert "ERROR: Interactive timeout" in result
        mock_child.close.assert_called_once()

# Brief comment: Asserts that fetch_quota_data_from_container propagates exceptions raised during spawn (e.g. Docker daemon unreachable).
def test_fetch_quota_data_from_container_spawn_failure_propagates():
    with patch("pexpect.spawn", side_effect=Exception("Docker daemon down")):
        with pytest.raises(Exception) as excinfo:
            quota.fetch_quota_data_from_container()
        assert "Docker daemon down" in str(excinfo.value)

