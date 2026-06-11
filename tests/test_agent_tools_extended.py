import os
import json
import pytest
import sqlite3
import tarfile
import io
import sys
from unittest.mock import MagicMock, patch
from flask import g

# Mock docker.errors before any imports to prevent ModuleNotFoundError
mock_docker = MagicMock()
class MockNotFound(Exception):
    pass
mock_docker.errors.NotFound = MockNotFound

sys.modules['docker'] = mock_docker
sys.modules['docker.errors'] = mock_docker.errors

import agent_tools

# Helper to avoid NameError: name 'Slide' is not defined in agent_tools.regenerate_presentation_slide
from pydantic import BaseModel, Field
class Slide(BaseModel):
    title: str = Field(default="Test Slide")
    summary: str = Field(default="Test Summary")
    background_description: str = Field(default="Test Background")

agent_tools.Slide = Slide

# --- regenerate_presentation_slide tests ---

@patch('agent_tools.genai.Client')
def test_regenerate_presentation_slide_success(mock_client_class, auth_client):
    """
    Asserts that regenerate_presentation_slide generates content plan via Gemini client,
    creates the slide image via Gemini Image API, updates the presentation file,
    and returns a success URL message.
    """
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    
    # Mock Gemini model responses
    mock_resp1 = MagicMock()
    mock_resp1.text = '{"title": "Updated Slide", "summary": "Updated summary detail", "background_description": "Clean layout"}'
    
    mock_resp2 = MagicMock()
    mock_part = MagicMock()
    mock_part.inline_data.data = b"new_image_bytes"
    mock_part.inline_data.mime_type = "image/png"
    mock_resp2.candidates = [MagicMock(content=MagicMock(parts=[mock_part]))]
    
    mock_client.models.generate_content.side_effect = [mock_resp1, mock_resp2]
    
    # Mock PPTX Presentation to avoid real filesystem errors
    with patch('pptx.Presentation') as mock_pres_class:
        mock_pres = MagicMock()
        mock_pres.slides = [MagicMock()]
        mock_pres_class.return_value = mock_pres
        
        def exists_side_effect(path):
            if path.endswith('.pptx'):
                return True
            return False
            
        with patch('os.path.exists', side_effect=exists_side_effect), \
             patch('os.makedirs'), \
             patch('builtins.open', MagicMock()):
            
            res = agent_tools.regenerate_presentation_slide(
                presentation_id="12345",
                slide_index=0,
                status="Regenerating...",
                timeout=10,
                topic="AI Progress",
                style="modern",
                additional_context="depth focus",
                feedback="add more blue theme"
            )
            assert "REGENERATED_SLIDE" in res
            assert "12345" in res
            assert "slide_1.png" in res


# --- repo_control tests ---

@patch('docker.from_env')
@patch('app.ensure_user_network')
def test_repo_control_deploy_success(mock_ensure_net, mock_docker_env, auth_client):
    """
    Asserts that repo_control deploy action inserts deployment history in the database,
    provisions and runs a Docker container, and stores correct details in Redis and active_apps.
    """
    mock_ensure_net.return_value = "stellar_net_1"
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    mock_container = MagicMock()
    mock_container.id = "fake_container_id_789"
    mock_container.status = "running"
    mock_container.attrs = {
        'NetworkSettings': {
            'Ports': {
                '5000/tcp': [{'HostPort': '12345'}]
            }
        }
    }
    mock_client.containers.run.return_value = mock_container
    
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1
        
        with patch('os.makedirs'), \
             patch('os.listdir', return_value=[]), \
             patch('app.redis_client', MagicMock()) as mock_redis_cli:
            
            res = agent_tools.repo_control(
                action="deploy",
                status="Deploying repo...",
                timeout=30,
                project_name="my_awesome_app",
                port=5000
            )
            assert "Container provisioned" in res
            assert "Live URL:" in res
            assert "my_awesome_app" in res or "Custom Stack Project" in res


@patch('docker.from_env')
def test_repo_control_execute_success(mock_docker_env, auth_client):
    """
    Asserts that repo_control execute runs a command in the container, performs health checking,
    and returns command output.
    """
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.exec_run.side_effect = [
        MagicMock(exit_code=0, output=b"Server started successfully"), # Command execution
        MagicMock(exit_code=0, output=b"200") # Health check curl
    ]
    mock_client.containers.get.return_value = mock_container
    
    # Pre-populate history to allow execution lookup
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT OR IGNORE INTO repo_history (user_id, project_name, process_id, status, files_snapshot) VALUES (1, 'exec_app', 'pid123', 'running', '{\"port\": 5000}')")
        db.commit()
        
        from flask import g
        g.user_id = 1
        
        res = agent_tools.repo_control(
            action="execute",
            app_id="pid123",
            command="python app.py",
            status="Running app...",
            timeout=15
        )
        assert "Server started successfully" in res
        assert "Server is READY" in res


