import os
import time
import json
import sqlite3
import shutil
import difflib
import logging
import threading
import docker
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List

# Constants
DATABASE_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stellar_local.db')
SANDBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sandbox_runs')

_healer_thread = None
_stop_event = threading.Event()

def get_db_conn():
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def get_working_api_key(model_name="gemini-3.5-flash"):
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
        # Fallback in case of import errors
        return os.getenv("PRIMARY_API_KEY")

def get_diff(old_content, new_content, filename):
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}"
    )
    return "".join(diff)

# Pydantic models for structured output
class EditBlock(BaseModel):
    search_text: str = Field(description="The exact block of text to find in the original file. Provide enough context lines to ensure uniqueness.")
    replace_text: str = Field(description="The new text to replace the search_text with.")

class FilePatch(BaseModel):
    file_path: str = Field(description="The relative path of the file to fix, e.g. 'app.py'")
    edits: List[EditBlock] = Field(default_factory=list, description="List of search/replace blocks to apply to the file. Use this for modifying existing files.")
    full_content: str = Field(default="", description="If creating a completely new file, provide its full content here instead of edits.")
    explanation: str = Field(description="A brief description of what was causing the bug and how this fixes it.")

class SelfHealingPatch(BaseModel):
    patches: List[FilePatch] = Field(description="The list of file patches to apply.")
    root_cause: str = Field(description="Root cause analysis of the bug.")

