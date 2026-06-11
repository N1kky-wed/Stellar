import sys
import os
import subprocess
import pytest
from unittest.mock import patch, MagicMock, mock_open
import dockersetup

# Tests verify behavior of the dockersetup script, ensuring robust mocking of docker client and subprocesses.

@patch("subprocess.run")
def test_check_docker_dependencies_success(mock_run):
    """Asserts that check_docker_dependencies returns True when docker is installed and running."""
    mock_run.return_value = MagicMock(returncode=0)
    res = dockersetup.check_docker_dependencies()
    assert res is True
    assert mock_run.call_count == 2


@patch("subprocess.run")
def test_check_docker_dependencies_no_daemon(mock_run):
    """Asserts that check_docker_dependencies returns False when Docker daemon is not running."""
    # First call is version check (succeeds), second call is info check (fails with daemon error)
    mock_run.side_effect = [
        MagicMock(returncode=0),
        subprocess.CalledProcessError(1, "docker info", stderr="docker daemon is not running")
    ]
    res = dockersetup.check_docker_dependencies()
    assert res is False


@patch("subprocess.run")
def test_check_docker_dependencies_no_docker(mock_run):
    """Asserts that check_docker_dependencies returns False when docker command is not found."""
    mock_run.side_effect = FileNotFoundError()
    res = dockersetup.check_docker_dependencies()
    assert res is False


def test_create_dockerfiles_success():
    """Asserts that create_dockerfiles creates the target directory and writes all Dockerfile blueprints."""
    with patch("os.makedirs") as mock_makedirs, \
         patch("builtins.open", mock_open()) as mocked_file:
        res = dockersetup.create_dockerfiles()
        assert res is True
        mock_makedirs.assert_called_once_with(dockersetup.TARGET_DIRECTORY, exist_ok=True)
        assert mocked_file.call_count == len(dockersetup.DOCKERFILES_TO_CREATE)


@patch("subprocess.run")
@patch("builtins.input", return_value="n")
def test_manage_images_all_exist(mock_input, mock_run):
    """Asserts that if all Docker images already exist, manage_images prompts user and can return True without building."""
    # Mock 'docker images' to return all image tags
    image_list_output = "\n".join(dockersetup.IMAGES_TO_BUILD.values())
    mock_run.return_value = MagicMock(stdout=image_list_output, returncode=0)

    res = dockersetup.manage_images(force_rebuild=False)
    assert res is True
    mock_input.assert_called_once()


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_manage_images_build_missing(mock_popen, mock_run):
    """Asserts that missing images are built via subprocess.Popen."""
    # Mock 'docker images' to return only one image, making others missing
    mock_run.return_value = MagicMock(stdout="some-other-image:latest\n", returncode=0)

    mock_process = MagicMock()
    mock_process.stdout.readline.side_effect = ["line1", "line2", ""]
    mock_process.wait.return_value = 0
    mock_process.returncode = 0
    mock_popen.return_value = mock_process

    res = dockersetup.manage_images(force_rebuild=False)
    assert res is True
    assert mock_popen.call_count == len(dockersetup.IMAGES_TO_BUILD)


@patch("dockersetup.check_docker_dependencies", return_value=True)
@patch("dockersetup.create_dockerfiles", return_value=True)
@patch("dockersetup.manage_images", return_value=True)
def test_main_success(mock_manage, mock_create, mock_check):
    """Asserts that main orchestrates the entire setup correctly."""
    with patch("sys.argv", ["dockersetup.py"]):
        dockersetup.main()
        mock_check.assert_called_once()
        mock_create.assert_called_once()
        mock_manage.assert_called_once_with(force_rebuild=False)
