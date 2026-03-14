import pytest
from unittest.mock import patch, MagicMock

def test_docker_cleanup(mocker):

    mock_container = mocker.Mock()
    mock_container.labels = {"stellar_session": "test_session"}
    mock_container.status = "running"

    mock_client = mocker.Mock()
    mock_client.containers.list.return_value = [mock_container]

    mocker.patch("docker.from_env", return_value=mock_client)

    from app import OrphanContainerMonitor

    # We must patch app.client which is what monitor uses
    mocker.patch('app.client', mock_client)

    monitor = OrphanContainerMonitor(interval=1)
    monitor._cleanup_orphans()

    mock_container.stop.assert_called_once()