def heal_application(process_id, error_id, r_client):
    from app import logger, KEY_MANAGER
    
    lock_key = f"lock:sentinel:heal:{process_id}"
    # Acquire Redis lock with 5-minute TTL
    if not r_client.set(lock_key, "locked", ex=300, nx=True):
        logger.info(f"Self-healing already in progress for app {process_id}. Re-queueing task.")
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
        publish_log("info", "Sentinel healer activated. Inspecting database details...", stage="Initializing Healer")
        
        # Connect to DB
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

        # Connect to Docker
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

        # Ensure we have permissions to the host directory
        import subprocess
        try:
            subprocess.run(["sudo", "chown", "-R", "stellaradmin:www-data", host_dir], check=True, capture_output=True)
        except Exception as perm_err:
            logger.warning(f"Failed to chown host directory: {perm_err}")

        # Create workspace backup directory
        os.makedirs(SANDBOX_DIR, exist_ok=True)
        backup_dir = os.path.join(SANDBOX_DIR, f"backup_{process_id}_{error_id}")
        shutil.copytree(host_dir, backup_dir, dirs_exist_ok=True)
        publish_log("info", "Created temporary backup snapshot of the workspace.")

        # Read workspace files as context for Gemini
        workspace_context = ""
        for root, dirs, files in os.walk(host_dir):
            dirs[:] = [d for d in dirs if d not in ['.git', 'venv', '__pycache__', '.pytest_cache']]
            for file in files:
                ext = os.path.splitext(file)[1]
                if ext in ['.py', '.js', '.html', '.css', '.json', '.txt']:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, host_dir)
                    try:
                        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                            content = f.read()
                        workspace_context += f"\n--- File: {rel_path} ---\n{content}\n"
                    except Exception as fe:
                        logger.warning(f"Could not read {rel_path} for workspace context: {fe}")

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

        publish_log("info", "Consulting Gemini for code synthesis...", stage="Synthesizing Patch")

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
                logger.info(f"Attempting Gemini generation using key context: {masked}")
                # Mirror gemini_generate exactly: create fresh client AND fresh chat on every rotation
                client = genai.Client(api_key=k, http_options={'api_version': 'v1beta'})
                chat_config = types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=SelfHealingPatch,
                    temperature=0.1,
                    max_output_tokens=65536,
                )
                chat = client.chats.create(model="gemini-3.5-flash", config=chat_config)
                r = chat.send_message(user_prompt)

                candidate = r.candidates[0]
                full_text = "".join(
                    p.text for p in getattr(candidate.content, 'parts', [])
                    if getattr(p, 'text', None)
                )

                if not full_text:
                    raise ValueError("Empty response received from Gemini.")

                gemini_response = SelfHealingPatch.model_validate_json(full_text)
                logger.info(f"Gemini generation successful using key context: {masked}")
                break

            except Exception as ge:
                logger.error(f"Gemini API call failed with key context {masked}: {ge}")
                last_genai_error = ge
                err_str = str(ge).lower()
                if '429' in err_str or 'quota' in err_str or 'resource_exhausted' in err_str:
                    KEY_MANAGER.block_key(k, "gemini-3.5-flash", 60, "RPM")
                    current_key_index += 1
                    logger.warning(f"429 on key {masked}, rotating to next key ({current_key_index}/{len(active_keys)})")
                    time.sleep(0.5)
                elif '403' in err_str or 'permission_denied' in err_str or 'invalid' in err_str:
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
            
            # Prevent directory traversal
            abs_host_dir = os.path.abspath(host_dir)
            target_file_path = os.path.abspath(os.path.join(abs_host_dir, file_path))
            if not target_file_path.startswith(abs_host_dir):
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
        
        # Check syntax for Python/JS files inside the container
        for patch in gemini_response.patches:
            file_path = patch.file_path.strip().lstrip('/')
            if file_path.endswith('.py'):
                res = container.exec_run(f"python3 -m py_compile {file_path}", user='root')
                if res.exit_code != 0:
                    err_msg = res.output.decode('utf-8', 'replace')
                    raise ValueError(f"Syntax validation failed for Python file {file_path}:\n{err_msg}")
            elif file_path.endswith('.js'):
                res = container.exec_run(f"node --check {file_path}", user='root')
                if res.exit_code != 0:
                    err_msg = res.output.decode('utf-8', 'replace')
                    raise ValueError(f"Syntax validation failed for JS file {file_path}:\n{err_msg}")

        # Restart server process inside container
        publish_log("info", "Restarting application server inside container...")
        container.exec_run("python3 -c \"import os, signal; my_pid = os.getpid(); [os.kill(int(p), signal.SIGKILL) for p in os.listdir('/proc') if p.isdigit() and int(p) != my_pid and any(kw in open(f'/proc/{p}/cmdline').read('\x00') for kw in ['app.py', 'python', 'flask'])]\"", user='root')
        container.exec_run("pkill -9 python || true", user='root')
        container.exec_run("pkill -f 'python app.py' || true", user='root')
        time.sleep(1)
        
        container.exec_run(["sh", "-c", "python app.py > app.log 2>&1"], detach=True)

        # Health check
        is_ready = False
        status_code = 0
        for i in range(1, 11):
            time.sleep(1)
            try:
                exec_result = container.exec_run("curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/")
                if exec_result.exit_code == 0:
                    status_code = int(exec_result.output.decode().strip())
                    if 0 < status_code < 500:
                        is_ready = True
                        break
            except Exception as e:
                logger.warning(f"Health check execution warning (attempt {i}): {e}")

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
        logger.error(f"Healer failure: {heal_err}", exc_info=True)
        publish_log("info", f"Error during healing: {heal_err}. Rolling back to baseline state...")
        
        if backup_dir and host_dir and os.path.exists(backup_dir):
            try:
                # Fix permissions using sudo before restoring
                import subprocess
                try:
                    subprocess.run(["sudo", "chown", "-R", "stellaradmin:www-data", host_dir], check=True, capture_output=True)
                except Exception as perm_err:
                    logger.warning(f"Failed to chown host directory during rollback: {perm_err}")

                # Inode-safe contents restoration
                for item in os.listdir(host_dir):
                    item_path = os.path.join(host_dir, item)
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)

                for item in os.listdir(backup_dir):
                    s_path = os.path.join(backup_dir, item)
                    d_path = os.path.join(host_dir, item)
                    if os.path.isdir(s_path):
                        shutil.copytree(s_path, d_path, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s_path, d_path)
                        
                publish_log("info", "Original workspace files restored.")
                
                # Restart original application server in container
                if container:
                    container.exec_run("python3 -c \"import os, signal; my_pid = os.getpid(); [os.kill(int(p), signal.SIGKILL) for p in os.listdir('/proc') if p.isdigit() and int(p) != my_pid and any(kw in open(f'/proc/{p}/cmdline').read('\x00') for kw in ['app.py', 'python', 'flask'])]\"", user='root')
                    container.exec_run("pkill -9 python || true", user='root')
                    container.exec_run("pkill -f 'python app.py' || true", user='root')
                    time.sleep(1)
                    container.exec_run(["sh", "-c", "python app.py > app.log 2>&1"], detach=True)
                    publish_log("info", "Original application server restarted.")
            except Exception as restore_err:
                logger.error(f"Restore from backup failed: {restore_err}", exc_info=True)

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

def _healer_loop():
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
                        heal_application(process_id, error_id, r_client)
                    except Exception as e:
                        from app import logger
                        logger.error(f"Error executing self-healing for app {process_id}: {e}", exc_info=True)
        except Exception as queue_err:
            try:
                from app import logger
                logger.error(f"Error in sentinel healer loop: {queue_err}", exc_info=True)
            except Exception:
                pass
            time.sleep(2)

def start_sentinel_healer():
    global _healer_thread, _stop_event
    if _healer_thread is not None and _healer_thread.is_alive():
        return
    _stop_event.clear()
    _healer_thread = threading.Thread(target=_healer_loop, daemon=True)
    _healer_thread.start()

def stop_sentinel_healer():
    global _stop_event
    _stop_event.set()
