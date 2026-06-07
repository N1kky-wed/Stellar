import os
import json
import pytest
import sqlite3
from unittest.mock import MagicMock, patch
from flask import g

import agent_tools

def test_request_user_interaction():
    res = agent_tools.request_user_interaction(html_ui="<div></div>", goal="fill", status="pause", timeout=5)
    assert res is None

@patch('agent_tools.TavilyClient')
def test_web_search_tavily(mock_tavily):
    mock_t_inst = MagicMock()
    mock_t_inst.search.return_value = {
        'results': [{'url': 'http://example.com', 'content': 'Sample content'}],
        'answer': 'Sample answer'
    }
    mock_tavily.return_value = mock_t_inst

    res = agent_tools.web_search(
        action='tavily_search',
        query='test query',
        status='Searching...',
        timeout=10
    )
    assert 'Sample content' in res or 'Sample answer' in res

@patch('smtplib.SMTP_SSL')
def test_send_self_email(mock_smtp, auth_client):
    mock_smtp_inst = MagicMock()
    mock_smtp.return_value.__enter__.return_value = mock_smtp_inst

    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        res = agent_tools.send_self_email(
            subject="Test Alert",
            body="This is a test email body",
            status="Sending email",
            timeout=10
        )
        assert "Success" in res

def test_schedule_task(auth_client):
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1

        # Test schedule
        res = agent_tools.schedule_task(
            task_prompt="Run background audit",
            status="Scheduling...",
            timeout=10,
            action="schedule"
        )
        assert "Task scheduled" in res

        # Test list
        res_list = agent_tools.schedule_task(
            task_prompt="",
            status="Listing...",
            timeout=10,
            action="list"
        )
        assert "Run background audit" in res_list

@patch('agent_tools.genai.Client')
def test_generate_image(mock_client, auth_client):
    mock_response = MagicMock()
    mock_part = MagicMock()
    mock_part.inline_data.data = b"image_data_bytes"
    mock_part.inline_data.mime_type = "image/png"
    mock_response.candidates[0].content.parts = [mock_part]
    
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.return_value = mock_response
    mock_client.return_value = mock_client_inst

    with auth_client.application.app_context():
        res = agent_tools.generate_image(
            model='gemini-3.1-flash-image-preview',
            prompt='a beautiful landscape painting',
            status='Generating image...',
            timeout=10
        )
        assert 'Generated Image' in res

def test_logs_and_preferences(auth_client):
    res = agent_tools.logs_and_preferences(
        status="Saving pref",
        timeout=10,
        write="Set model temp to 0.7",
        user_id="test_user"
    )
    assert "Feedback successfully" in res or "Developer" in res or "stored" in res or "Error" not in res

@patch('agent_tools.requests.get')
@patch('agent_tools.genai.Client')
def test_make_presentation(mock_client, mock_get):
    mock_response = MagicMock()
    mock_response.text = '{"slides": [{"title": "Intro to ML", "summary": "Detailed info", "background_description": "Clean layout"}]}'
    
    mock_client_inst = MagicMock()
    
    def generate_content_side_effect(model, **kwargs):
        if model == 'gemini-2.5-flash':
            return mock_response
        else:
            import base64
            mock_img_resp = MagicMock()
            mock_part = MagicMock()
            mock_part.inline_data.data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
            mock_part.inline_data.mime_type = "image/png"
            mock_img_resp.candidates = [MagicMock(content=MagicMock(parts=[mock_part]))]
            return mock_img_resp
            
    mock_client_inst.models.generate_content.side_effect = generate_content_side_effect
    mock_client.return_value = mock_client_inst

    mock_resp_img = MagicMock()
    mock_resp_img.content = b"image_content"
    mock_get.return_value = mock_resp_img

    res = agent_tools.make_presentation(
        topic="Introduction to Machine Learning",
        status="Creating pptx...",
        timeout=10,
        num_slides=3
    )
    assert "Presentation created" in res or "ML" in res or ".pptx" in res

def test_obtain_talent(auth_client):
    # Get the temp database path from the app context
    from app import get_db
    db_path = None
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT OR IGNORE INTO talents (talent_name, mandate_text) VALUES ('design', 'Frontend Design Guidelines')")
        db.commit()
        # Find database connection filename
        cursor = db.execute("PRAGMA database_list")
        db_path = cursor.fetchone()[2]

    # Patch sqlite3.connect inside obtain_talent safely to avoid infinite recursion
    orig_connect = sqlite3.connect
    with patch('sqlite3.connect', side_effect=lambda path: orig_connect(db_path)):
        res = agent_tools.obtain_talent(
            talent_names=['design'],
            status="Loading talent",
            timeout=10
        )
        assert "TALENT ACQUIRED" in res
        assert "Frontend Design Guidelines" in res

def test_read_tool_output(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT INTO tool_calls (chat_id, tool_name, input_params, result) VALUES (1, 'test_tool', '{}', 'Line1: output\nLine2: output\nLine3: target')")
        db.commit()
        last_id = db.execute("SELECT id FROM tool_calls ORDER BY id DESC LIMIT 1").fetchone()[0]

    res = agent_tools.read_tool_output(
        output_id=last_id,
        status="Reading...",
        timeout=10,
        keyword="target"
    )
    assert "Line 2: Line3: target" in res

@patch('subprocess.Popen')
def test_report_process_issue(mock_popen, auth_client):
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1

        res = agent_tools.report_process_issue(
            topic="SIGKILL error",
            issue_description="Container ran out of memory",
            technical_context="Exit code 137",
            status="Reporting...",
            timeout=10
        )
        assert "Feedback successfully reported" in res

def test_compress_memory(auth_client):
    from app import get_db
    with auth_client.application.app_context():
        from flask import g
        g.chat_id = 1
        db = get_db()
        db.execute("INSERT INTO messages (chat_id, message_type, message_content) VALUES (1, 'user', 'message 1')")
        db.execute("INSERT INTO messages (chat_id, message_type, message_content) VALUES (1, 'stellar', 'message 2')")
        db.commit()

        res = agent_tools.compress_memory(
            target="both",
            state_document="Structured state containing current objectives, tasks, and issues. Long enough to pass validation.",
            status="Compressing...",
            timeout=10
        )
        assert "Memory compressed" in res or "compression" in res or "Error" not in res