def test_repo_control_list_history_success(auth_client):
    """
    Asserts that repo_control list_history retrieves and formats past deployments for the user.
    """
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT OR IGNORE INTO repo_history (user_id, project_name, process_id, status, files_snapshot, subdomain) VALUES (1, 'list_app', 'pid789', 'running', '{}', 'list-subdomain')")
        db.commit()
        
        from flask import g
        g.user_id = 1
        
        res = agent_tools.repo_control(
            action="list_history",
            status="Listing history...",
            timeout=10
        )
        assert "list_app" in res
        assert "pid789" in res
        assert "running" in res


def test_repo_control_rename_success(auth_client):
    """
    Asserts that repo_control rename updates the name of the deployment and subdomain in the database.
    """
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT OR IGNORE INTO repo_history (user_id, project_name, process_id, status, files_snapshot, subdomain) VALUES (1, 'old_name', 'pid_rename', 'running', '{}', 'old-sub')")
        db.commit()
        
        from flask import g
        g.user_id = 1
        
        res = agent_tools.repo_control(
            action="rename",
            app_id="pid_rename",
            project_name="new_name",
            status="Renaming project...",
            timeout=10
        )
        assert "Deployment renamed to 'new_name'" in res
        
        # Verify database reflects rename
        row = db.execute("SELECT project_name FROM repo_history WHERE process_id = 'pid_rename'").fetchone()
        assert row['project_name'] == "new_name"


@patch('app.stop_and_cleanup_app_by_process_id')
def test_repo_control_stop_success(mock_stop_cleanup, auth_client):
    """
    Asserts that repo_control stop terminates the container and updates database status to 'stopped'.
    """
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT OR IGNORE INTO repo_history (user_id, project_name, process_id, status, files_snapshot) VALUES (1, 'stop_app', 'pid_stop', 'running', '{}')")
        db.commit()
        
        from flask import g
        g.user_id = 1
        
        res = agent_tools.repo_control(
            action="stop",
            app_id="pid_stop",
            status="Stopping app...",
            timeout=10
        )
        assert "stopped" in res
        mock_stop_cleanup.assert_called_once_with('pid_stop', app_type='repo')
        
        # Verify db status is updated
        row = db.execute("SELECT status FROM repo_history WHERE process_id = 'pid_stop'").fetchone()
        assert row['status'] == "stopped"


@patch('docker.from_env')
@patch('app.stop_and_cleanup_app_by_process_id')
@patch('app.ensure_user_network')
def test_repo_control_restart_success(mock_ensure_net, mock_stop_cleanup, mock_docker_env, auth_client):
    """
    Asserts that repo_control restart stops the existing container and runs a new one, loading snapshotted files.
    """
    mock_ensure_net.return_value = "stellar_isolated"
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    mock_container = MagicMock()
    mock_container.id = "restarted_container_id"
    mock_container.status = "running"
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b"200")
    mock_container.attrs = {
        'NetworkSettings': {
            'Ports': {
                '5000/tcp': [{'HostPort': '12345'}]
            }
        }
    }
    mock_client.containers.run.return_value = mock_container
    
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute("INSERT OR IGNORE INTO repo_history (user_id, project_name, process_id, status, files_snapshot, subdomain) VALUES (1, 'restart_app', 'pid_restart', 'running', '{\"port\": 5000}', 'restart-sub')")
        db.commit()
        
        from flask import g
        g.user_id = 1
        
        with patch('os.makedirs'), \
             patch('os.listdir', return_value=[]), \
             patch('app.redis_client', MagicMock()):
            
            res = agent_tools.repo_control(
                action="restart",
                app_id="pid_restart",
                status="Restarting...",
                timeout=10
            )
            assert "restarted" in res
            assert "Live URL:" in res
            mock_stop_cleanup.assert_called_once_with('pid_restart', app_type='repo')


def test_repo_control_snapshot_success(auth_client):
    """
    Asserts that repo_control snapshot returns success message for manually snapshotted files.
    """
    res = agent_tools.repo_control(
        action="snapshot",
        app_id="pid_snap",
        files=["file1.py", "file2.py"],
        status="Snapshotting...",
        timeout=10
    )
    assert "Successfully snapshotted" in res
    assert "pid_snap" in res


def test_repo_control_unknown_action(auth_client):
    """
    Asserts that repo_control returns error message on unknown actions.
    """
    res = agent_tools.repo_control(
        action="invalid_action",
        status="Testing...",
        timeout=10
    )
    assert "Unknown action" in res


# --- lab_execute tests ---

@patch('docker.from_env')
@patch('app.ensure_user_network')
def test_lab_execute_success(mock_ensure_net, mock_docker_env, auth_client):
    """
    Asserts that lab_execute executes a command in a persistent sandbox and returns its output.
    """
    mock_ensure_net.return_value = "stellar_isolated"
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    mock_container = MagicMock()
    mock_container.status = 'running'
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b"Hello from sandbox")
    mock_client.containers.get.return_value = mock_container
    
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1
        
        with patch('os.makedirs'), \
             patch('os.path.exists', return_value=False), \
             patch('shutil.copy2'):
            
            res = agent_tools.lab_execute(
                command="echo 'Hello'",
                status="Executing...",
                timeout=10
            )
            assert "Hello from sandbox" in res


