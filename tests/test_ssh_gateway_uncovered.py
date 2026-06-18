import os
import json
import pytest
import sqlite3
import threading
import logging
from unittest.mock import patch, MagicMock
import docker

import ssh_gateway

# ---------------------------------------------------------------------------
# LazyRedis Tests
# ---------------------------------------------------------------------------

def test_lazy_redis_getattr_success():
    """
    Asserts that LazyRedis lazily initializes the redis.Redis client and
    routes attributes/methods to it successfully.
    """
    lazy = ssh_gateway.LazyRedis("localhost", port=6379)
    assert lazy._client is None
    # Access an attribute (e.g. ping) to trigger initialization
    assert lazy.ping() is True
    assert lazy._client is not None


def test_lazy_redis_getattr_attribute_error():
    """
    Asserts that accessing a non-existent attribute on LazyRedis raises AttributeError.
    """
    lazy = ssh_gateway.LazyRedis("localhost", port=6379)
    with pytest.raises(AttributeError):
        _ = lazy.non_existent_attribute


# ---------------------------------------------------------------------------
# GatewayFormatter Tests
# ---------------------------------------------------------------------------

def test_gateway_formatter_format_with_session_id():
    """
    Asserts that GatewayFormatter successfully extracts the session_id from
    thread-local storage and attaches it to the LogRecord.
    """
    formatter = ssh_gateway.GatewayFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="test message",
        args=(),
        exc_info=None
    )
    
    with patch.object(ssh_gateway._thread_local, 'session_id', 'session-12345', create=True):
        formatter.format(record)
        assert record.session_id == 'session-12345'


def test_gateway_formatter_format_without_session_id():
    """
    Asserts that GatewayFormatter defaults session_id to 'system' when it is
    not set on thread-local storage.
    """
    formatter = ssh_gateway.GatewayFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="test message",
        args=(),
        exc_info=None
    )
    
    # Ensure session_id is not set
    if hasattr(ssh_gateway._thread_local, 'session_id'):
        delattr(ssh_gateway._thread_local, 'session_id')
        
    formatter.format(record)
    assert record.session_id == 'system'


def test_gateway_formatter_format_exception():
    """
    Asserts that GatewayFormatter catches any exception raised during thread-local
    lookup and sets session_id to 'error'.
    """
    formatter = ssh_gateway.GatewayFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="test message",
        args=(),
        exc_info=None
    )
    
    class BadThreadLocal:
        @property
        def session_id(self):
            raise RuntimeError("Unexpected failure")

    with patch('ssh_gateway._thread_local', BadThreadLocal()):
        formatter.format(record)
        assert record.session_id == 'error'


# ---------------------------------------------------------------------------
# get_container Tests
# ---------------------------------------------------------------------------

def test_get_container_web_app_schema():
    """
    Asserts that get_container retrieves the container matching the app_type name
    schema (stellar-web-process) first.
    """
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.get.return_value = mock_container

    container = ssh_gateway.get_container(mock_client, "proc123", "web")
    assert container == mock_container
    mock_client.containers.get.assert_called_once_with("stellar-web-proc123")


def test_get_container_fallback_repo_schema():
    """
    Asserts that get_container falls back to checking the default generic 'stellar-repo-'
    naming convention if the app_type name schema raises docker.errors.NotFound.
    """
    mock_client = MagicMock()
    mock_container = MagicMock()
    
    # Define a clean Exception subclass that matches docker.errors.NotFound to prevent TypeError
    class LocalNotFound(Exception):
        pass

    with patch.object(docker.errors, 'NotFound', LocalNotFound):
        # First call throws LocalNotFound, second returns mock_container
        mock_client.containers.get.side_effect = [LocalNotFound("Not found"), mock_container]

        container = ssh_gateway.get_container(mock_client, "proc123", "web")
        assert container == mock_container
        assert mock_client.containers.get.call_count == 2
        mock_client.containers.get.assert_any_call("stellar-web-proc123")
        mock_client.containers.get.assert_any_call("stellar-repo-proc123")


# ---------------------------------------------------------------------------
# Background Refresher Cache Tests
# ---------------------------------------------------------------------------

@patch('ssh_gateway.get_docker_client')
def test_start_refresher_thread_if_needed(mock_get_docker):
    """
    Asserts that start_refresher_thread_if_needed populates the container cache,
    spawns a daemon refresher thread on first call, and does not spawn a second thread.
    """
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.name = "stellar-web-proc1"
    mock_container.status = "running"
    mock_client.containers.list.return_value = [mock_container]
    mock_get_docker.return_value = mock_client

    # Reset module state variables for clean testing
    with patch('ssh_gateway._refresher_started', False):
         
        # We also mock threading.Thread to avoid starting a real infinite loop thread in tests
        mock_thread = MagicMock()
        with patch('threading.Thread', return_value=mock_thread) as mock_thread_class:
            ssh_gateway.start_refresher_thread_if_needed()
            
            # Verify cache populated with the initial query
            assert ssh_gateway._container_statuses_cache == {"stellar-web-proc1": "running"}
            # Verify thread instantiated and started
            mock_thread_class.assert_called_once()
            mock_thread.start.assert_called_once()


@patch('ssh_gateway.get_docker_client')
def test_docker_status_refresher_loop_handles_exception(mock_get_docker):
    """
    Asserts that _docker_status_refresher handles exceptions gracefully when listing containers.
    """
    mock_get_docker.side_effect = Exception("Docker daemon unavailable")
    
    # We want to test a single iteration of the refresher loop.
    # We can mock wait to return True/False or simply raise a custom exception to break the loop.
    class BreakLoopException(Exception):
        pass

    with patch('ssh_gateway._refresh_event.wait', side_effect=BreakLoopException):
        with pytest.raises(BreakLoopException):
            ssh_gateway._docker_status_refresher()


def test_invalidate_container_cache():
    """
    Asserts that invalidate_container_cache sets the _refresh_event to wake up the refresher.
    """
    with patch('ssh_gateway._refresh_event') as mock_event:
        ssh_gateway.invalidate_container_cache()
        mock_event.set.assert_called_once()
