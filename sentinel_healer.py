"""
Sentinel Healer Module

This module implements the autonomous self-healing daemon for Stellar. It monitors the Redis healing
queue ('sentinel:queue') for errors reported by deployed user repositories. When a runtime exception
or compilation error is encountered by a user application, the Sentinel Healer:
1. Acquires a Redis lock for the corresponding application process.
2. Extracts error details from the local SQLite database.
3. Creates a workspace backup.
4. Synthesizes a corrective code patch using the Gemini API (gemini-3.5-flash) with structured outputs.
5. Applies the synthesized patch.
6. Performs syntax validation and health checks inside the application's Docker container.
7. Commits changes, updates database status, and falls back/rolls back if validation fails.
"""

import os
import time
import json
import sqlite3
import shutil
import difflib
import logging
import threading
# Inline imports are used for docker, genai, and types to speed up startup time.

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Constants
DATABASE_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stellar_local.db')
SANDBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sandbox_runs')

_healer_thread = None
_stop_event = threading.Event()

def get_db_conn():
    """
    Establish a connection to the SQLite local database.
    Sets the row factory to sqlite3.Row for dictionary-like access, enables WAL mode,
    and sets a busy timeout of 5 seconds to handle concurrent write contention.

    Returns:
        sqlite3.Connection: The database connection object.
    """
    t0 = time.time()
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    duration = time.time() - t0
    if duration > 0.05:
        logger.warning("Slow sentinel healer database connection duration_sec=%.3f", duration)
    return conn

def get_working_api_key(model_name="gemini-3.5-flash"):
    """
    Retrieve an active, unblocked Gemini API key from the primary and backup key list.
    Checks the key block manager to prevent using rate-limited or invalid keys.
    Falls back to the primary key if all keys are currently marked as blocked.

    Args:
        model_name (str): The name of the Gemini model to check block status for.

    Returns:
        str: A valid Gemini API key.
    """
    try:
        from app import PRIMARY_API_KEY, BACKUP_API_KEYS, KEY_MANAGER
        keys_to_try = [PRIMARY_API_KEY] + [bk for bk in BACKUP_API_KEYS if bk]
        for k in keys_to_try:
            if not k:
                continue
            is_blocked, _ = KEY_MANAGER.is_key_blocked(k, model_name)
            if not is_blocked:
                return k
        # Fallback to PRIMARY_API_KEY if all are blocked
        return PRIMARY_API_KEY
    except Exception as e:
        # DOUBLE-HANDLED FALLBACK: If the Flask app imports or KeyManager lookup fails (e.g., when run standalone or under specific testing frameworks), we fall back directly to reading the PRIMARY_API_KEY from environment variables.
        return os.getenv("PRIMARY_API_KEY")