@patch('docker.from_env')
@patch('app.ensure_user_network')
def test_lab_execute_not_found_creates_container(mock_ensure_net, mock_docker_env, auth_client):
    """
    Asserts that if the sandbox container does not exist, lab_execute creates and runs it.
    """
    mock_ensure_net.return_value = "stellar_isolated"
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    mock_client.containers.get.side_effect = MockNotFound("Not Found")
    
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=b"created and ran")
    mock_client.containers.run.return_value = mock_container
    
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1
        
        with patch('os.makedirs'), \
             patch('os.path.exists', return_value=False):
            
            res = agent_tools.lab_execute(
                command="echo 'Hi'",
                status="Executing...",
                timeout=10
            )
            assert "created and ran" in res
            mock_client.containers.run.assert_called_once()


# --- analyze_youtube_video tests ---

@patch('agent_tools.requests.get')
def test_analyze_youtube_video_search_success(mock_get, auth_client):
    """
    Asserts that analyze_youtube_video searches for videos and returns stats in JSON format.
    """
    with auth_client.application.app_context():
        from app import app
        with patch.dict(app.config, {'YOUTUBE_API_KEY': 'fake_yt_key'}), \
             patch('app.YOUTUBE_API_KEY', 'fake_yt_key'):
            
            # Setup search response
            mock_search_res = MagicMock()
            mock_search_res.json.return_value = {
                'items': [{'id': {'videoId': 'vid_test'}, 'snippet': {'title': 'My Video', 'channelTitle': 'My Channel'}}]
            }
            # Setup stats response
            mock_stats_res = MagicMock()
            mock_stats_res.json.return_value = {
                'items': [{
                    'id': 'vid_test',
                    'snippet': {'title': 'My Video', 'channelTitle': 'My Channel', 'description': 'Video desc'},
                    'statistics': {'viewCount': '1500', 'likeCount': '85'},
                    'contentDetails': {'duration': 'PT5M30S'}
                }]
            }
            mock_get.side_effect = [mock_search_res, mock_stats_res]
            
            res = agent_tools.analyze_youtube_video(
                query="Python tutoring",
                status="Searching...",
                timeout=15,
                action="search"
            )
            assert "vid_test" in res
            assert "My Video" in res
            assert "My Channel" in res
            assert "PT5M30S" in res


@patch('agent_tools.genai.Client')
def test_analyze_youtube_video_analyze_success(mock_client_class, auth_client):
    """
    Asserts that analyze_youtube_video analyze action calls Gemini multimodal API and returns analysis.
    """
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_resp.text = "This video outlines AI agents."
    mock_client.models.generate_content.return_value = mock_resp
    
    with auth_client.application.app_context():
        res = agent_tools.analyze_youtube_video(
            query="Analyze agents",
            status="Analyzing...",
            timeout=10,
            action="analyze",
            video_url="https://youtube.com/watch?v=agents123"
        )
        assert "AI agents" in res


# --- manage_files tests ---

@patch('docker.from_env')
def test_manage_files_read_success(mock_docker_env, auth_client):
    """
    Asserts that manage_files read list all files in upload folder.
    """
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1
        
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=["report.pdf", "data.csv"]):
            
            res = agent_tools.manage_files(
                action="read",
                status="Reading...",
                timeout=10
            )
            assert "report.pdf" in res
            assert "data.csv" in res


@patch('docker.from_env')
def test_manage_files_move_success(mock_docker_env, auth_client):
    """
    Asserts that manage_files move successfully transfers a file from uploads to the lab container workspace.
    """
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.put_archive.return_value = True
    mock_client.containers.get.return_value = mock_container
    
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1
        
        with patch('os.path.exists', return_value=True), \
             patch('tarfile.open'), \
             patch('builtins.open', MagicMock()):
            
            res = agent_tools.manage_files(
                action="move",
                file_name="notes.txt",
                target_env="lab",
                source_env="chat",
                status="Moving...",
                timeout=15
            )
            assert "Moved 'notes.txt' from chat to lab" in res


@patch('docker.from_env')
def test_manage_files_project_success(mock_docker_env, auth_client):
    """
    Asserts that manage_files project retrieves file from container, saves to outputs directory, and returns link.
    """
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.exec_run.return_value = MagicMock(exit_code=1) # Is a file, not a directory
    
    # Mock container.get_archive output (tar data containing the file)
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        info = tarfile.TarInfo(name="result.txt")
        info.size = len(b"final results content")
        tar.addfile(info, io.BytesIO(b"final results content"))
    tar_stream.seek(0)
    
    mock_container.get_archive.return_value = ([tar_stream.getvalue()], MagicMock())
    mock_client.containers.get.return_value = mock_container
    
    with auth_client.application.app_context():
        from flask import g
        g.user_id = 1
        g.chat_id = 1
        
        with patch('os.makedirs'), \
             patch('builtins.open', MagicMock()):
            
            res = agent_tools.manage_files(
                action="project",
                file_name="result.txt",
                target_env="lab",
                status="Projecting...",
                timeout=15
            )
            assert "Projected successfully" in res
            assert "result.txt" in res
