import pytest
import time
from unittest.mock import MagicMock, patch
from app import OrphanContainerMonitor, active_apps, active_apps_lock

def test_orphan_monitor_cleanup(mock_docker_client):
    # Setup
    monitor = OrphanContainerMonitor(interval=1)

    # Mock containers
    container1 = MagicMock()
    container1.status = 'running'
    container1.short_id = 'c1'
    container1.labels = {
        'stellar_type': 'repo',
        'stellar_process_id': 'p1',
        'created_at_ts': str(time.time() - 100) # Older than 60s
    }

    container2 = MagicMock()
    container2.status = 'running'
    container2.short_id = 'c2'
    container2.labels = {
        'stellar_type': 'repo',
        'stellar_process_id': 'p2',
        'created_at_ts': str(time.time()) # Recent
    }

    container3 = MagicMock()
    container3.status = 'exited'
    container3.short_id = 'c3'
    container3.labels = {}

    mock_docker_client.containers.list.return_value = [container1, container2, container3]

    # Simulate p2 being active, p1 being inactive
    with active_apps_lock:
        active_apps['p2'] = {}
        if 'p1' in active_apps:
            del active_apps['p1']

    # Run cleanup
    with patch('app.client', mock_docker_client):
        monitor._cleanup_orphans()

    # Verify c1 (orphan) was stopped and removed
    container1.stop.assert_called_once()
    container1.remove.assert_called_once()

    # Verify c2 (active) was NOT touched
    container2.stop.assert_not_called()
    container2.remove.assert_not_called()

    # Verify c3 (exited) was removed
    container3.remove.assert_called_once()

def test_cleanup_stale_containers(mock_docker_client):
    from app import cleanup_stale_containers

    container1 = MagicMock()
    container1.name = 'stellar-sandbox-1'

    container2 = MagicMock()
    container2.name = 'other-container'

    mock_docker_client.containers.list.side_effect = lambda all=False, filters=None: (
        [container1] if filters and ('name' in filters or 'label' in filters) else []
    )
    # The actual implementation calls list twice with different filters and unions them.
    # Let's adjust the side_effect to match better.

    def side_effect(all=False, filters=None):
        if filters and filters.get('label') == 'stellar_type':
            return [container1]
        if filters and filters.get('name') == 'stellar-sandbox-*':
            return [container1]
        return []

    mock_docker_client.containers.list.side_effect = side_effect

    with patch('app.client', mock_docker_client):
        # We need to patch docker.from_env inside the function too, or ensure it uses the mock
        with patch('docker.from_env', return_value=mock_docker_client):
            cleanup_stale_containers()

    container1.remove.assert_called()
