import os
import json
import pytest
from unittest.mock import patch, MagicMock, mock_open
import compile_and_mail_prompts

# Tests verify behavior of compiling agent prompts and emailing them.

def test_compile_prompts_success():
    """Asserts that compile_prompts correctly compiles agent prompts and reviewer JSON."""
    fake_agents = {
        os.path.join(compile_and_mail_prompts.AGENTS_DIR, "bolt.txt"): "Bolt prompt content",
        os.path.join(compile_and_mail_prompts.AGENTS_DIR, "sentinel.txt"): "Sentinel prompt content",
        os.path.join(compile_and_mail_prompts.AGENTS_DIR, "palette.txt"): "Palette prompt content",
        os.path.join(compile_and_mail_prompts.AGENTS_DIR, "newton.txt"): "Newton prompt content",
        os.path.join(compile_and_mail_prompts.AGENTS_DIR, "lucios.txt"): "Lucios prompt content",
        os.path.join(compile_and_mail_prompts.AGENTS_DIR, "proton.txt"): "Proton prompt content",
    }
    
    fake_json_data = {
        "customAgentSpec": {
            "customAgent": {
                "systemPromptSections": [
                    {"title": "Section 1", "content": "Reviewer system prompt content"}
                ],
                "toolNames": ["tool_a", "tool_b"]
            }
        }
    }

    def exists_side_effect(path):
        if path in fake_agents:
            return True
        if path == compile_and_mail_prompts.REVIEWER_PATH:
            return True
        return False

    def open_side_effect(path, mode="r", *args, **kwargs):
        if path in fake_agents:
            return mock_open(read_data=fake_agents[path])(path, mode)
        if path == compile_and_mail_prompts.REVIEWER_PATH:
            return mock_open(read_data=json.dumps(fake_json_data))(path, mode)
        raise FileNotFoundError(f"Mock file not found: {path}")

    with patch("os.path.exists", side_effect=exists_side_effect), \
         patch("builtins.open", side_effect=open_side_effect):
        
        result = compile_and_mail_prompts.compile_prompts()
        
        assert "# Stellar Autonomous Agent System — Complete Prompt Registry" in result
        assert "Bolt prompt content" in result
        assert "Reviewer system prompt content" in result
        assert "`tool_a`" in result
        assert "`tool_b`" in result


def test_compile_prompts_missing_files():
    """Asserts that compile_prompts handles missing files by putting warning messages."""
    def exists_side_effect(path):
        return False

    with patch("os.path.exists", side_effect=exists_side_effect):
        result = compile_and_mail_prompts.compile_prompts()
        
        assert "⚠️ **FILE NOT FOUND:** `agents/bolt.txt`" in result
        assert "⚠️ **FILE NOT FOUND:** `code-reviewer.json`" in result


@patch("smtplib.SMTP_SSL")
def test_send_email_success(mock_smtp_class):
    """Asserts that send_email successfully sends email, saves output file and logs success."""
    mock_smtp_inst = MagicMock()
    mock_smtp_class.return_value.__enter__.return_value = mock_smtp_inst

    body_md = "## Test Document"
    
    with patch("builtins.open", mock_open(read_data=b"## Test Document")) as mocked_file, \
         patch("os.makedirs") as mock_makedirs:
        
        compile_and_mail_prompts.send_email(body_md)
        
        # Verify file is saved
        mocked_file.assert_called()
        # Verify SMTP actions
        mock_smtp_class.assert_called_once_with("smtp.gmail.com", 465)
        mock_smtp_inst.login.assert_called_once_with(
            compile_and_mail_prompts.SENDER, 
            compile_and_mail_prompts.SENDER_PASS
        )
        mock_smtp_inst.send_message.assert_called_once()

