import time
import socket
import pytest
from unittest.mock import MagicMock, patch
import AngelTrace

def test_angel_tracer_cooldown():
    """Verify that AngelTracer connect respects connection backoff cooldown."""
    tracer = AngelTrace.AngelTracer(host="127.0.0.1", port=9099)
    assert tracer.sock is None
    assert tracer.last_connect_attempt == 0.0

    # Patch socket.socket to fail connection immediately
    with patch("socket.socket") as mock_socket_class:
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("Connection refused")
        mock_socket_class.return_value = mock_sock

        # First connection attempt should call socket.connect and update last_connect_attempt
        tracer.connect()
        assert tracer.sock is None
        mock_sock.connect.assert_called_once()
        t1 = tracer.last_connect_attempt
        assert t1 > 0.0
        mock_sock.close.assert_called_once()

        # Reset mocks
        mock_sock.reset_mock()
        mock_socket_class.reset_mock()

        # Second connection attempt immediately after should be blocked by cooldown
        tracer.connect()
        mock_socket_class.assert_not_called()
        assert tracer.last_connect_attempt == t1

        # Simulate time passing beyond cooldown
        with patch("time.monotonic", return_value=t1 + 6.0):
            tracer.connect()
            mock_socket_class.assert_called_once()
            assert tracer.last_connect_attempt == t1 + 6.0
            mock_sock.close.assert_called_once()


def test_angel_tracer_send_trace_error_handling():
    """Verify that send_trace handles socket send errors and closes socket on failure."""
    tracer = AngelTrace.AngelTracer(host="127.0.0.1", port=9099)
    
    # Pre-populate socket
    mock_sock = MagicMock()
    mock_sock.sendall.side_effect = OSError("Send failed")
    tracer.sock = mock_sock

    # Send trace should fail, close the socket, and set it to None
    tracer.send_trace("test_node", 12345, "test_trace_id")
    time.sleep(1.5)
    assert tracer.sock is None
    mock_sock.close.assert_called_once()


def test_angel_tracer_send_event_error_handling():
    """Verify that send_event handles socket send errors and closes socket on failure."""
    tracer = AngelTrace.AngelTracer(host="127.0.0.1", port=9099)
    
    # Pre-populate socket
    mock_sock = MagicMock()
    mock_sock.sendall.side_effect = OSError("Send failed")
    tracer.sock = mock_sock

    # Send event should fail, close the socket, and set it to None
    tracer.send_event({"event": "test"})
    time.sleep(1.5)
    assert tracer.sock is None
    mock_sock.close.assert_called_once()
