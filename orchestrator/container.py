# container.py
import subprocess
import os
import json
import logging
import time
from typing import Optional, Tuple, List, Dict, Any
import orchestrator.config as config

logger = logging.getLogger("stellar-orchestrator")

def is_container_running() -> bool:
    t0 = time.time()
    try:
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", config.CONTAINER_NAME],
            capture_output=True, text=True, check=True
        )
        running = res.stdout.strip() == "true"
        # Only log check if not running to avoid cluttering logs on every tick
        if not running:
            logger.warning("Checked if container is running: running=False container=%s duration_sec=%.3f", config.CONTAINER_NAME, time.time() - t0)
        return running
    except Exception as e:
        logger.error("Error checking if container is running: container=%s error=%s duration_sec=%.3f", config.CONTAINER_NAME, str(e), time.time() - t0)
        return False

def exec_in_container(cmd: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    """Execute a command inside the container using docker exec."""
    t0 = time.time()
    try:
        args = ["docker", "exec", config.CONTAINER_NAME, "bash", "-c", cmd]
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        duration = time.time() - t0
        # Truncate cmd in log message if too long to keep logs clean
        cmd_summary = cmd[:100] + "..." if len(cmd) > 100 else cmd
        logger.info("Executed command in container cmd=%s exit_code=%d duration_sec=%.3f", cmd_summary, res.returncode, duration)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired as te:
        duration = time.time() - t0
        logger.error("Timeout executing command in container: cmd=%s duration_sec=%.3f", cmd, duration)
        return -1, "", "TimeoutExpired"
    except Exception as e:
        duration = time.time() - t0
        logger.error("Exception executing command in container: cmd=%s error=%s duration_sec=%.3f", cmd, str(e), duration)
        return -1, "", str(e)

def copy_to_container(host_path: str, container_path: str):
    t0 = time.time()
    try:
        subprocess.run(["docker", "exec", config.CONTAINER_NAME, "mkdir", "-p", os.path.dirname(container_path)], check=True)
        subprocess.run(["docker", "cp", host_path, f"{config.CONTAINER_NAME}:{container_path}"], check=True)
        logger.info("Copied file to container: host_path=%s container_path=%s duration_sec=%.3f", host_path, container_path, time.time() - t0)
    except Exception as e:
        logger.error("Failed to copy file to container: host_path=%s container_path=%s error=%s duration_sec=%.3f", host_path, container_path, str(e), time.time() - t0)
        raise

def remove_from_container(container_path: str):
    t0 = time.time()
    try:
        subprocess.run(["docker", "exec", config.CONTAINER_NAME, "rm", "-rf", container_path])
        logger.info("Removed file/directory from container: container_path=%s duration_sec=%.3f", container_path, time.time() - t0)
    except Exception as e:
        logger.error("Failed to remove file/directory from container: container_path=%s error=%s duration_sec=%.3f", container_path, str(e), time.time() - t0)

def load_agent_prompt(agent_id: str, prompt_file: str):
    """Loads agent instructions and reviewer specs into container."""
    t0 = time.time()
    # 1. Load the agent prompt
    host_prompt_path = os.path.join(config.HOST_AGENTS_DIR, prompt_file)
    container_prompt_path = os.path.join(config.CONTAINER_AGENTS_DIR, "AGENTS.md")
    
    if not os.path.exists(host_prompt_path):
        logger.error("Host agent prompt file not found: host_prompt_path=%s", host_prompt_path)
        raise FileNotFoundError(f"Host agent prompt file not found: {host_prompt_path}")
        
    logger.info("Loading agent prompt: agent_id=%s host_prompt_path=%s container_prompt_path=%s", agent_id, host_prompt_path, container_prompt_path)
    copy_to_container(host_prompt_path, container_prompt_path)

    # 2. Copy the reviewer plugin config and specifications
    logger.info("Loading code-reviewer plugin into container plugins directory: destination=%s", config.CONTAINER_REVIEWER_DIR)
    # The plugin config needs to reside inside ~/.gemini/antigravity-cli/plugins/code-review
    # Let's copy the entire scratch/code-review-plugin dir into the container
    try:
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
            logger.error("Failed to install code-review plugin: error=%s", stderr_inst)
        
        # Verify reviewer is registered by running: agy plugin list
        rc, stdout, stderr = exec_in_container(f"{config.AGY_BINARY} plugin list")
        logger.info("Loaded plugins in container: duration_sec=%.3f output=%s", time.time() - t0, stdout.strip())
    except Exception as e:
        logger.error("Failed loading reviewer plugin: error=%s duration_sec=%.3f", str(e), time.time() - t0)
        raise

def unload_agent_prompt():
    """Cleans up the loaded agent prompt and plugins to prevent leakage."""
    t0 = time.time()
    logger.info("Unloading agent prompt and plugins from container...")
    remove_from_container(os.path.join(config.CONTAINER_AGENTS_DIR, "AGENTS.md"))
    remove_from_container(os.path.join(config.CONTAINER_AGENTS_DIR, "memory_context.md"))
    remove_from_container(os.path.join(config.CONTAINER_AGENTS_DIR, "memory_outbox.json"))
    
    # Uninstall the plugin via agy CLI
    exec_in_container(f"{config.AGY_BINARY} plugin uninstall code-review")
    
    remove_from_container(config.CONTAINER_REVIEWER_DIR)
    
    # Kill any runaway or stale agent-spawned processes inside the container
    exec_in_container("pkill -f pytest; pkill -f python; pkill -f node; pkill -f npm; pkill -f git; pkill -f gh")
    
    remove_from_container(config.CONTAINER_WORKSPACE)
    logger.info("Unloaded successfully: duration_sec=%.3f", time.time() - t0)

def copy_memory_context_to_container(host_path: str):
    """Copies the memory context markdown file to the container's agent directory."""
    container_path = os.path.join(config.CONTAINER_AGENTS_DIR, "memory_context.md")
    logger.info("Loading memory context into container: container_path=%s", container_path)
    copy_to_container(host_path, container_path)

def read_memory_outbox_from_container(host_path: str) -> bool:
    """Attempts to copy memory_outbox.json from the container to the host. Returns True if file exists."""
    t0 = time.time()
    container_path = os.path.join(config.CONTAINER_AGENTS_DIR, "memory_outbox.json")
    
    # Check if file exists inside container first
    rc, _, _ = exec_in_container(f"[ -f {container_path} ]")
    if rc != 0:
        logger.info("No memory outbox file found in container.")
        return False
        
    try:
        logger.info("Copying memory outbox from container %s to host %s", container_path, host_path)
        subprocess.run(["docker", "cp", f"{config.CONTAINER_NAME}:{container_path}", host_path], check=True, capture_output=True)
        logger.info("Copied memory outbox successfully: duration_sec=%.3f", time.time() - t0)
        return True
    except Exception as e:
        logger.error("Failed to copy memory outbox from container: error=%s duration_sec=%.3f", str(e), time.time() - t0)
        return False

def restart_container():
    """Restart the agent container to clean up any leftover processes and zombies."""
    logger.info("Restarting container %s to ensure a clean slate...", config.CONTAINER_NAME)
    t0 = time.time()
    try:
        subprocess.run(["docker", "restart", config.CONTAINER_NAME], check=True, capture_output=True)
        logger.info("Container restarted successfully: container_name=%s duration_sec=%.3f", config.CONTAINER_NAME, time.time() - t0)
    except Exception as e:
        logger.error("Failed to restart container: container_name=%s error=%s duration_sec=%.3f", config.CONTAINER_NAME, str(e), time.time() - t0)
        # Fallback: try to start it if it was stopped
        t1 = time.time()
        try:
            subprocess.run(["docker", "start", config.CONTAINER_NAME], check=True, capture_output=True)
            logger.info("Fallback start of container succeeded: container_name=%s duration_sec=%.3f", config.CONTAINER_NAME, time.time() - t1)
        except Exception as start_err:
            logger.error("Failed to start container as fallback: container_name=%s error=%s duration_sec=%.3f", config.CONTAINER_NAME, str(start_err), time.time() - t1)

def run_agent(agent_id: str, prompt_file: str, branch_name: str, model_name: str = config.MODEL_GEMINI) -> subprocess.Popen:
    """Launch the agent's work cycle in a background process."""
    t0 = time.time()
    # Restart container before agent starts to guarantee clean environment
    restart_container()

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
        logger.error("Failed to prepare git branch: branch_name=%s error=%s duration_sec=%.3f", branch_name, err, time.time() - t0)
        raise RuntimeError(f"Git branch preparation failed: {err}")

    # Launch agy in the background
    agy_exec_cmd = f"""
    cd {config.CONTAINER_WORKSPACE} && \
    export PATH="/root/.local/bin:$PATH" && \
    /root/.local/bin/agy --model "{model_name}" --dangerously-skip-permissions --print-timeout 40m --print "Please read your instructions from /root/.agents/AGENTS.md carefully and fulfill your role completely. You are working in the repository /root/Stellar. CRITICAL: Every turn of your response MUST contain at least one tool call until your work is fully completed and you have opened the pull request. Do NOT write text-only thoughts or plan descriptions without accompanying tool calls, otherwise your execution will terminate prematurely. When your work is complete, verify it using your verify instructions, get code-reviewer approval, and then open a pull request using the github cli (gh pr create). CRITICAL: Do NOT checkout or create a new git branch. You are already placed on a dedicated branch for this run; make commits and open the PR directly from the current active branch."
    """
    
    logger.info("Starting agy run for agent: agent_id=%s branch=%s model=%s duration_sec=%.3f", agent_id, branch_name, model_name, time.time() - t0)
    
    proc = subprocess.Popen(
        ["docker", "exec", "-t", config.CONTAINER_NAME, "bash", "-c", agy_exec_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    return proc

def check_new_prs(branch_name: str) -> List[Dict[str, Any]]:
    """Query GitHub CLI inside container to find if any PR matches the branch."""
    t0 = time.time()
    cmd = f"gh pr list --repo {config.GITHUB_REPO} --head {branch_name} --state all --json number,url,state,title"
    rc, stdout, stderr = exec_in_container(cmd)
    duration = time.time() - t0
    if rc == 0:
        try:
            prs = json.loads(stdout)
            logger.info("Checked new PRs: branch_name=%s count=%d duration_sec=%.3f", branch_name, len(prs), duration)
            return prs
        except Exception as e:
            logger.error("Failed to parse PR JSON output: branch_name=%s error=%s duration_sec=%.3f output=%s", branch_name, str(e), duration, stdout)
            return []
    else:
        logger.error("Failed to fetch PR list: branch_name=%s error=%s duration_sec=%.3f", branch_name, stderr, duration)
        return []

def check_pr_status(pr_number: int) -> str:
    """Check the status of a specific PR."""
    t0 = time.time()
    cmd = f"gh pr view {pr_number} --repo {config.GITHUB_REPO} --json state"
    rc, stdout, stderr = exec_in_container(cmd)
    duration = time.time() - t0
    if rc == 0:
        try:
            data = json.loads(stdout)
            state = data.get("state", "OPEN")
            logger.info("Checked PR status: pr_number=%d state=%s duration_sec=%.3f", pr_number, state, duration)
            return state # OPEN, MERGED, CLOSED
        except Exception as e:
            logger.error("Failed to parse PR status: pr_number=%d error=%s duration_sec=%.3f output=%s", pr_number, str(e), duration, stdout)
            return "OPEN"
    else:
        logger.error("Failed to view PR: pr_number=%d error=%s duration_sec=%.3f", pr_number, stderr, duration)
        return "OPEN"

def get_agent_final_summary() -> Optional[str]:
    """Find the most recent conversation transcript in the container and extract the final message."""
    t0 = time.time()
    find_file_cmd = "ls -1t /root/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl 2>/dev/null | head -n 1"
    rc, stdout, stderr = exec_in_container(find_file_cmd)
    if rc != 0 or not stdout.strip():
        logger.info("No agent transcript found in container: duration_sec=%.3f", time.time() - t0)
        return None
        
    transcript_path = stdout.strip()
    
    # Read the transcript file from the container
    cat_cmd = f"cat {transcript_path} 2>/dev/null"
    rc, stdout, stderr = exec_in_container(cat_cmd)
    if rc != 0 or not stdout.strip():
        logger.warning("Could not read agent transcript from container: path=%s duration_sec=%.3f", transcript_path, time.time() - t0)
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
                    logger.info("Extracted agent final summary: path=%s length=%d duration_sec=%.3f", transcript_path, len(content), time.time() - t0)
                    return content
        except Exception as e:
            continue
            
    logger.warning("No valid PLANNER_RESPONSE summary found in transcript: path=%s duration_sec=%.3f", transcript_path, time.time() - t0)
    return None
