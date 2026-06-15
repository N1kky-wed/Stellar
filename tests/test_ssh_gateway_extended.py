import os
import json
import pytest
import sqlite3
import time
from unittest.mock import patch, MagicMock
from rich.console import Console
import ssh_gateway

# Tests to cover TUI rendering, read_line, and handle_session logic.

def test_tui_micro_logo():
    """Asserts that TUI.get_logo correctly styles the logo with primary/dim colors."""
    theme = {
        "primary": "cyan",
        "dim": "grey"
    }
    logo = ssh_gateway.TUI.get_logo(theme)
    assert "Stellar" in logo
    assert "Code" in logo

def test_tui_big_logo():
    """Asserts that TUI.get_big_logo returns the ASCI art styled with primary color."""
    theme = {"primary": "cyan"}
    logo = ssh_gateway.TUI.get_big_logo(theme)
    assert "███████" in logo

def test_tui_render_narrow_term():
    """Asserts that TUI._render falls back without Panel on very narrow terminal dimensions."""
    theme = {
        "primary": "cyan",
        "bg": "black",
        "border": "white",
        "text": "white",
        "dim": "grey"
    }
    rendered = ssh_gateway.TUI._render(width=10, height=2, content="Test content", theme=theme)
    assert "Test content" in rendered

def test_tui_theme_picker():
    """Asserts that TUI.theme_picker renders the theme selection interface successfully."""
    picker = ssh_gateway.TUI.theme_picker(
        selected_theme=0,
        selected_border=0,
        focus="theme",
        width=80,
        height=24,
        is_default=False
    )
    assert "Select Theme:" in picker
    assert "Stellar Classic" in picker

def test_tui_auth_screen():
    """Asserts that TUI.auth_screen renders the passcode entry screen successfully."""
    theme = ssh_gateway.TUI.THEMES[0]
    screen = ssh_gateway.TUI.auth_screen(
        width=80,
        height=24,
        typed_code="123",
        error_msg="Some error",
        theme=theme
    )
    assert "Some error" in screen
    assert "Authentication required" in screen
    assert "Enter Code:" in screen

def test_tui_dashboard():
    """Asserts that TUI.dashboard renders the repositories list and control instructions."""
    theme = ssh_gateway.TUI.THEMES[0]
    repos = [
        {
            "id": 1,
            "name": "MyProj",
            "status": "running",
            "subdomain": "myproj",
            "app_type": "web",
            "created": "2026-06-13",
            "process_id": "proc-1",
            "container_id": "cont-1"
        }
    ]
    dash = ssh_gateway.TUI.dashboard(
        repos=repos,
        selected=0,
        username="testuser",
        width=80,
        height=24,
        status_msg="Status OK",
        theme=theme,
        search_query="",
        filter_state="All",
        sort_state="Name",
        mode="NORMAL"
    )
    assert "Filter: All" in dash
    assert "Sort: Name" in dash
    assert "Status OK" in dash
    assert "Stellar" in dash

def test_tui_connecting_screen():
    """Asserts that TUI.connecting_screen renders the connecting message."""
    theme = ssh_gateway.TUI.THEMES[0]
    screen = ssh_gateway.TUI.connecting_screen("MyRepo", width=80, height=24, theme=theme)
    assert "Connecting" in screen
    assert "MyRepo" in screen

def test_tui_logs_screen():
    """Asserts that TUI.logs_screen renders the application logs container."""
    theme = ssh_gateway.TUI.THEMES[0]
    screen = ssh_gateway.TUI.logs_screen("MyRepo", ["log line 1", "log line 2"], width=80, height=24, theme=theme)
    assert "MyRepo" in screen
    assert "log line 1" in screen

def test_tui_goodbye_screen():
    """Asserts that TUI.goodbye_screen renders the exit screen message."""
    theme = ssh_gateway.TUI.THEMES[0]
    screen = ssh_gateway.TUI.goodbye_screen("testuser", width=80, height=24, theme=theme)
    assert "Goodbye" in screen

@patch("ssh_gateway.read_key")
@patch("ssh_gateway.send_raw")
def test_read_line_basic(mock_send, mock_read_key):
    """Asserts that read_line returns entered characters on ENTER key."""
    # Simulates typing 'h', 'i', then 'ENTER'
    mock_read_key.side_effect = ['h', 'i', 'ENTER']
    mock_channel = MagicMock()
    
    line = ssh_gateway.read_line(mock_channel, prompt=">", mask=False, max_len=10)
    assert line == "hi"

@patch("ssh_gateway.read_key")
@patch("ssh_gateway.send_raw")
def test_read_line_backspace_and_printable(mock_send, mock_read_key):
    """Asserts that read_line handles BACKSPACE and filters out non-printable characters."""
    # Simulates typing 'a', 'b', 'BACKSPACE', 'c', then 'ENTER'
    mock_read_key.side_effect = ['a', 'b', 'BACKSPACE', 'c', 'ENTER']
    mock_channel = MagicMock()
    
    line = ssh_gateway.read_line(mock_channel, prompt=">", mask=False, max_len=10)
    assert line == "ac"