def get_diff(old_content, new_content, filename):
    """
    Generate a unified diff showing changes between the original file content and the modified content.

    Args:
        old_content (str): The baseline content of the file.
        new_content (str): The proposed/modified content of the file.
        filename (str): The relative path/name of the file.

    Returns:
        str: The unified diff formatted as a string.
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}"
    )
    return "".join(diff)

def detect_startup_command(host_dir):
    """
    Analyze the files present in the host application directory to guess the appropriate startup command.
    Checks for:
    - server.js -> node server.js
    - package.json -> reads 'scripts.start', defaults to node server.js
    - app.py -> python app.py
    Falls back to python app.py if no matches are found.

    Args:
        host_dir (str): The directory containing user application source files.

    Returns:
        str: The command string to start the application (e.g., 'python app.py').
    """
    # Check if server.js exists
    if os.path.exists(os.path.join(host_dir, 'server.js')):
        return "node server.js"
    # Check if package.json exists
    if os.path.exists(os.path.join(host_dir, 'package.json')):
        try:
            with open(os.path.join(host_dir, 'package.json'), 'r') as f:
                data = json.load(f)
            if 'scripts' in data and 'start' in data['scripts']:
                return "npm start"
        except Exception:
            pass
        return "node server.js"
    # Check if app.py exists
    if os.path.exists(os.path.join(host_dir, 'app.py')):
        return "python app.py"
    # Fallback search
    try:
        files = os.listdir(host_dir)
        if 'app.py' in files:
            return "python app.py"
        if 'server.js' in files:
            return "node server.js"
    except Exception:
        pass
    return "python app.py"

def stop_application_server(container, cmd):
    """
    Stop the application server running inside the Docker container.
    Constructs and executes a safe python kill script to locate and SIGKILL running instances
    of the web server (avoiding killing the container's main shell or the execution hook itself),
    and issues broad pkill fallsbacks.

    Args:
        container (docker.models.containers.Container): The Docker container object.
        cmd (str): The startup command used to detect running process keywords.
    """
    t_stop = time.time()
    try:
        kws = ['node', 'npm'] if ('node' in cmd or 'npm' in cmd) else ['app.py', 'python', 'flask']
        kws_str = ", ".join([f"'{kw}'" for kw in kws])
        kill_script = (
            f"import os, signal; my_pid = os.getpid(); "
            f"[os.kill(int(p), signal.SIGKILL) for p in os.listdir('/proc') "
            f"if p.isdigit() and int(p) != my_pid and "
            f"any(kw in open(f'/proc/{{p}}/cmdline').read('\\x00') for kw in [{kws_str}])]"
        )
        container.exec_run(["python3", "-c", kill_script], user='root')
    except Exception as e:
        logger.warning("Process PID kill script failed container_id=%s cmd=%s error=%s", getattr(container, 'id', 'unknown'), cmd, e)
        
    # DOUBLE-HANDLED SERVER STOPPAGE: To ensure application servers are completely terminated without leaving orphaned zombie processes, we execute both direct signal 9 pkill by process class name (python, node) and pattern-matched pkill on the full startup command lines, ignoring any 'not found' errors.
    container.exec_run("pkill -9 python || true", user='root')
    container.exec_run("pkill -f 'python app.py' || true", user='root')
    container.exec_run("pkill -9 node || true", user='root')
    container.exec_run("pkill -f 'node server.js' || true", user='root')
    container.exec_run("pkill -f 'npm' || true", user='root')
    logger.info("stop_application_server completed container_id=%s cmd=%s duration_sec=%.2f", getattr(container, 'id', 'unknown'), cmd, time.time() - t_stop)

def heal_application(process_id, error_id, r_client):
    """
    Perform the core self-healing sequence for a user application.
    Orchestrates the entire recovery pipeline:
    - Acquires a Redis lock for the given process ID to avoid concurrent runs.
    - Updates application error status in SQLite.
    - Fetches the error context (affected file/line, stack trace).
    - Backs up the current container codebase directory.
    - Synthesizes patches via Gemini API.
    - Restarts the app container and runs health validation checks.
    - Finalizes state updates in DB/Redis or performs rollbacks on validation failures.

    Args:
        process_id (str): The unique process/subdomain ID of the user application.
        error_id (int): The database primary key ID of the sentinel_app_errors record.
        r_client (redis.StrictRedis): The Redis client instance used for locking and queue operations.
    """
    # Inline import of Pydantic and definition of schema models to speed up startup time
    from pydantic import BaseModel, Field
    from typing import List

    class EditBlock(BaseModel):
        """
        Represents a search-and-replace edit block targeting a specific section of a file.
        """
        search_text: str = Field(description="The exact block of text to find in the original file. Provide enough context lines to ensure uniqueness.")
        replace_text: str = Field(description="The new text to replace the search_text with.")

    class FilePatch(BaseModel):
        """
        Represents a set of edits or complete new content to apply to a specific repository file.
        """
        file_path: str = Field(description="The relative path of the file to fix, e.g. 'app.py'")
        edits: List[EditBlock] = Field(default_factory=list, description="List of search/replace blocks to apply to the file. Use this for modifying existing files.")
        full_content: str = Field(default="", description="If creating a completely new file, provide its full content here instead of edits.")
        explanation: str = Field(description="A brief description of what was causing the bug and how this fixes it.")

    class SelfHealingPatch(BaseModel):
        """
        Represents the full structured patch generated by the Gemini model for self-healing.
        """
        patches: List[FilePatch] = Field(description="The list of file patches to apply.")
        root_cause: str = Field(description="Root cause analysis of the bug.")

    from app import KEY_MANAGER
    logger.info("Initiating self-healing workflow process_id=%s error_id=%s", process_id, error_id)
    
    lock_key = f"lock:sentinel:heal:{process_id}"
    # DOUBLE-HANDLED CONCURRENCY CHECK: Uses a Redis-based set-nx lock as the primary guard against concurrent healing operations. If lock acquisition fails (indicating another worker or process is already active), the task is pushed back to the 'sentinel:queue' with a 2-second delay fallback.
    if not r_client.set(lock_key, "locked", ex=300, nx=True):
        logger.info("Self-healing already in progress for app process_id=%s. Re-queueing task.", process_id)
        time.sleep(2)
        payload = json.dumps({"error_id": error_id, "process_id": process_id})
        r_client.lpush("sentinel:queue", payload)
        return

    # Set status key in Redis
    r_client.set(f"sentinel:healing:{process_id}", "Initializing Healer")

    db = None
    backup_dir = None
    host_dir = None
    container = None
    patches_applied = []
    
    log_history_key = f"sentinel:log_history:{process_id}"
    r_client.delete(log_history_key)  # Clear any stale history from previous healing runs

    def publish_log(event, message, stage=None):
        """
        Publish diagnostic logs to Redis channel and history log list for real-time SSE streaming.

        Args:
            event (str): The classification of log event (e.g. 'info', 'healed', 'failed').
            message (str): The descriptive log message.
            stage (str, optional): The current phase of the healing workflow to update in Redis.
        """
        payload = {"event": event, "message": message}
        if stage:
            payload["stage"] = stage
            r_client.set(f"sentinel:healing:{process_id}", stage)
        encoded = json.dumps(payload)
        # Publish live to any connected SSE clients
        r_client.publish(f"sentinel:logs:{process_id}", encoded)
        # Also append to history list so late-connecting clients can replay
        r_client.rpush(log_history_key, encoded)
        r_client.expire(log_history_key, 300)  # Keep for 5 minutes

    try:
        heal_start_time = time.time()
        publish_log("info", "Sentinel healer activated. Inspecting database details...", stage="Initializing Healer")
        
        # Connect to DB
        t_db = time.time()
        db = get_db_conn()
        
        # Fetch the error details
        cursor = db.execute("SELECT * FROM sentinel_app_errors WHERE id = ?", (error_id,))
        error_row = cursor.fetchone()
        if not error_row:
            raise ValueError(f"Error {error_id} not found in database.")
            
        error_type = error_row['error_type']
        error_message = error_row['error_message']
        stack_trace = error_row['stack_trace']
        affected_file = error_row['affected_file']
        affected_line = error_row['affected_line']
        
        db.execute("UPDATE sentinel_app_errors SET status = 'fixing' WHERE id = ?", (error_id,))
        db.commit()
        logger.info("Database initialization and error fetch completed error_id=%s duration_sec=%.2f", error_id, time.time() - t_db)

        # Connect to Docker
        t_docker = time.time()
        import docker
        d_client = docker.from_env()
        container_name = f"stellar-repo-{process_id}"
        try:
            container = d_client.containers.get(container_name)
        except docker.errors.NotFound:
            raise ValueError(f"Docker container '{container_name}' not found.")
            
        if container.status != "running":
            raise ValueError(f"Docker container '{container_name}' is not running.")
            
        # Find mount directory on host
        mounts = container.attrs.get('Mounts', [])
        for m in mounts:
            if m['Destination'] == '/app':
                host_dir = m['Source']
                break
        if not host_dir:
            raise ValueError(f"Could not find host directory mount for container {container_name}")
        logger.info("Docker container check and host mount resolution completed container_name=%s duration_sec=%.2f", container_name, time.time() - t_docker)

        # Resolve dynamic port and startup command
        port = 5000
        try:
            cursor = db.execute("SELECT files_snapshot FROM repo_history WHERE process_id = ?", (process_id,))
            repo_row = cursor.fetchone()
            if repo_row and repo_row['files_snapshot']:
                snap = json.loads(repo_row['files_snapshot'])
                port = int(snap.get('port', 5000))
        except Exception as e:
            logger.warning("Failed to fetch port for process process_id=%s error=%s", process_id, e)
            
        # Ensure we have permissions to the host directory before detecting startup command
        import subprocess
        t_chown = time.time()
        try:
            subprocess.run(["sudo", "chown", "-R", "stellaradmin:www-data", host_dir], check=True, capture_output=True)
            logger.info("Chown host directory completed host_dir=%s duration_sec=%.2f", host_dir, time.time() - t_chown)
        except Exception as perm_err:
            logger.warning("Failed to chown host directory host_dir=%s error=%s duration_sec=%.2f", host_dir, perm_err, time.time() - t_chown)

        startup_cmd = detect_startup_command(host_dir)
        logger.info("Detected app startup command process_id=%s startup_cmd=%s port=%d", process_id, startup_cmd, port)

        # Define directories to exclude from context and backups
        exclude_dirs = {
            '.git', 'venv', '__pycache__', '.pytest_cache', 'node_modules',
            'bower_components', '.next', 'dist', 'build', 'out', 'coverage',
            '.gemini', 'outputs', 'tmp', 'temp', 'sandbox_runs', '.antigravitycli'
        }

        # Create workspace backup directory (ignoring heavy/git/dependency folders)
        os.makedirs(SANDBOX_DIR, exist_ok=True)
        backup_dir = os.path.join(SANDBOX_DIR, f"backup_{process_id}_{error_id}")
        t_backup = time.time()
        shutil.copytree(
            host_dir, backup_dir,
            ignore=shutil.ignore_patterns(*exclude_dirs),
            dirs_exist_ok=True
        )
        logger.info("Created temporary backup snapshot backup_dir=%s host_dir=%s duration_sec=%.2f", backup_dir, host_dir, time.time() - t_backup)
        publish_log("info", "Created temporary backup snapshot of the workspace.")
        
        t_walk = time.time()
        workspace_context = ""
        total_chars = 0
        MAX_TOTAL_CHARS = 500000 # Keep context safe from 250k token limit (~1 token ≈ 3-4 chars)

        for root, dirs, files in os.walk(host_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            if total_chars >= MAX_TOTAL_CHARS:
                break
            for file in files:
                if total_chars >= MAX_TOTAL_CHARS:
                    break
                ext = os.path.splitext(file)[1]
                if ext in ['.py', '.js', '.html', '.css', '.json', '.txt']:
                    full_path = os.path.join(root, file)
                    # Sentinel Security Fix: Verify resolved path doesn't point outside host_dir (symlink traversal mitigation)
                    real_base = os.path.realpath(host_dir)
                    real_full_path = os.path.realpath(full_path)
                    if os.path.commonpath([real_base, real_full_path]) != real_base:
                        logger.warning("Skipping symlink pointing outside sandbox: %s", full_path)
                        continue
                    rel_path = os.path.relpath(full_path, host_dir)
                    try:
                        # Skip files larger than 100KB to prevent quota blowout
                        if os.path.getsize(full_path) > 100000:
                            logger.info("Skipping large file in context rel_path=%s size=%d", rel_path, os.path.getsize(full_path))
                            continue
                        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                            content = f.read()
                        if total_chars + len(content) > MAX_TOTAL_CHARS:
                            allowed_len = MAX_TOTAL_CHARS - total_chars
                            content = content[:allowed_len] + "\n... [TRUNCATED DUE TO SIZE LIMIT] ..."
                        workspace_context += f"\n--- File: {rel_path} ---\n{content}\n"
                        total_chars += len(content)
                    except Exception as fe:
                        logger.warning("Could not read file for workspace context rel_path=%s error=%s", rel_path, fe)
        logger.info("Workspace context gathering completed process_id=%s files_size_chars=%d duration_sec=%.2f", process_id, total_chars, time.time() - t_walk)

        # Construct system prompt and user prompt
        system_prompt = (
            "You are Stellar Sentinel, an autonomous self-healing agent running on Stellar.\n"
            "Your objective is to fix code errors in user repositories.\n"
            "Analyze the workspace context files, standard logs, and the reported exception.\n"
            "Produce corrected replacements for only the files that contain bugs or require modifications.\n\n"
            "You MUST respond with a JSON object containing the patches according to the schema provided.\n"
            "For existing files, you MUST use the `edits` array to perform search-and-replace instead of rewriting the whole file. Provide enough `search_text` context to ensure a unique match.\n"
            "Write highly precise fixes targeting the exact lines causing the error."
        )

        user_prompt = f"""
=== REPORTED ERROR ===
Type: {error_type}
Message: {error_message}
Stack Trace/Logs:
{stack_trace}
Affected File: {affected_file}
Affected Line: {affected_line}

=== WORKSPACE FILES ===
{workspace_context}

Please provide the corrected file contents to heal the application.
"""

        publish_log("info", "Consulting Stellar for code synthesis...", stage="Synthesizing Patch")

        from app import PRIMARY_API_KEY, BACKUP_API_KEYS
        raw_keys = [PRIMARY_API_KEY] + [bk for bk in BACKUP_API_KEYS if bk]
        all_keys = [k for k in dict.fromkeys(raw_keys) if k]

        # Mirror gemini_generate: filter blocked keys but fall back to all if every key is blocked
        active_keys = [k for k in all_keys if not KEY_MANAGER.is_key_blocked(k, "gemini-3.5-flash")[0]]
        if not active_keys:
            logger.warning("All keys are blocked for gemini-3.5-flash — falling back to trying all keys anyway.")
            active_keys = all_keys

        gemini_response = None
        last_genai_error = None
        current_key_index = 0

        while current_key_index < len(active_keys):
            k = active_keys[current_key_index]
            masked = k[:4] + "..." + k[-4:] if len(k) > 8 else k
            try:
                logger.info("Attempting Gemini generation using key context masked=%s", masked)
                # Mirror gemini_generate exactly: create fresh client AND fresh chat on every rotation
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=k, http_options={'api_version': 'v1beta'})
                chat_config = types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=SelfHealingPatch,
                    temperature=0.1,
                    max_output_tokens=65536,
                )
                chat = client.chats.create(model="gemini-3.5-flash", config=chat_config)
                t0 = time.time()
                r = chat.send_message(user_prompt)
                duration = time.time() - t0
                usage = getattr(r, 'usage_metadata', None)
                prompt_tokens = getattr(usage, 'prompt_token_count', 0) if usage else 0
                candidates_tokens = getattr(usage, 'candidates_token_count', 0) if usage else 0
                logger.info("Gemini API call completed model=%s duration_sec=%.2f purpose=sentinel_healer prompt_tokens=%d candidates_tokens=%d attempt=%d", "gemini-3.5-flash", duration, prompt_tokens, candidates_tokens, current_key_index + 1)

                candidate = r.candidates[0]
                full_text = "".join(
                    p.text for p in getattr(candidate.content, 'parts', [])
                    if getattr(p, 'text', None)
                )

                if not full_text:
                    raise ValueError("Empty response received from Gemini.")

                gemini_response = SelfHealingPatch.model_validate_json(full_text)
                logger.info("Gemini generation successful using key context masked=%s", masked)
                break

            except Exception as ge:
                logger.error("Gemini API call failed with key context masked=%s error=%s", masked, ge, exc_info=True)
                last_genai_error = ge
                err_str = str(ge).lower()
                is_invalid_key = ('api_key_invalid' in err_str or 'api key not valid' in err_str or 'invalid_api_key' in err_str or 'key expired' in err_str or '401' in err_str or 'unauthenticated' in err_str)
                if '429' in err_str or 'quota' in err_str or 'resource_exhausted' in err_str:
                    KEY_MANAGER.block_key(k, "gemini-3.5-flash", 60, "RPM")
                    current_key_index += 1
                    logger.warning("429 on key masked=%s rotating to next key (%d/%d)", masked, current_key_index, len(active_keys))
                    time.sleep(0.5)
                elif is_invalid_key:
                    KEY_MANAGER.block_key(k, "gemini-3.5-flash", 3600, "INVALID")
                    current_key_index += 1
                else:
                    current_key_index += 1

        if not gemini_response:
            raise ValueError(f"Failed to generate patch with all available Gemini keys. Last error: {last_genai_error}")


        publish_log("info", f"Root Cause Analysis: {gemini_response.root_cause}")

        # Apply the patches
        for patch in gemini_response.patches:
            file_path = patch.file_path.strip().lstrip('/')
            
            # Sentinel Security Fix: Prevent directory/symlink traversal during patch writing
            real_host_dir = os.path.realpath(host_dir)
            target_file_path = os.path.abspath(os.path.join(real_host_dir, file_path))
            real_target_path = os.path.realpath(target_file_path)
            parent_dir = os.path.dirname(real_target_path)
            if (os.path.commonpath([real_host_dir, real_target_path]) != real_host_dir and
                os.path.commonpath([real_host_dir, parent_dir]) != real_host_dir):
                raise ValueError(f"Security error: path traversal attempt detected: {file_path}")

            # Read old content if exists
            old_content = ""
            if os.path.exists(target_file_path):
                with open(target_file_path, 'r', encoding='utf-8', errors='replace') as f:
                    old_content = f.read()

            new_content = old_content
            if patch.full_content:
                # New file or explicit full rewrite
                new_content = patch.full_content
            else:
                # Apply each search/replace edit block
                for edit in patch.edits:
                    if edit.search_text in new_content:
                        new_content = new_content.replace(edit.search_text, edit.replace_text, 1)
                    else:
                        publish_log("info", f"Warning: Could not find search text in {file_path} — edit block skipped.")

            # Calculate diff
            diff_str = get_diff(old_content, new_content, file_path)

            patches_applied.append({
                "file_path": file_path,
                "diff": diff_str,
                "explanation": patch.explanation,
                "content": new_content
            })

            # Write the new content to file
            os.makedirs(os.path.dirname(target_file_path), exist_ok=True)
            with open(target_file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)

            publish_log("info", f"Applied patch to workspace file: {file_path}")

        # Phase 5: Pre-flight Sandbox Validation (Syntax & Startup)
        publish_log("info", "Starting syntax and runtime validation checks...", stage="Validating Patch")
        t_syntax = time.time()
        # Check syntax for Python/JS files inside the container
        for patch in gemini_response.patches:
            file_path = patch.file_path.strip().lstrip('/')
            if file_path.endswith('.py'):
                # Sentinel Security Fix: Use a list of arguments to avoid shell command injection
                res = container.exec_run(["python3", "-m", "py_compile", file_path], user='root')
                if res.exit_code != 0:
                    err_msg = res.output.decode('utf-8', 'replace')
                    raise ValueError(f"Syntax validation failed for Python file {file_path}:\n{err_msg}")
            elif file_path.endswith('.js'):
                # Sentinel Security Fix: Use a list of arguments to avoid shell command injection
                res = container.exec_run(["node", "--check", file_path], user='root')
                if res.exit_code != 0:
                    err_msg = res.output.decode('utf-8', 'replace')
                    raise ValueError(f"Syntax validation failed for JS file {file_path}:\n{err_msg}")
        logger.info("Syntax validation checks completed process_id=%s duration_sec=%.2f", process_id, time.time() - t_syntax)

        # Restart server process inside container
        publish_log("info", "Restarting application server inside container...")
        t_restart = time.time()
        stop_application_server(container, startup_cmd)
        time.sleep(1)
        
        container.exec_run(["sh", "-c", f"{startup_cmd} > app.log 2>&1"], detach=True)
        logger.info("Application server process started inside container process_id=%s startup_cmd=%s duration_sec=%.2f", process_id, startup_cmd, time.time() - t_restart)

        # Health check
        is_ready = False
        status_code = 0
        t_health = time.time()
        for i in range(1, 11):
            time.sleep(1)
            try:
                exec_result = container.exec_run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/")
                if exec_result.exit_code == 0:
                    status_code = int(exec_result.output.decode().strip())
                    if 0 < status_code < 500:
                        is_ready = True
                        break
            except Exception as e:
                logger.warning("Health check execution warning process_id=%s attempt=%d error=%s", process_id, i, e)

        logger.info("Health check validation completed process_id=%s is_ready=%s status_code=%d duration_sec=%.2f", process_id, is_ready, status_code, time.time() - t_health)

        if not is_ready:
            app_log = ""
            log_res = container.exec_run("cat app.log")
            if log_res.exit_code == 0:
                app_log = log_res.output.decode('utf-8', 'replace')
            raise ValueError(f"Server health check failed with HTTP {status_code}.\n--- App logs:\n{app_log}")

        # Success: Commit & DB Updates
        publish_log("info", "Validation successful. Recording patch snapshots...")
        
        # Record each patch in DB
        for pa in patches_applied:
            db.execute(
                "INSERT INTO sentinel_app_patches (error_id, patch_diff, status) VALUES (?, ?, 'applied')",
                (error_id, pa['diff'])
            )
            
        # Update error status to healed
        db.execute("UPDATE sentinel_app_errors SET status = 'healed' WHERE id = ?", (error_id,))
        
        # Update files_snapshot in repo_history
        cursor = db.execute("SELECT id, files_snapshot FROM repo_history WHERE process_id = ? ORDER BY id DESC LIMIT 1", (process_id,))
        hist_row = cursor.fetchone()
        if hist_row:
            hist_id = hist_row['id']
            try:
                snapshot = json.loads(hist_row['files_snapshot']) if hist_row['files_snapshot'] else {}
            except Exception:
                snapshot = {}
            for pa in patches_applied:
                snapshot[pa['file_path']] = pa['content']
            db.execute("UPDATE repo_history SET files_snapshot = ? WHERE id = ?", (json.dumps(snapshot), hist_id))
            
        db.commit()
        
        # Publish final healed state
        publish_log("healed", "Application healed successfully! Restoring subdomain access.", stage="Application Healed")

    except Exception as heal_err:
        # DOUBLE-HANDLED ERROR RESTORATION: In case of any execution or validation error during healing, the system immediately invokes a rollback sequence: it chowns the directory to safe defaults, removes any new/modified files, copies clean files from the backup, restarts the original application server, and updates the database to mark the patch as failed.
        logger.error("Healer failure error=%s", heal_err, exc_info=True)
        publish_log("info", f"Error during healing: {heal_err}. Rolling back to baseline state...")
        
        if backup_dir and host_dir and os.path.exists(backup_dir):
            t_rollback = time.time()
            try:
                # Fix permissions using sudo before restoring
                import subprocess
                try:
                    subprocess.run(["sudo", "chown", "-R", "stellaradmin:www-data", host_dir], check=True, capture_output=True)
                except Exception as perm_err:
                    logger.warning("Failed to chown host directory during rollback host_dir=%s error=%s", host_dir, perm_err)

                # Inode-safe contents restoration (preserving heavy excluded dirs)
                for item in os.listdir(host_dir):
                    if item in exclude_dirs:
                        continue
                    item_path = os.path.join(host_dir, item)
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)

                for item in os.listdir(backup_dir):
                    if item in exclude_dirs:
                        continue
                    s_path = os.path.join(backup_dir, item)
                    d_path = os.path.join(host_dir, item)
                    if os.path.isdir(s_path):
                        shutil.copytree(s_path, d_path, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s_path, d_path)
                        
                publish_log("info", "Original workspace files restored.")
                
                # Restart original application server in container
                if container:
                    stop_application_server(container, startup_cmd)
                    time.sleep(1)
                    container.exec_run(["sh", "-c", f"{startup_cmd} > app.log 2>&1"], detach=True)
                    publish_log("info", "Original application server restarted.")
                logger.info("Rollback to backup snapshot completed process_id=%s duration_sec=%.2f", process_id, time.time() - t_rollback)
            except Exception as restore_err:
                logger.error("Restore from backup failed process_id=%s error=%s duration_sec=%.2f", process_id, restore_err, time.time() - t_rollback, exc_info=True)

        if db:
            # Record failed patch
            for pa in patches_applied:
                db.execute(
                    "INSERT INTO sentinel_app_patches (error_id, patch_diff, status) VALUES (?, ?, 'failed_test')",
                    (error_id, pa['diff'])
                )
            db.execute("UPDATE sentinel_app_errors SET status = 'open' WHERE id = ?", (error_id,))
            db.commit()

        publish_log("failed", f"Healing execution failed: {heal_err}", stage="Healing Suspended")

    finally:
        # Delete backup dir
        if backup_dir and os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
            
        if db:
            db.close()
            
        r_client.delete(f"sentinel:healing:{process_id}")
        r_client.delete(lock_key)
        
        try:
            duration = time.time() - heal_start_time
            logger.info("Sentinel self-healing process completed for process_id=%s error_id=%s duration_sec=%.2f", process_id, error_id, duration)
        except Exception:
            pass

def _healer_loop():
    """
    Main loop for the Sentinel Healer background worker.
    Runs continuously in a background daemon thread until stop event is signaled.
    Blocks for 1 second at a time while popping jobs from the 'sentinel:queue' Redis list.
    When a job is dequeued, it delegates execution to the `heal_application` helper.
    """
    import redis
    # Connect to Redis
    r_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)
    
    while not _stop_event.is_set():
        try:
            # Block for 1 second on redis pop
            val = r_client.brpop("sentinel:queue", timeout=1)
            if val:
                payload_str = val[1]
                payload = json.loads(payload_str)
                process_id = payload.get("process_id")
                error_id = payload.get("error_id")
                if process_id and error_id:
                    try:
                        from app import thread_local_ctx
                        thread_local_ctx.request_id = f"heal-{process_id[:8]}"
                    except Exception:
                        pass
                    logger.info("Dequeued self-healing job process_id=%s error_id=%s", process_id, error_id)
                    try:
                        heal_application(process_id, error_id, r_client)
                    except Exception as e:
                        logger.error("Error executing self-healing for app process_id=%s: %s", process_id, e, exc_info=True)
                    finally:
                        try:
                            thread_local_ctx.request_id = None
                        except Exception:
                            pass
        except Exception as queue_err:
            try:
                logger.error("Error in sentinel healer loop: %s", queue_err, exc_info=True)
            except Exception:
                pass
            time.sleep(2)

def start_sentinel_healer():
    """
    Start the Sentinel self-healing background thread if it is not already active.
    Initializes a new daemon thread targetting `_healer_loop`.
    """
    global _healer_thread, _stop_event
    if _healer_thread is not None and _healer_thread.is_alive():
        return
    logger.info("Starting Sentinel self-healing daemon thread...")
    _stop_event.clear()
    _healer_thread = threading.Thread(target=_healer_loop, daemon=True)
    _healer_thread.start()

def stop_sentinel_healer():
    """
    Signal the Sentinel self-healing background thread to exit gracefully.
    Sets the stop event which causes the `_healer_loop` to exit.
    """
    global _stop_event
    logger.info("Stopping Sentinel self-healing daemon thread...")
    _stop_event.set()

def __getattr__(name):
    """
    Lazy module attribute resolution to support mock patching in unit tests.

    Caches the imported module into globals() after the first access so that
    Python's attribute lookup finds it directly on subsequent calls without
    re-entering __getattr__.
    """
    if name == 'docker':
        import docker
        globals()['docker'] = docker  # Cache: bypass __getattr__ on next access
        return docker
    if name == 'genai':
        from google import genai
        globals()['genai'] = genai  # Cache: bypass __getattr__ on next access
        return genai
    if name == 'types':
        from google.genai import types
        globals()['types'] = types  # Cache: bypass __getattr__ on next access
        return types
    raise AttributeError(f"module {__name__} has no attribute {name}")
