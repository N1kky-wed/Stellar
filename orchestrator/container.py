# container.py
import subprocess
import os
import json
import logging
from typing import Optional, Tuple, List, Dict, Any
import orchestrator.config as config

logger = logging.getLogger("stellar-orchestrator")

def is_container_running() -> bool:
    try:
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", config.CONTAINER_NAME],
            capture_output=True, text=True, check=True
        )
        return res.stdout.strip() == "true"
    except Exception as e:
        logger.error(f"Error checking if container {config.CONTAINER_NAME} is running: {e}")
        return False

def exec_in_container(cmd: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    """Execute a command inside the container using docker exec."""
    try:
        args = ["docker", "exec", config.CONTAINER_NAME, "bash", "-c", cmd]
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired as te:
        logger.error(f"Timeout executing command in container: {cmd}")
        return -1, "", "TimeoutExpired"
    except Exception as e:
        logger.error(f"Exception executing command in container: {e}")
        return -1, "", str(e)

def copy_to_container(host_path: str, container_path: str):
    subprocess.run(["docker", "exec", config.CONTAINER_NAME, "mkdir", "-p", os.path.dirname(container_path)], check=True)
    subprocess.run(["docker", "cp", host_path, f"{config.CONTAINER_NAME}:{container_path}"], check=True)

def remove_from_container(container_path: str):
    subprocess.run(["docker", "exec", config.CONTAINER_NAME, "rm", "-rf", container_path])

def load_agent_prompt(agent_id: str, prompt_file: str):
    """Loads agent instructions and reviewer specs into container."""
    # 1. Load the agent prompt
    host_prompt_path = os.path.join(config.HOST_AGENTS_DIR, prompt_file)
    container_prompt_path = os.path.join(config.CONTAINER_AGENTS_DIR, "AGENTS.md")
    
    if not os.path.exists(host_prompt_path):
        raise FileNotFoundError(f"Host agent prompt file not found: {host_prompt_path}")
        
    logger.info(f"Loading agent {agent_id} prompt from {host_prompt_path} into container {container_prompt_path}")
    copy_to_container(host_prompt_path, container_prompt_path)

    # 2. Copy the reviewer plugin config and specifications
    logger.info("Loading code-reviewer plugin into container plugins directory")
    # The plugin config needs to reside inside ~/.gemini/antigravity-cli/plugins/code-review
    # Let's copy the entire scratch/code-review-plugin dir into the container
    subprocess.run(["docker", "exec", config.CONTAINER_NAME, "mkdir", "-p", config.CONTAINER_REVIEWER_DIR], check=True)
    # Copy host content to container destination. Docker cp does this cleanly.
    # Note: docker cp host_dir/. container:dest_dir copies contents of host_dir into dest_dir
    subprocess.run([
        "docker", "cp", 
        f"{config.HOST_REVIEWER_DIR}/.", 
        f"{config.CONTAINER_NAME}:{config.CONTAINER_REVIEWER_DIR}"
    ], check=True)
    
    # Install the plugin via agy CLI
    rc_inst, stdout_inst, stderr_inst = exec_in_container(f"{config.AGY_BINARY} plugin install {config.CONTAINER_REVIEWER_DIR}")
    if rc_inst != 0:
        logger.error(f"Failed to install code-review plugin: {stderr_inst}")
    
    # Verify reviewer is registered by running: agy plugin list
    rc, stdout, stderr = exec_in_container(f"{config.AGY_BINARY} plugin list")
    logger.info(f"Loaded plugins in container:\n{stdout}")

def unload_agent_prompt():
    """Cleans up the loaded agent prompt and plugins to prevent leakage."""
    logger.info("Unloading agent prompt and plugins from container...")
    remove_from_container(os.path.join(config.CONTAINER_AGENTS_DIR, "AGENTS.md"))
    
    # Uninstall the plugin via agy CLI
    exec_in_container(f"{config.AGY_BINARY} plugin uninstall code-review")
    
    remove_from_container(config.CONTAINER_REVIEWER_DIR)
    
    # Kill any runaway or stale agent-spawned processes inside the container
    exec_in_container("pkill -f pytest; pkill -f python; pkill -f node; pkill -f npm; pkill -f git; pkill -f gh")
    
    remove_from_container(config.CONTAINER_WORKSPACE)
    logger.info("Unloaded successfully.")

def run_agent(agent_id: str, prompt_file: str, branch_name: str) -> subprocess.Popen:
    """Launch the agent's work cycle in a background process."""
    # Ensure agent prompt is loaded
    load_agent_prompt(agent_id, prompt_file)
    
    # Prepare the git branch inside container
    prepare_git_cmd = f"""
    rm -rf {config.CONTAINER_WORKSPACE} && \
    git clone git@github.com:{config.GITHUB_REPO}.git {config.CONTAINER_WORKSPACE} && \
    cd {config.CONTAINER_WORKSPACE} && \
    git checkout -B {branch_name} && \
    pip install -r requirements.txt
    """
    rc, out, err = exec_in_container(prepare_git_cmd)
    if rc != 0:
        logger.error(f"Failed to prepare git branch {branch_name}: {err}")
        raise RuntimeError(f"Git branch preparation failed: {err}")

    # Launch agy in the background
    # Note: We must tell agy to read instructions from /root/.agents/AGENTS.md and act on /root/Stellar.
    # Since we want to capture stdout/stderr, we redirect them to a log or we let the parent process read it.
    # We will invoke agy with output redirecting to a log file inside the container, or pipe it.
    # Let's run agy via docker exec inside a subprocess.Popen so the watchdog can monitor/kill it.
    
    agy_exec_cmd = f"""
    cd {config.CONTAINER_WORKSPACE} && \
    export PATH="/root/.local/bin:$PATH" && \
    /root/.local/bin/agy --dangerously-skip-permissions --print-timeout 40m --print "Please read your instructions from /root/.agents/AGENTS.md carefully and fulfill your role completely. You are working in the repository /root/Stellar. CRITICAL: Every turn of your response MUST contain at least one tool call until your work is fully completed and you have opened the pull request. Do NOT write text-only thoughts or plan descriptions without accompanying tool calls, otherwise your execution will terminate prematurely. When your work is complete, verify it using your verify instructions, get code-reviewer approval, and then open a pull request using the github cli (gh pr create). CRITICAL: Do NOT checkout or create a new git branch. You are already placed on a dedicated branch for this run; make commits and open the PR directly from the current active branch."
    """
    
    logger.info(f"Starting agy run for agent {agent_id} on branch {branch_name}...")
    
    proc = subprocess.Popen(
        ["docker", "exec", "-t", config.CONTAINER_NAME, "bash", "-c", agy_exec_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    return proc

def check_new_prs(branch_name: str) -> List[Dict[str, Any]]:
    """Query GitHub CLI inside container to find if any PR matches the branch."""
    cmd = f"gh pr list --repo {config.GITHUB_REPO} --head {branch_name} --state all --json number,url,state,title"
    rc, stdout, stderr = exec_in_container(cmd)
    if rc == 0:
        try:
            return json.loads(stdout)
        except Exception as e:
            logger.error(f"Failed to parse PR JSON output: {e}. Output was: {stdout}")
            return []
    else:
        logger.error(f"Failed to fetch PR list: {stderr}")
        return []

def check_pr_status(pr_number: int) -> str:
    """Check the status of a specific PR."""
    cmd = f"gh pr view {pr_number} --repo {config.GITHUB_REPO} --json state"
    rc, stdout, stderr = exec_in_container(cmd)
    if rc == 0:
        try:
            data = json.loads(stdout)
            return data.get("state", "OPEN") # OPEN, MERGED, CLOSED
        except Exception as e:
            logger.error(f"Failed to parse PR status: {e}")
            return "OPEN"
    else:
        logger.error(f"Failed to view PR {pr_number}: {stderr}")
        return "OPEN"

def get_agent_final_summary() -> Optional[str]:
    """Find the most recent conversation transcript in the container and extract the final message."""
    find_file_cmd = "ls -1t /root/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl 2>/dev/null | head -n 1"
    rc, stdout, stderr = exec_in_container(find_file_cmd)
    if rc != 0 or not stdout.strip():
        return None
        
    transcript_path = stdout.strip()
    
    # Read the transcript file from the container
    cat_cmd = f"cat {transcript_path} 2>/dev/null"
    rc, stdout, stderr = exec_in_container(cat_cmd)
    if rc != 0 or not stdout.strip():
        return None
        
    # Parse the jsonl lines from the end to find the last PLANNER_RESPONSE
    lines = stdout.strip().split('\n')
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if data.get("type") == "PLANNER_RESPONSE" and data.get("status") == "DONE":
                content = data.get("content")
                if content:
                    return content
        except Exception:
            continue
            
    return None