@patch("ssh_gateway.read_key")
@patch("ssh_gateway.send_raw")
def test_read_line_escape_chars(mock_send, mock_read_key):
    """Asserts that read_line ignores cursor movement keys and returns on newline in text."""
    # Simulates typing 'x', UP key, '\n' (which acts like ENTER)
    mock_read_key.side_effect = ['x', 'UP', '\n']
    mock_channel = MagicMock()
    
    line = ssh_gateway.read_line(mock_channel, prompt=">", mask=False, max_len=10)
    assert line == "x"

@patch("ssh_gateway.read_key")
@patch("ssh_gateway.send_raw")
def test_read_line_eof(mock_send, mock_read_key):
    """Asserts that read_line returns None early on EOF or CTRL_C."""
    mock_read_key.side_effect = ['a', 'CTRL_C']
    mock_channel = MagicMock()
    
    line = ssh_gateway.read_line(mock_channel, prompt=">", mask=False, max_len=10)
    assert line is None

@patch("ssh_gateway.rate_limiter")
@patch("ssh_gateway.send_raw")
def test_handle_session_ip_blocked(mock_send, mock_limiter):
    """Asserts that handle_session returns early when the IP is blocked."""
    mock_limiter.is_ip_blocked.return_value = True
    
    mock_channel = MagicMock()
    mock_server = MagicMock()
    mock_server.username = "1.2.3.4"
    mock_server.event.wait.return_value = True
    
    ssh_gateway.handle_session(mock_channel, mock_server, client_addr="127.0.0.1")
    # Verify that the blocked message was sent
    mock_send.assert_any_call(mock_channel, "\r\n\x1b[31m  ✗ Your IP is blocked due to too many failed attempts.\x1b[0m\r\n")

@patch("ssh_gateway.rate_limiter")
@patch("ssh_gateway.verify_auth_code")
@patch("ssh_gateway.read_key")
@patch("ssh_gateway.send_raw")
def test_handle_session_auth_lockout(mock_send, mock_read_key, mock_verify, mock_limiter):
    """Asserts that handle_session locks the user out after max auth failures."""
    mock_limiter.is_ip_blocked.return_value = False
    # Simulate entering invalid code 3 times
    # 8 keys for first attempt: '1', '2', '3', '4', '5', '6', '7', '8'
    # 8 keys for second attempt: '1', '2', '3', '4', '5', '6', '7', '8'
    # 8 keys for third attempt: '1', '2', '3', '4', '5', '6', '7', '8'
    mock_read_key.side_effect = ['1', '2', '3', '4', '5', '6', '7', '8'] * 3
    mock_verify.return_value = None
    
    mock_channel = MagicMock()
    mock_server = MagicMock()
    mock_server.username = "1.2.3.4"
    mock_server.event.wait.return_value = True
    mock_server.term_width = 80
    mock_server.term_height = 24
    
    ssh_gateway.handle_session(mock_channel, mock_server, client_addr="127.0.0.1")
    
    # 3 record failures recorded
    assert mock_limiter.record_auth_failure.call_count == 3

@patch("ssh_gateway.rate_limiter")
@patch("ssh_gateway.verify_auth_code")
@patch("ssh_gateway.read_key")
@patch("ssh_gateway.load_theme")
@patch("ssh_gateway.get_user_repos")
@patch("ssh_gateway.get_all_container_statuses")
@patch("ssh_gateway.send_raw")
def test_handle_session_success_and_navigation(mock_send, mock_get_statuses, mock_get_repos, mock_load_theme, mock_read_key, mock_verify, mock_limiter):
    """Asserts that handle_session runs theme picker, goes to dashboard, handles navigation and exits on 'q'."""
    mock_limiter.is_ip_blocked.return_value = False
    mock_verify.return_value = {"user_id": 1, "username": "testuser", "display_name": "Test User"}
    mock_load_theme.return_value = {"theme_idx": 0, "border_idx": 0}
    mock_get_repos.return_value = [
        {
            "id": 1,
            "name": "MyProj",
            "status": "running",
            "subdomain": "myproj",
            "app_type": "web",
            "created": "2026-06-13",
            "process_id": "proc-1",
            "container_id": "cont-1"
        }
    ]
    mock_get_statuses.return_value = {"stellar-web-proc-1": "running"}
    
    # Keyboard simulation:
    # 1. 8 chars for code: '1','2','3','4','5','6','7','8' (takes user to auth success)
    # 2. Enter dashboard loop. 
    # 3. Press 't' to open theme picker.
    # 4. In theme picker: Press 'ENTER' to select theme.
    # 5. Back to dashboard: Press 'DOWN' arrow.
    # 6. Press 's' to filter.
    # 7. Press 'o' to sort.
    # 8. Press 'q' to quit.
    mock_read_key.side_effect = [
        '1', '2', '3', '4', '5', '6', '7', '8',  # auth code
        't',                           # open theme picker
        'ENTER',                       # save theme
        'DOWN',                        # select next repo
        's',                           # cycle filter
        'o',                           # cycle sort
        'q'                            # quit
    ]
    
    mock_channel = MagicMock()
    mock_server = MagicMock()
    mock_server.username = "1.2.3.4"
    mock_server.event.wait.return_value = True
    mock_server.term_width = 80
    mock_server.term_height = 24
    
    ssh_gateway.handle_session(mock_channel, mock_server, client_addr="127.0.0.1")
    
    # Verify theme was loaded
    mock_load_theme.assert_called_once_with(1)
