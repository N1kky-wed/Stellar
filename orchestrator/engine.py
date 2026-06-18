# engine.py
"""
Execution pipeline engine for Stellar agent orchestrator.
Manages scheduling pipeline agents, processing their outcomes, doing memory summarizations,
and handling API usage quota checking and service updates.
"""
import re
import sys
import time
import logging
import os
import json
from datetime import datetime, timedelta
import pytz
import subprocess
from typing import Optional, List, Dict, Any

import orchestrator.config as config
from orchestrator.state import StateDB
import orchestrator.container as container
from orchestrator.memory import MemoryDB

logger = logging.getLogger("stellar-orchestrator")
IST = pytz.timezone(config.TIMEZONE)

class OrchestratorEngine:
    """
    The main execution engine for the Stellar Agent Orchestrator.
    Handles recovery, runs execution loops, schedules agents, processes memories,
    performs memory summarization, and manages quota-based model routing.
    """
    def __init__(self):
        """
        Initializes the orchestrator engine, recovers active runs if recovered from crash,
        and restores stored model cooldown status.
        """
        self.state_db = StateDB(config.DB_PATH)
        self.memory_db = MemoryDB(config.MEMORY_DB_PATH)
        self.current_process: Optional[subprocess.Popen] = None
        self.current_agent_id: Optional[str] = None
        self.current_run_id: Optional[int] = None
        self.agent_start_time: Optional[datetime] = None
        self.branch_name: Optional[str] = None
        self.prompt_file: Optional[str] = None
        # Quota cooldowns (global and model-specific)
        self.quota_cooldown_until: Optional[datetime] = None
        self.gemini_cooldown_until: Optional[datetime] = None
        self.claude_cooldown_until: Optional[datetime] = None
        self.current_model: Optional[str] = None

        # Recover from previous crash/restart if there's a running run in database
        self._recover_state()

        # Restore persisted cooldown (survives service restarts)
        self._restore_cooldown_from_db()

    def _recover_state(self):
        """
        Recovers the active run state on orchestrator startup if a run was interrupted mid-execution.
        """
        current = self.state_db.get_current_run()
        if current:
            logger.info("Recovering active run state run_id=%s agent_id=%s branch_name=%s", current['id'], current['agent_id'], current['branch_name'])
            self.current_agent_id = current['agent_id']
            self.current_run_id = current['id']
            self.branch_name = current['branch_name']
            try:
                self.current_model = current['model']
            except Exception:
                self.current_model = None
            
            # Find the prompt file from pipeline config
            for a in config.AGENT_PIPELINE:
                if a['id'] == self.current_agent_id:
                    self.prompt_file = a['prompt_file']
                    break
            
            # Since the orchestrator restarted, the background Popen handle is lost.
            # We can check if docker exec command for agy is still running inside container.
            # Let's inspect active exec processes. If not running, we mark it as FAILED (or let it timeout).
            rc, stdout, stderr = container.exec_in_container("ps aux | grep -i agy | grep -v grep")
            if rc == 0 and "agy" in stdout:
                logger.info("Associated agy process is still active inside container container_name=%s", config.CONTAINER_NAME)
                # We can't poll a non-existent Popen, but we can periodically check the container process list.
                # However, for simplicity, we will wrap the check using a dummy process or let the watchdog handle it
                # if it hangs, or check process presence inside self._check_running_agent.
                # Let's mock a process handle that checks process list on poll()
                class RecoveredProcess:
                    """
                    Mock process handle for an agent run recovered after orchestrator restart.
                    """
                    def poll(self):
                        """
                        Checks if the recovered agent process is still running inside the container.

                        Returns:
                            Optional[int]: 0 if completed/stopped, None if still running.
                        """
                        inner_rc, inner_out, _ = container.exec_in_container("ps aux | grep -i agy | grep -v grep")
                        if inner_rc != 0 or "agy" not in inner_out:
                            return 0 # Completed/stopped
                        return None # Still running
                    def kill(self):
                        """
                        Kills the recovered agent process inside the container.
                        """
                        container.exec_in_container("pkill -f agy")
                self.current_process = RecoveredProcess()
                self.agent_start_time = datetime.fromisoformat(current['started_at'])
            else:
                logger.warning("No active agy process found in container for recovered run marking_failed=true run_id=%s", self.current_run_id)
                now_str = datetime.now(IST).isoformat()
                self.state_db.fail_run(self.current_run_id, now_str, "Orchestrator restarted and container process was not found.")
                container.unload_agent_prompt()
                self._clear_active_run()

    def _clear_active_run(self):
        """
        Clears the in-memory trackers for the current active run.
        """
        self.current_process = None
        self.current_agent_id = None
        self.current_run_id = None
        self.agent_start_time = None
        self.branch_name = None
        self.prompt_file = None
        self.current_model = None

    def run(self):
        """
        Starts the main orchestrator polling loop.
        """
        logger.info("Stellar Agent Orchestrator Engine Started.")
        while True:
            try:
                self._tick()
            except Exception as e:
                logger.error("Error in orchestrator engine tick: %s", e, exc_info=True)
            
            if self.current_process:
                time.sleep(2)
            else:
                time.sleep(config.PR_CHECK_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Quota / Rate-limit helpers
    # ------------------------------------------------------------------

    def _restore_cooldown_from_db(self):
        """
        Restores stored model or global quota cooldown limits from the database.
        """
        """On startup, restore any persisted model/quota cooldowns so restarts don't bypass them."""
        # 1. Restore Gemini cooldown
        stored_gemini = self.state_db.get_state("gemini_cooldown_until")
        if stored_gemini:
            try:
                cooldown_dt = datetime.fromisoformat(stored_gemini)
                now = datetime.now(IST)
                if cooldown_dt > now:
                    self.gemini_cooldown_until = cooldown_dt
                    remaining = (cooldown_dt - now).total_seconds() / 60
                    logger.warning("[STARTUP] Restored Gemini quota cooldown remaining_min=%.1f cooldown_until=%s", remaining, cooldown_dt.isoformat())
                else:
                    self.state_db.set_state("gemini_cooldown_until", "")
            except Exception as e:
                logger.error("Failed to restore Gemini cooldown: %s", e)

        # 2. Restore Claude cooldown
        stored_claude = self.state_db.get_state("claude_cooldown_until")
        if stored_claude:
            try:
                cooldown_dt = datetime.fromisoformat(stored_claude)
                now = datetime.now(IST)
                if cooldown_dt > now:
                    self.claude_cooldown_until = cooldown_dt
                    remaining = (cooldown_dt - now).total_seconds() / 60
                    logger.warning("[STARTUP] Restored Claude quota cooldown remaining_min=%.1f cooldown_until=%s", remaining, cooldown_dt.isoformat())
                else:
                    self.state_db.set_state("claude_cooldown_until", "")
            except Exception as e:
                logger.error("Failed to restore Claude cooldown: %s", e)

        # 3. Restore global cooldown
        stored = self.state_db.get_state("quota_cooldown_until")
        if stored:
            try:
                cooldown_dt = datetime.fromisoformat(stored)
                now = datetime.now(IST)
                if cooldown_dt > now:
                    self.quota_cooldown_until = cooldown_dt
                    remaining = (cooldown_dt - now).total_seconds() / 60
                    logger.warning(
                        "[STARTUP] Restored global quota cooldown from DB remaining_min=%.1f cooldown_until=%s",
                        remaining, cooldown_dt.isoformat()
                    )
                else:
                    logger.info("[STARTUP] Stored global quota cooldown has already expired clearing=true")
                    self.state_db.set_state("quota_cooldown_until", "")
            except Exception as e:
                logger.error("Failed to restore cooldown from DB error=%s", str(e))

    def _get_agy_log_tail(self, start_time: datetime) -> str:
        """
        Reads the last 100 lines of the agent execution log file inside the container.
        Args:
            start_time (datetime): Start time of the agent run to verify log freshness.
        Returns:
            str: Log tail contents.
        """
        """Read the last 100 lines of the current run's log file from inside the container.
        Ensures that we do not perform dirty reads from previous runs' logs.
        """
        # 1. Resolve symlink to get actual log file path and its mtime
        cmd = "readlink -f /root/.gemini/antigravity-cli/cli.log"
        rc, log_path, _ = container.exec_in_container(cmd, timeout=5)
        if rc != 0 or not log_path.strip():
            return ""
        log_path = log_path.strip()

        # 2. Check the mtime of the log file
        cmd_mtime = f"stat -c %Y {log_path}"
        rc_mtime, mtime_str, _ = container.exec_in_container(cmd_mtime, timeout=5)
        if rc_mtime != 0 or not mtime_str.strip():
            return ""
        
        try:
            log_mtime = float(mtime_str.strip())
        except ValueError:
            return ""
            
        # Convert start_time to epoch time
        start_epoch = start_time.timestamp()
        
        # If the log file was modified before the agent started, it's a stale log file
        # We allow a 5-second buffer for clock skew
        if log_mtime < start_epoch - 5:
            logger.info("Log file modification time older than agent start time log_path=%s log_mtime=%.1f start_epoch=%.1f action=ignore", log_path, log_mtime, start_epoch)
            return ""

        # 3. Read the last 100 lines of this file
        cmd_cat = f"tail -n 100 {log_path}"
        rc_cat, stdout, _ = container.exec_in_container(cmd_cat, timeout=10)
        return stdout if rc_cat == 0 else ""

    def _check_quota_error(self, log_tail: str) -> Optional[datetime]:
        """
        Scans the log tail for quota exhaustion (429) errors.
        Args:
            log_tail (str): Log tail content to check.
        Returns:
            Optional[datetime]: Recovery timestamp if quota error is detected, otherwise None.
        """
        """
        Scan the agy log tail for RESOURCE_EXHAUSTED / 429 errors.
        If found, parse 'Resets in XhYmZs' and return when quota will be restored.
        Returns None if no quota error found.
        """
        if "RESOURCE_EXHAUSTED" not in log_tail and not re.search(r"\b429\b", log_tail):
            return None

        logger.warning("Quota error detected in agy log run_id=%s agent_id=%s", self.current_run_id, self.current_agent_id)

        # Format is compact: 'Resets in 3h39m35s' (no spaces between parts)
        # Also handles partial: '45m12s', '2h30m', '90s'
        match = re.search(r"Resets in ((?:\d+h)?(?:\d+m)?(?:\d+s)?)", log_tail)
        now = datetime.now(IST)
        if match and match.group(1):
            raw = match.group(1)  # e.g. '3h39m35s'
            h = int(re.search(r'(\d+)h', raw).group(1)) if 'h' in raw else 0
            m = int(re.search(r'(\d+)m', raw).group(1)) if 'm' in raw else 0
            s = int(re.search(r'(\d+)s', raw).group(1)) if 's' in raw else 0
            delta = timedelta(hours=h, minutes=m, seconds=s)
            # Add 2-minute buffer so we don't retry right at the boundary
            reset_at = now + delta + timedelta(minutes=2)
            logger.warning("Quota resets cooldown_duration_sec=%d cooldown_until=%s", delta.total_seconds(), reset_at.isoformat())
            return reset_at
        else:
            # Couldn't parse reset time — default to 4-hour cooldown
            fallback = now + timedelta(hours=4)
            logger.warning("Could not parse reset time from log defaulting_cooldown=true cooldown_until=%s", fallback.isoformat())
            return fallback

    def _update_average_quota_usage(self, run_id: Optional[int], model: Optional[str]):
        """
        Calculates and updates average weekly quota percentage costs for a model.
        Args:
            run_id (Optional[int]): Run database identifier.
            model (Optional[str]): Model name.
        """
        if not run_id or not model:
            logger.warning("Skipping average quota update: run_id or model is missing. run_id=%s, model=%s", run_id, model)
            return

        # 1. Retrieve the starting quota percent for this run
        w_start = None
        try:
            with self.state_db._get_conn() as conn:
                row = conn.execute("SELECT quota_start_percent FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
                if row:
                    w_start = row["quota_start_percent"]
        except Exception as e:
            logger.error("Failed to retrieve starting quota from DB: %s", e)
            return

        if w_start is None:
            logger.warning("Could not calculate average quota cost. w_start is None for run_id=%d", run_id)
            return

        # 2. Fetch current (ending) quota percent
        try:
            from orchestrator.quota import fetch_quota_data_from_container, parse_quota_text
            raw_text = fetch_quota_data_from_container()
            quota_data = parse_quota_text(raw_text)
            
            # Save the new quota data as the latest
            quota_data["last_updated"] = datetime.now(IST).isoformat()
            self.state_db.set_state("quota_data", json.dumps(quota_data))
            
            model_key = "gemini" if model == config.MODEL_GEMINI else "claude"
            w_end = quota_data.get(model_key, {}).get("weekly_percent")
        except Exception as e:
            logger.error("Failed to fetch ending quota data: %s", e)
            return

        if w_end is None:
            logger.warning("Could not calculate average quota cost. w_end is None for model=%s", model)
            return

        # 3. Calculate cost
        cost = w_start - w_end
        if cost < 0:
            # Maybe a reset occurred or account switched, ignore this run's cost
            logger.info("Negative cost calculated (w_start=%.2f, w_end=%.2f). A reset or account swap likely occurred. Skipping avg update.", w_start, w_end)
            return
            
        # Update the quota_cost column for this run
        try:
            with self.state_db._get_conn() as conn:
                conn.execute("UPDATE agent_runs SET quota_cost = ? WHERE id = ?", (cost, run_id))
                conn.commit()
            logger.info("Saved quota cost of %.2f%% for run_id=%d", cost, run_id)
        except Exception as e:
            logger.error("Failed to update quota_cost in agent_runs table: %s", e)
            
        # 4. Update the running average in the database
        avg_key = f"{model_key}_avg_cost"
        count_key = f"{model_key}_runs_count"
        
        try:
            with self.state_db._get_conn() as conn:
                # Query all runs for this model from June 14, 2026 02:55 PM onwards
                row_stats = conn.execute("""
                    SELECT AVG(quota_cost), COUNT(id) 
                    FROM agent_runs 
                    WHERE model LIKE ? AND started_at >= '2026-06-14T14:55:00' AND quota_cost IS NOT NULL
                """, (f"%{model_key}%",)).fetchone()
                
                if row_stats and row_stats[1] > 0:
                    new_avg = row_stats[0]
                    new_count = row_stats[1]
                    self.state_db.set_state(avg_key, str(new_avg))
                    self.state_db.set_state(count_key, str(new_count))
                    logger.info("Recalculated running average for %s: new_avg=%.4f%%, total_runs=%d", model_key, new_avg, new_count)
                else:
                    # Fallback
                    self.state_db.set_state(avg_key, str(cost))
                    self.state_db.set_state(count_key, "1")
        except Exception as e:
            logger.error("Failed to update running average: %s", e)

    def _is_in_cooldown(self, now: datetime) -> bool:
        """
        Returns True if the engine is currently in a global/model-specific quota cooldown state.
        Args:
            now (datetime): Current time.
        Returns:
            bool: True if in cooldown, False otherwise.
        """
        """Return True if the engine is currently in a global quota cooldown (i.e. both models are exhausted/throttled)."""
        # Clear expired model cooldowns
        if self.gemini_cooldown_until and now >= self.gemini_cooldown_until:
            logger.info("[COOLDOWN] Gemini quota cooldown lifted.")
            self.gemini_cooldown_until = None
            self.state_db.set_state("gemini_cooldown_until", "")
            
        if self.claude_cooldown_until and now >= self.claude_cooldown_until:
            logger.info("[COOLDOWN] Claude quota cooldown lifted.")
            self.claude_cooldown_until = None
            self.state_db.set_state("claude_cooldown_until", "")

        # Check cached database quota data for throttling
        gemini_throttled = False
        claude_throttled = False
        gemini_recovery_time = None
        claude_recovery_time = None
        
        quota_json = self.state_db.get_state("quota_data")
        if quota_json:
            try:
                quota_dict = json.loads(quota_json)
                gemini_info = quota_dict.get('gemini', {})
                claude_info = quota_dict.get('claude', {})
                
                gemini_status = gemini_info.get('status')
                claude_status = claude_info.get('status')
                
                # Determine how much time has passed since the quota was fetched
                last_updated_str = quota_dict.get('last_updated')
                elapsed_hours = 0.0
                if last_updated_str:
                    try:
                        last_updated_dt = datetime.fromisoformat(last_updated_str)
                        elapsed_hours = max(0.0, (now - last_updated_dt).total_seconds() / 3600.0)
                    except Exception:
                        pass
                
                pacing_multiplier = 24.0 / config.DAILY_PACING_TARGET_PERCENT
                if gemini_status in ('Throttled', 'Exhausted'):
                    gemini_throttled = True
                    g_pct = gemini_info.get('weekly_percent', 100.0)
                    g_ref = gemini_info.get('weekly_refreshes_in_hours', 0.0)
                    g_wait = max(0.0, (g_ref - elapsed_hours) - pacing_multiplier * g_pct)
                    if g_wait > 0:
                        gemini_recovery_time = now + timedelta(hours=g_wait)
                
                if claude_status in ('Throttled', 'Exhausted'):
                    claude_throttled = True
                    c_pct = claude_info.get('weekly_percent', 100.0)
                    c_ref = claude_info.get('weekly_refreshes_in_hours', 0.0)
                    c_wait = max(0.0, (c_ref - elapsed_hours) - pacing_multiplier * c_pct)
                    if c_wait > 0:
                        claude_recovery_time = now + timedelta(hours=c_wait)
            except Exception as e:
                logger.error("Error parsing quota data in cooldown check: %s", e)

        g_cooldown = self.gemini_cooldown_until or (gemini_recovery_time if gemini_throttled else None)
        c_cooldown = self.claude_cooldown_until or (claude_recovery_time if claude_throttled else None)

        # If a model is free, we are NOT in global cooldown
        if not g_cooldown or not c_cooldown:
            # Clear global cooldown if it was set
            if self.quota_cooldown_until:
                self.quota_cooldown_until = None
                self.state_db.set_state("quota_cooldown_until", "")
            return False

        # If both models are in cooldown, the engine must sleep
        # We set the global quota_cooldown_until to the earliest of the two resets
        earliest_recovery = min(g_cooldown, c_cooldown)
        if not self.quota_cooldown_until or self.quota_cooldown_until != earliest_recovery:
            self.quota_cooldown_until = earliest_recovery
            self.state_db.set_state("quota_cooldown_until", earliest_recovery.isoformat())

        if now < self.quota_cooldown_until:
            remaining = (self.quota_cooldown_until - now).total_seconds() / 60
            logger.info(
                "[COOLDOWN] Both Gemini and Claude quotas are exhausted/throttled. remaining_min=%.1f cooldown_until=%s action=skip_tick",
                remaining, self.quota_cooldown_until.isoformat()
            )
            return True

        # Global cooldown lifted
        logger.info("[COOLDOWN] Global quota cooldown lifted resuming_scheduling=true")
        self.quota_cooldown_until = None
        self.state_db.set_state("quota_cooldown_until", "")
        return False

    def _tick(self):
        """
        Executes a single polling tick of the orchestrator scheduler.
        """
        now = datetime.now(IST)
        
        # Check and execute memory summarization if due
        self._check_memory_summarization(now)
        
        # Verify container is running
        if not container.is_container_running():
            logger.error("Docker container %s is not running! Skipping tick.", config.CONTAINER_NAME)
            return

        # 0. Check for any agent that was deferred due to an orchestrator restart
        pending_immediate = self.state_db.get_state("pending_immediate_agent")
        if pending_immediate and not self._is_in_cooldown(now):
            self.state_db.set_state("pending_immediate_agent", "")
            for agent in config.AGENT_PIPELINE:
                if agent['id'] == pending_immediate:
                    logger.info("Starting pending immediate agent %s after orchestrator restart", agent['name'])
                    self._start_agent(agent)
                    return

        # 1. Always monitor a running agent (even during cooldown — shouldn't happen, but be safe)
        if self.current_process:
            self._check_running_agent(now)
            return

        # 1.5 Check for CI/CD failures on pending PRs and trigger Mercury (bypasses cooldown)
        mercury_triggered = self._check_and_trigger_mercury(now)
        if mercury_triggered:
            return

        # 2. Always check for merged PRs and trigger service reloads — even during cooldown.
        #    Agent fixes should be deployed immediately regardless of quota state.
        next_agent = self._check_for_merge_trigger()
        if next_agent:
            if not self._is_in_cooldown(now):
                logger.info("PR MERGE EVENT DETECTED: Starting next agent %s immediately!", next_agent['name'])
                self._start_agent(next_agent)
                return
            else:
                logger.info("PR MERGE EVENT DETECTED during cooldown: Deferring next agent %s to DB state.", next_agent['name'])
                self.state_db.set_state("pending_immediate_agent", next_agent['id'])
                return

        # 3. Quota cooldown guard — don't schedule new agents until quota refreshes
        if self._is_in_cooldown(now):
            return

    def _drain_stdout(self):
        """
        Reads and logs stdout lines non-blockingly from the current background process.
        """
        if not (hasattr(self.current_process, "stdout") and self.current_process.stdout):
            return
        import os
        import fcntl
        fd = self.current_process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        if not (fl & os.O_NONBLOCK):
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        try:
            while True:
                line = self.current_process.stdout.readline()
                if not line:
                    break
                logger.info("[%s] %s", self.current_agent_id, line.strip())
        except BlockingIOError:
            pass
        except Exception as e:
            logger.error("Error reading process stdout: %s", e)

    def _get_summary(self) -> Optional[str]:
        """
        Extracts the final summary of the agent run from the container.
        Returns:
            Optional[str]: Summary text if retrieved, otherwise None.
        """
        try:
            return container.get_agent_final_summary()
        except Exception as e:
            logger.error("Error extracting agent final summary: %s", e)
            return None

    def _check_running_agent(self, now: datetime):
        """
        Monitors the active agent process, checks execution outcomes, handles PRs/errors.
        Args:
            now (datetime): Current check timestamp.
        """
        # Read some stdout/stderr to prevent buffer block if using pipes
        self._drain_stdout()
        
        retcode = self.current_process.poll()
        if retcode is not None:
            # Drain one last time to get final output
            self._drain_stdout()
            
            logger.info("Agent process finished agent_id=%s run_id=%s exit_code=%d", self.current_agent_id, self.current_run_id, retcode)
            self._update_average_quota_usage(self.current_run_id, self.current_model)
            now_str = now.isoformat()

            # --- Quota check: read cli.log BEFORE deciding success/failure ---
            log_tail = self._get_agy_log_tail(self.agent_start_time)
            quota_reset_at = self._check_quota_error(log_tail)

            if quota_reset_at:
                current_model = self.current_model or config.MODEL_GEMINI
                logger.warning("Agent terminated by quota exhaustion model=%s agent_id=%s run_id=%s cooldown_until=%s", current_model, self.current_agent_id, self.current_run_id, quota_reset_at.isoformat())
                
                # Set model-specific cooldown in database
                if current_model == config.MODEL_GEMINI:
                    self.gemini_cooldown_until = quota_reset_at
                    self.state_db.set_state("gemini_cooldown_until", quota_reset_at.isoformat())
                else:
                    self.claude_cooldown_until = quota_reset_at
                    self.state_db.set_state("claude_cooldown_until", quota_reset_at.isoformat())
                
                self.state_db.fail_run(
                    self.current_run_id, now_str,
                    f"RESOURCE_EXHAUSTED (429): quota resets at {quota_reset_at.isoformat()}",
                    summary_message=f"Agent aborted due to API quota exhaustion on model: {current_model}."
                )
                container.unload_agent_prompt()
                
                # Find current agent config to check for fallback retry
                current_agent = None
                for a in config.AGENT_PIPELINE:
                    if a['id'] == self.current_agent_id:
                        current_agent = a
                        break
                
                # If Gemini failed and Claude is available, retry immediately
                if current_agent and current_model == config.MODEL_GEMINI and not self.claude_cooldown_until:
                    logger.warning("Gemini quota exhausted. Retrying agent %s immediately using Claude Sonnet fallback...", self.current_agent_id)
                    self._clear_active_run()
                    self._start_agent(current_agent)
                    return
                
                # Otherwise, clear active run (if both failed, engine goes to global cooldown sleep)
                self._clear_active_run()
                return
            
            if retcode == 0:
                summary = self._get_summary()
                # Query container for any newly created PRs on this branch
                prs = container.check_new_prs(self.branch_name)
                if not prs and summary:
                    # Fallback: parse PR number/URL from the agent's summary message
                    import re
                    match = re.search(r"github\.com/[^/]+/[^/]+/pull/(\d+)", summary)
                    if match:
                        pr_num = int(match.group(1))
                        pr_url = f"https://github.com/{config.GITHUB_REPO}/pull/{pr_num}"
                        pr_state = container.check_pr_status(pr_num)
                        prs = [{"number": pr_num, "url": pr_url, "state": pr_state}]
                        logger.info("Fallback PR detected from agent summary pr_num=%d pr_url=%s pr_state=%s", pr_num, pr_url, pr_state)
                
                # Process agent memory outbox
                self._process_agent_memory_outbox(self.current_agent_id, self.current_run_id, summary)

                if prs:
                    # Found PR
                    pr = prs[0]
                    pr_num = pr.get("number")
                    pr_url = pr.get("url")
                    pr_state = pr.get("state")
                    logger.info("Detected PR created by agent pr_num=%d pr_url=%s pr_state=%s", pr_num, pr_url, pr_state)
                    
                    db_pr_status = 'PENDING'
                    if pr_state == 'MERGED':
                        db_pr_status = 'MERGED'
                    elif pr_state == 'CLOSED':
                        db_pr_status = 'CLOSED'
                        
                    self.state_db.complete_run(self.current_run_id, now_str, pr_number=pr_num, pr_url=pr_url, pr_status=db_pr_status, summary_message=summary)

                    # Add system message to group chat
                    self.memory_db.add_message(
                        channel="group",
                        sender_id="orchestrator",
                        content=f"✅ Agent **{self.current_agent_id.capitalize()}** completed successfully! (PR #{pr_num})\nPull Request: {pr_url}",
                        message_type="system"
                    )

                    # If already merged (fast auto-merge), trigger reload and start the next agent immediately
                    if db_pr_status == 'MERGED':
                        logger.info("PR already merged triggering immediate service reload pr_num=%d", pr_num)
                        restart_orchestrator = self._pull_and_reload_services(pr_num)
                        
                        next_agent = self._get_next_pipeline_agent(self.current_agent_id)
                        if restart_orchestrator:
                            if next_agent:
                                logger.info("Orchestrator restart scheduled. Deferring next agent %s to DB state.", next_agent['name'])
                                self.state_db.set_state("pending_immediate_agent", next_agent['id'])
                            logger.info("Exiting current orchestrator process to allow restart.")
                            sys.exit(0)
                        elif next_agent:
                            logger.info("PR already merged: Starting next agent %s immediately!", next_agent['name'])
                            self._start_agent(next_agent)
                            return
                else:
                    logger.warning("Agent completed but no PR was detected branch_name=%s", self.branch_name)
                    self.state_db.complete_run(self.current_run_id, now_str, pr_status='NONE', summary_message=summary)

                    # Add system message to group chat
                    self.memory_db.add_message(
                        channel="group",
                        sender_id="orchestrator",
                        content=f"✅ Agent **{self.current_agent_id.capitalize()}** completed successfully!",
                        message_type="system"
                    )
            else:
                if log_tail:
                    logger.warning("Agent run failed: agent_id=%s run_id=%s exit_code=%d\nLast log lines:\n%s", 
                                   self.current_agent_id, self.current_run_id, retcode, log_tail)
                else:
                    logger.warning("Agent run failed (no log tail available): agent_id=%s run_id=%s exit_code=%d", 
                                   self.current_agent_id, self.current_run_id, retcode)
                self.state_db.fail_run(self.current_run_id, now_str, f"Process exited with non-zero code {retcode}.", summary_message=f"Agent process exited with non-zero code {retcode}.")
                
                # Add failure system message
                self.memory_db.add_message(
                    channel="group",
                    sender_id="orchestrator",
                    content=f"❌ Agent **{self.current_agent_id.capitalize()}** run failed.\nError: Process exited with non-zero code {retcode}.",
                    message_type="system"
                )

            container.unload_agent_prompt()
            self._clear_active_run()
            return

        # Watchdog timeout check
        elapsed_minutes = (now - self.agent_start_time).total_seconds() / 60
        if elapsed_minutes > config.MAX_AGENT_RUNTIME_MINUTES:
            logger.warning("Agent exceeded max runtime killing_agent=true agent_id=%s run_id=%s max_runtime_min=%d elapsed_min=%.1f", self.current_agent_id, self.current_run_id, config.MAX_AGENT_RUNTIME_MINUTES, elapsed_minutes)
            self._handle_timeout(now)

    def _handle_timeout(self, now: datetime):
        """
        Aborts an agent execution when it exceeds runtime limits.
        Args:
            now (datetime): Interruption timestamp.
        """
        log_tail = self._get_agy_log_tail(self.agent_start_time)
        if log_tail:
            logger.warning("Agent run timed out: agent_id=%s run_id=%s\nLast log lines:\n%s", 
                           self.current_agent_id, self.current_run_id, log_tail)
        else:
            logger.warning("Agent run timed out (no log tail available): agent_id=%s run_id=%s", 
                           self.current_agent_id, self.current_run_id)

        try:
            self.current_process.kill()
        except Exception as e:
            logger.error("Error killing agent process: %s", e)
        
        # Kill any runaway agy inside container
        container.exec_in_container("pkill -f agy")
        
        self._update_average_quota_usage(self.current_run_id, self.current_model)
        
        now_str = now.isoformat()
        self.state_db.timeout_run(self.current_run_id, now_str, summary_message="Agent execution timed out after exceeding limits.")
        
        # Add timeout system message
        self.memory_db.add_message(
            channel="group",
            sender_id="orchestrator",
            content=f"⚠️ Agent **{self.current_agent_id.capitalize()}** run timed out after exceeding limits.",
            message_type="system"
        )
        
        container.unload_agent_prompt()
        self._clear_active_run()

    def _check_for_merge_trigger(self) -> Optional[Dict[str, Any]]:
        """
        Checks if open agent PRs have been merged to trigger restarts/deployments.
        Returns:
            Optional[Dict[str, Any]]: Next pipeline agent config to launch.
        """
        """Checks if the PR of the most recently run agent was merged."""
        pending_runs = self.state_db.get_pending_prs()
        for run in pending_runs:
            pr_num = run['pr_number']
            current_status = container.check_pr_status(pr_num)
            
            if current_status == 'MERGED':
                logger.info("PR merged pr_num=%d agent_id=%s", pr_num, run['agent_id'])
                
                # Auto pull and reload affected services
                restart_orchestrator = self._pull_and_reload_services(pr_num)
                
                # Get the NEXT agent in pipeline
                next_agent = self._get_next_pipeline_agent(run['agent_id'])
                
                if restart_orchestrator:
                    if next_agent:
                        logger.info("Orchestrator restart scheduled. Deferring next agent %s to DB state.", next_agent['name'])
                        self.state_db.set_state("pending_immediate_agent", next_agent['id'])
                    
                    self.state_db.update_pr_status(run['id'], 'MERGED')
                    logger.info("Exiting current orchestrator process to allow restart.")
                    sys.exit(0)
                
                self.state_db.update_pr_status(run['id'], 'MERGED')
                return next_agent
            elif current_status == 'CLOSED':
                logger.info("PR closed without merging pr_num=%d agent_id=%s", pr_num, run['agent_id'])
                self.state_db.update_pr_status(run['id'], 'CLOSED')
                
        return None

    def _pull_and_reload_services(self, pr_num: int) -> bool:
        """
        Pulls merge changes from main on host, updates packages, reloads systemctl services.
        Args:
            pr_num (int): Merged pull request number.
        Returns:
            bool: True if the orchestrator service itself needs a restart.
        """
        t_start = time.time()
        logger.info("PR merged starting host service update pr_num=%d", pr_num)
        restart_orchestrator = False
        try:
            repo = "/home/stellaradmin/my_app"

            # 1. Ensure we're on main (orchestrator may have left the repo on an agent branch)
            t0 = time.time()
            checkout_res = subprocess.run(
                ["sudo", "-u", "stellaradmin", "git", "checkout", "main"],
                cwd=repo, capture_output=True, text=True
            )
            if checkout_res.returncode != 0:
                logger.warning("git checkout main failed error=%s duration_sec=%.3f", checkout_res.stderr.strip(), time.time() - t0)
            else:
                logger.info("git checkout main completed duration_sec=%.3f", time.time() - t0)

            # 2. Pull as stellaradmin — they own the SSH key
            t0 = time.time()
            pull_res = subprocess.run(
                ["sudo", "-u", "stellaradmin", "git", "pull"],
                cwd=repo, capture_output=True, text=True, check=True
            )
            logger.info("git pull completed output=%s duration_sec=%.3f", pull_res.stdout.strip(), time.time() - t0)

            # 3. Get list of files modified in the merge commit
            t0 = time.time()
            diff_res = subprocess.run(
                ["sudo", "-u", "stellaradmin", "git", "diff", "HEAD~1", "HEAD", "--name-only"],
                cwd=repo, capture_output=True, text=True, check=True
            )
            files = [f.strip() for f in diff_res.stdout.strip().split('\n') if f.strip()]
            logger.info("Files modified in merge commit files=%s duration_sec=%.3f", str(files), time.time() - t0)

            reload_stellar = False
            restart_ssh = False
            update_packages = False

            for file in files:
                if file == "requirements.txt":
                    update_packages = True
                    restart_orchestrator = True
                    reload_stellar = True
                    restart_ssh = True
                elif file.startswith("orchestrator/") or file == "deploy/stellar_orchestrator.service":
                    restart_orchestrator = True
                elif file == "ssh_gateway.py":
                    restart_ssh = True
                elif file in ["app.py", "sentinel_healer.py", "webscrapper.py", "agent_tools.py"] or file.startswith("templates/") or file.startswith("static/"):
                    reload_stellar = True

            if update_packages:
                logger.info("requirements.txt changed. Installing package updates in host venv...")
                t0 = time.time()
                try:
                    subprocess.run([
                        "sudo", "-u", "stellaradmin",
                        "/home/stellaradmin/my_app/venv/bin/pip", "install",
                        "-r", "/home/stellaradmin/my_app/requirements.txt"
                    ], check=True, capture_output=True, text=True)
                    logger.info("Package updates installed successfully duration_sec=%.3f", time.time() - t0)
                except subprocess.CalledProcessError as cpe:
                    logger.error("Failed to install packages in host venv: %s duration_sec=%.3f", cpe.stderr.strip(), time.time() - t0)

            if reload_stellar:
                t0 = time.time()
                logger.info("Auto-reloading stellar.service")
                subprocess.run(["sudo", "systemctl", "reload", "stellar"], check=True)
                logger.info("Auto-reloaded stellar.service duration_sec=%.3f", time.time() - t0)

            if restart_ssh:
                t0 = time.time()
                logger.info("Auto-restarting stellar-ssh.service")
                subprocess.run(["sudo", "systemctl", "restart", "stellar-ssh"], check=True)
                logger.info("Auto-restarted stellar-ssh.service duration_sec=%.3f", time.time() - t0)

            if restart_orchestrator:
                logger.info("Auto-restarting stellar_orchestrator.service")
                # Run detached so it doesn't kill this process before finishing the tick
                subprocess.Popen(["sudo", "systemctl", "restart", "stellar_orchestrator"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        except Exception as e:
            logger.error("Failed to auto-pull or reload services for PR pr_num=%d error=%s duration_sec=%.3f", pr_num, str(e), time.time() - t_start, exc_info=True)
            
        logger.info("Host service update completed pr_num=%d total_duration_sec=%.3f", pr_num, time.time() - t_start)
        return restart_orchestrator


    def _get_next_pipeline_agent(self, current_agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Finds the next pipeline agent config following the finished agent ID.
        Args:
            current_agent_id (str): Completed agent ID.
        Returns:
            Optional[Dict[str, Any]]: Next agent configuration dict.
        """
        for i, agent in enumerate(config.AGENT_PIPELINE):
            if agent['id'] == current_agent_id:
                for offset in range(1, len(config.AGENT_PIPELINE) + 1):
                    next_idx = (i + offset) % len(config.AGENT_PIPELINE)
                    candidate = config.AGENT_PIPELINE[next_idx]
                    if candidate['id'] != "mercury":
                        return candidate
        return None

    def _start_agent(self, agent: Dict[str, Any]):
        """
        Launches an agent run by initializing git checkout, database entries, and executing agy.
        Args:
            agent (Dict[str, Any]): Agent config dict.
        """
        self.current_agent_id = agent['id']
        self.prompt_file = agent['prompt_file']
        
        now = datetime.now(IST)
        date_str = now.strftime("%Y-%m-%d-%H%M")
        self.branch_name = f"{config.GITHUB_BRANCH_PREFIX}{self.current_agent_id}/{date_str}"
        self.agent_start_time = now
        
        # Determine active model based on quota states
        active_model = self._get_active_model(now)
        if not active_model:
            logger.warning("No healthy models available for run (both Gemini and Claude are throttled/exhausted). Deferring agent %s.", self.current_agent_id)
            self.state_db.set_state("pending_immediate_agent", self.current_agent_id)
            self._clear_active_run()
            return
            
        self.current_model = active_model
        
        # Get start quota percentage — prefer a live fetch, fall back to cache.
        # This prevents recording 100.0 as the starting point when the cache is stale.
        quota_start_percent = None
        model_key_start = "gemini" if active_model == config.MODEL_GEMINI else "claude"
        try:
            from orchestrator.quota import fetch_quota_data_from_container, parse_quota_text
            raw_start = fetch_quota_data_from_container()
            fresh_start = parse_quota_text(raw_start)
            # Only trust the live value if it parsed successfully (not Error/Unknown)
            live_status = fresh_start.get(model_key_start, {}).get("status", "Unknown")
            if live_status not in ("Error", "Unknown"):
                quota_start_percent = fresh_start.get(model_key_start, {}).get("weekly_percent")
                # Persist the fresh snapshot so later cost calc uses the same baseline
                fresh_start["last_updated"] = datetime.now(IST).isoformat()
                self.state_db.set_state("quota_data", json.dumps(fresh_start))
                logger.info("Live quota_start_percent for %s: %.2f%%", active_model, quota_start_percent)
            else:
                logger.warning("Live quota parse returned status=%s. Falling back to DB cache for start percent.", live_status)
                raise ValueError("Live parse gave non-healthy status")
        except Exception as e:
            logger.warning("Live quota fetch for start percent failed: %s. Trying DB cache.", e)
            try:
                quota_json = self.state_db.get_state("quota_data")
                if quota_json:
                    quota_dict = json.loads(quota_json)
                    cached_status = quota_dict.get(model_key_start, {}).get("status", "Unknown")
                    cached_pct = quota_dict.get(model_key_start, {}).get("weekly_percent")
                    # Reject only if the status flag signals a parse error — a genuine
                    # fresh-week 100% has status='Healthy' or 'Throttled', not 'Error'.
                    if cached_status not in ("Error", "Unknown") and cached_pct is not None:
                        quota_start_percent = cached_pct
                        logger.info("Using cached quota_start_percent for %s: %.2f%%", active_model, quota_start_percent)
                    else:
                        logger.warning("Cached quota_data is errored/unknown (status=%s). quota_start_percent will be NULL.", cached_status)
            except Exception as cache_err:
                logger.error("Failed to extract starting quota from cache: %s", cache_err)

        # Register in database
        self.current_run_id = self.state_db.start_run(
            agent_id=self.current_agent_id,
            branch_name=self.branch_name,
            started_at=now.isoformat(),
            quota_start_percent=quota_start_percent,
            model=self.current_model
        )
        
        try:
            # Prepare and load memory context into container
            self._prepare_and_load_memory_context(self.current_agent_id)
            
            self.current_process = container.run_agent(
                agent_id=self.current_agent_id,
                prompt_file=self.prompt_file,
                branch_name=self.branch_name,
                model_name=self.current_model
            )
        except Exception as e:
            logger.error("Failed to start agent agent_id=%s run_id=%s error=%s", self.current_agent_id, self.current_run_id, str(e))
            now_str = datetime.now(IST).isoformat()
            self.state_db.fail_run(self.current_run_id, now_str, str(e), summary_message=f"Failed to start agent: {e}")
            container.unload_agent_prompt()
            self._clear_active_run()

    def _check_and_trigger_mercury(self, now: datetime) -> bool:
        """
        Checks pending PRs for CI/CD failures or merge conflicts, and launches Mercury to heal.
        Bypasses quota cooldown checks.
        Returns:
            bool: True if Mercury was started, False otherwise.
        """
        pending_runs = self.state_db.get_pending_prs()
        for run in pending_runs:
            pr_num = run['pr_number']
            if not pr_num:
                continue

            # Check CI status of this PR
            try:
                ci_info = container.check_pr_ci_status(pr_num)
            except Exception as e:
                logger.error("Failed to check CI/CD status for PR #%d: %s", pr_num, e)
                continue

            status_type = ci_info['status']
            head_sha = ci_info.get('head_sha')

            if status_type == 'conflict':
                if not head_sha:
                    continue
                # For conflicts, the state key uses head_sha to prevent double triggers
                state_key = f"mercury_healed_conflict_{head_sha}"
                if self.state_db.get_state(state_key) == "triggered":
                    continue

                # Trigger Mercury!
                logger.info("MERGE CONFLICT DETECTED: PR #%d has merge conflicts (head commit: %s). Launching Mercury to rebase!", pr_num, head_sha)
                
                # Mark this run as triggered
                self.state_db.set_state(state_key, "triggered")

                # Post a system message in the chat
                self.memory_db.add_message(
                    channel="group",
                    sender_id="orchestrator",
                    content=f"🚨 **Merge conflict detected** for PR #{pr_num} (branch: `{run['branch_name']}`).\n"
                            f"Starting **Mercury (Reliability Engineer)** to perform git rebase and resolve conflicts. 🔀",
                    message_type="system"
                )

                # Launch Mercury to resolve conflict
                self._start_mercury(run['branch_name'], pr_num, conflict=True, model_name=run['model'])
                return True

            elif status_type == 'failure':
                run_id = ci_info['run_id']
                if not run_id:
                    logger.warning("PR #%d failed CI/CD, but no run ID could be extracted. Output: %s", pr_num, ci_info['raw_output'])
                    continue

                # Check if we already triggered Mercury for this run_id
                state_key = f"mercury_healed_run_{run_id}"
                if self.state_db.get_state(state_key) == "triggered":
                    continue

                # Trigger Mercury!
                logger.info("CI/CD FAILURE DETECTED: PR #%d failed CI/CD on run %s. Launching Mercury to heal!", pr_num, run_id)
                
                # Retrieve the failed run log/trace
                error_trace = None
                try:
                    error_trace = container.get_run_failed_log(run_id)
                except Exception as e:
                    logger.error("Failed to fetch run logs for run_id=%s: %s", run_id, e)
                
                if not error_trace:
                    error_trace = "Failed to retrieve logs. Please run tests or compile locally to diagnose."

                # Mark this run as triggered to avoid double triggers
                self.state_db.set_state(state_key, "triggered")

                # Post a system message in the chat
                self.memory_db.add_message(
                    channel="group",
                    sender_id="orchestrator",
                    content=f"🚨 **CI/CD build failed** for PR #{pr_num} (branch: `{run['branch_name']}`).\n"
                            f"Starting **Mercury (Reliability Engineer)** to diagnose and heal the failure. 🩺",
                    message_type="system"
                )

                # Launch Mercury on the same branch
                self._start_mercury(run['branch_name'], pr_num, error_trace=error_trace, model_name=run['model'])
                return True

        return False

    def _start_mercury(self, branch_name: str, pr_number: int, error_trace: Optional[str] = None, conflict: bool = False, model_name: Optional[str] = None):
        """
        Launches Mercury agent run to heal a failed CI/CD pipeline or resolve conflicts on a branch.
        """
        self.current_agent_id = "mercury"
        self.branch_name = branch_name
        self.current_model = model_name or config.MODEL_GEMINI
        self.prompt_file = "mercury_temp.md"
        self.agent_start_time = datetime.now(IST)

        logger.info("Mercury: Preparing execution on branch=%s model=%s type=%s", branch_name, self.current_model, "conflict" if conflict else "failure")

        # 1. Read base prompt from mercury.md
        base_prompt_path = os.path.join(config.HOST_AGENTS_DIR, "mercury.md")
        base_prompt = ""
        if os.path.exists(base_prompt_path):
            with open(base_prompt_path, "r", encoding="utf-8") as f:
                base_prompt = f.read()
        else:
            base_prompt = "You are Mercury, the Reliability Engineer. Repair the build error or merge conflict."

        # 2. Append the dynamic CI/CD context or conflict context
        if conflict:
            dynamic_context = f"""

═══════════════════════════════════════════════════════════════════════════════

MERGE CONFLICT CONTEXT (AUTO-INJECTED)

The pull request has FAILED to auto-merge due to conflicts with the target branch ('main').
• Target Branch: {branch_name}
• Pull Request: #{pr_number}

OBJECTIVE:
You must perform a git rebase against the latest origin/main and resolve any conflicts that arise:
1. Fetch main from origin: `git fetch origin main`
2. Attempt to rebase: `git rebase origin/main`
3. If rebasing pauses with conflicts, check the conflicting files using `git status`.
4. Open the conflicting files and resolve the merge conflict markers (marked with `<<<<<<< HEAD`, `=======`, and `>>>>>>>`). Ensure the code remains functionally correct.
5. After resolving the conflicts in a file, add/stage the changes: `git add <resolved_file>`
6. Continue the rebase: `git rebase --continue` (repeat if there are further conflict steps).
7. Once rebase is complete, verify the code compiled and tests pass locally using verification commands.
8. Force push the resolved branch to origin to update the PR: `git push --force-with-lease origin {branch_name}`.
"""
        else:
            dynamic_context = f"""

═══════════════════════════════════════════════════════════════════════════════

CI/CD FAILURE CONTEXT (AUTO-INJECTED)

The CI/CD build for this pull request has FAILED.
• Target Branch: {branch_name}
• Pull Request: #{pr_number}

Below is the failed build run log/trace. Please analyze it, locate the offending files/lines, and make the necessary edits/fixes to ensure tests pass and the code compiles:

```text
{error_trace}
```
"""

        dynamic_prompt = f"{base_prompt}{dynamic_context}"

        # 3. Write dynamic prompt to temp file on host
        temp_prompt_path = os.path.join(config.HOST_AGENTS_DIR, "mercury_temp.md")
        with open(temp_prompt_path, "w", encoding="utf-8") as f:
            f.write(dynamic_prompt)

        # Register in database
        quota_start_percent = None
        try:
            quota_json = self.state_db.get_state("quota_data")
            if quota_json:
                quota_dict = json.loads(quota_json)
                model_key = "gemini" if self.current_model == config.MODEL_GEMINI else "claude"
                quota_start_percent = quota_dict.get(model_key, {}).get("weekly_percent")
        except Exception as e:
            logger.error("Failed to extract starting quota for Mercury: %s", e)

        self.current_run_id = self.state_db.start_run(
            agent_id="mercury",
            branch_name=branch_name,
            started_at=self.agent_start_time.isoformat(),
            quota_start_percent=quota_start_percent,
            model=self.current_model
        )

        try:
            # Prepare memory context
            self._prepare_and_load_memory_context("mercury")

            # Start agent process using container api
            self.current_process = container.run_agent(
                agent_id="mercury",
                prompt_file="mercury_temp.md",
                branch_name=branch_name,
                model_name=self.current_model
            )
        except Exception as e:
            logger.error("Failed to start Mercury agent run_id=%s error=%s", self.current_run_id, str(e))
            now_str = datetime.now(IST).isoformat()
            self.state_db.fail_run(self.current_run_id, now_str, str(e), summary_message=f"Failed to start healer agent: {e}")
            container.unload_agent_prompt()
            self._clear_active_run()
        finally:
            # Clean up temp file from host immediately after Popen returns
            if os.path.exists(temp_prompt_path):
                try:
                    os.remove(temp_prompt_path)
                except Exception as cleanup_err:
                    logger.error("Failed to delete temp Mercury prompt file on cleanup: %s", cleanup_err)

    def _prepare_and_load_memory_context(self, agent_id: str):
        """
        Aggregates tasks, DMs, group chats, facts, and histories to construct the agent's startup context file.
        Args:
            agent_id (str): Target agent ID.
        """
        context_lines = []
        context_lines.append("=" * 80)
        context_lines.append("SHARED MEMORY CONTEXT")
        context_lines.append("(auto-injected by orchestrator — read this before starting your work)")
        context_lines.append("=" * 80)
        context_lines.append("")

        # 1. Assigned Tasks
        context_lines.append("## 🔴 Assigned Tasks (act on these first)")
        tasks = self.memory_db.get_active_tasks(agent_id)
        if tasks:
            for i, t in enumerate(tasks, 1):
                priority_str = f"[{t['priority'].upper()}]"
                context_lines.append(f"{i}. {priority_str} {t['title']} (ID: {t['id']})")
                if t['description']:
                    context_lines.append(f"   - Description: {t['description']}")
                context_lines.append(f"   - Created by: {t['created_by']}")
                if t['status'] == 'fix_submitted':
                    context_lines.append(f"   - Status: FIX SUBMITTED (verification pending by creator {t['created_by']})")
                if t['related_pr']:
                    context_lines.append(f"   - Related PR: #{t['related_pr']}")
                if t['related_file']:
                    context_lines.append(f"   - Related File: {t['related_file']}")
            context_lines.append("")
            context_lines.append("If you have pending assigned tasks above, fix at least one and submit a PR.")
            context_lines.append("If you are verifying a task you created that is marked 'fix_submitted', inspect the changes, and if correct, mark it as 'resolved' in your outbox.")
        else:
            context_lines.append("None — search for improvements according to your role.")
        context_lines.append("")

        # 2. Unread DMs
        context_lines.append("## 💬 Unread DMs")
        dms = self.memory_db.get_unread_dms(agent_id)
        if dms:
            for dm in dms:
                sender = dm['sender_id'].capitalize()
                ts = dm['created_at'].replace('T', ' ').split('.')[0]
                context_lines.append(f"- [{sender} → You] ({ts})")
                context_lines.append(f"  \"{dm['content']}\"")
                if dm['thread_id']:
                    context_lines.append(f"  Thread ID: {dm['thread_id']}")
                if dm['ref_id']:
                    context_lines.append(f"  Reference: {dm['ref_id']}")
        else:
            context_lines.append("None.")
        context_lines.append("")

        # 3. Recent Group Chat
        context_lines.append("## 📢 Recent Group Chat (last 24h)")
        group_msgs = self.memory_db.get_recent_group_messages(hours=24)
        if group_msgs:
            # Only show last 15 messages to avoid blowing up context
            for msg in group_msgs[-15:]:
                sender = msg['sender_id'].capitalize()
                ts = msg['created_at'].replace('T', ' ').split('.')[0]
                content = msg['content'].replace('\n', '\n  ')
                context_lines.append(f"- [{sender}] ({ts})")
                context_lines.append(f"  {content}")
        else:
            context_lines.append("No recent group chat messages.")
        context_lines.append("")

        # 4. Your Last Run Summary
        context_lines.append("## 🧠 Your Last Run Summary")
        last_run = self.state_db.get_last_run_for_agent(agent_id)
        if last_run:
            status = last_run['status']
            finished = (last_run['finished_at'] or "unknown").replace('T', ' ').split('.')[0]
            summary = last_run['summary_message'] or "No summary provided."
            context_lines.append(f"Your last run completed at {finished} with status: {status}")
            context_lines.append(f"Summary: {summary}")
        else:
            context_lines.append("This is your first run in this environment.")
        context_lines.append("")

        # 5. Relevant Facts
        context_lines.append("## 📌 Relevant Facts")
        facts = self.memory_db.get_active_facts()
        if facts:
            for f in facts:
                cat_str = f" [{f['category']}]" if f['category'] else ""
                context_lines.append(f"- {f['fact']}{cat_str} (Fact ID: {f['id']})")
        else:
            context_lines.append("No active facts/constraints registered.")
        context_lines.append("")
        context_lines.append("=" * 80)

        markdown_content = "\n".join(context_lines)
        
        orchestrator_dir = os.path.dirname(config.DB_PATH)
        host_context_path = os.path.join(orchestrator_dir, "memory_context.md")
        try:
            with open(host_context_path, "w") as f:
                f.write(markdown_content)
            container.copy_memory_context_to_container(host_context_path)
        finally:
            if os.path.exists(host_context_path):
                os.remove(host_context_path)

    def _process_agent_memory_outbox(self, agent_id: str, run_id: Optional[int], summary: Optional[str]):
        """
        Ingests the memory_outbox.json from the completed agent run, recording new facts/tasks/DMs.
        Args:
            agent_id (str): Executing agent ID.
            run_id (Optional[int]): Run database ID.
            summary (Optional[str]): Output final summary message.
        """
        t0 = time.time()
        orchestrator_dir = os.path.dirname(config.DB_PATH)
        host_outbox_path = os.path.join(orchestrator_dir, "memory_outbox.json")
        
        copied = container.read_memory_outbox_from_container(host_outbox_path)
        
        # Add run summary to group chat and memory DB
        if summary:
            self.memory_db.add_memory(
                agent_id=agent_id,
                run_id=run_id,
                memory_type="outcome",
                content=f"Completed run: {summary}",
                scope="global",
                tags=["run_summary"]
            )
            self.memory_db.add_message(
                channel="group",
                sender_id=agent_id,
                content=summary,
                message_type="text"
            )
            
        if copied and os.path.exists(host_outbox_path):
            try:
                with open(host_outbox_path, "r") as f:
                    outbox = json.load(f)
                
                logger.info("Processing memory outbox for agent %s...", agent_id)
                
                # 1. Memories
                for mem in outbox.get("memories", []):
                    m_type = mem.get("type", "observation")
                    content = mem.get("content")
                    scope = mem.get("scope", "global")
                    tags = mem.get("tags", [])
                    if content:
                        self.memory_db.add_memory(
                            agent_id=agent_id,
                            run_id=run_id,
                            memory_type=m_type,
                            content=content,
                            scope=scope,
                            tags=tags
                        )
                
                # 2. Messages
                for msg in outbox.get("messages", []):
                    channel = msg.get("channel", "group")
                    to = msg.get("to")
                    content = msg.get("content")
                    thread_id = msg.get("thread_id")
                    m_type = msg.get("message_type", "text")
                    ref_id = msg.get("ref")
                    
                    if content:
                        self.memory_db.add_message(
                            channel=channel,
                            sender_id=agent_id,
                            recipient_id=to if channel == "dm" else None,
                            content=content,
                            thread_id=thread_id,
                            message_type=m_type,
                            ref_id=ref_id
                        )
                
                # 3. Tasks resolved
                for task_id in outbox.get("tasks_resolved", []):
                    try:
                        t_id = int(task_id)
                        success = self.memory_db.update_task_status(t_id, agent_id, 'resolved')
                        logger.info("Task %d resolved update by %s status=%s", t_id, agent_id, success)
                    except Exception as te:
                        logger.error("Failed to process resolved task ID %s: %s", task_id, te)
                
                # 4. Tasks created
                for task in outbox.get("tasks_created", []):
                    title = task.get("title")
                    desc = task.get("description")
                    assigned_to = task.get("assigned_to")
                    priority = task.get("priority", "normal")
                    tags = task.get("tags", [])
                    related_pr = task.get("related_pr")
                    related_file = task.get("related_file")
                    
                    if title:
                        t_id = self.memory_db.create_task(
                            title=title,
                            description=desc,
                            created_by=agent_id,
                            assigned_to=assigned_to,
                            priority=priority,
                            tags=tags,
                            related_pr=related_pr,
                            related_file=related_file
                        )
                        logger.info("Task created with ID %d: %s", t_id, title)
                        
                        # Link a DM thread to this task automatically if assigned to another agent
                        if assigned_to:
                            self.memory_db.add_message(
                                channel="dm",
                                sender_id=agent_id,
                                recipient_id=assigned_to,
                                content=f"New task assigned: **{title}**\nDescription: {desc or 'None'}",
                                thread_id=f"resolve:task:{t_id}",
                                message_type="task_ref",
                                ref_id=str(t_id)
                            )
                
                # 5. Facts added
                for fact in outbox.get("facts", []):
                    f_content = fact.get("fact")
                    cat = fact.get("category")
                    if f_content:
                        f_id = self.memory_db.add_fact(f_content, agent_id, cat)
                        logger.info("Fact added with ID %d", f_id)
                
                # 6. Facts updated/superseded
                for fact in outbox.get("facts_updated", []):
                    f_id = fact.get("id")
                    f_content = fact.get("fact")
                    cat = fact.get("category")
                    if f_id and f_content:
                        try:
                            new_f_id = self.memory_db.update_fact(int(f_id), f_content, agent_id, cat)
                            logger.info("Fact %d superseded by new fact %d", f_id, new_f_id)
                        except Exception as fe:
                            logger.error("Failed to update fact %s: %s", f_id, fe)
                            
            except Exception as e:
                logger.error("Error reading/parsing memory outbox for agent %s: %s", agent_id, e, exc_info=True)
            finally:
                if os.path.exists(host_outbox_path):
                    os.remove(host_outbox_path)
        logger.info("Processed memory outbox for agent: agent_id=%s run_id=%s duration_sec=%.3f", agent_id, run_id, time.time() - t0)

    def _check_memory_summarization(self, now: datetime):
        """
        Triggers memory summarization if more than 12 hours have elapsed since the last run.
        Args:
            now (datetime): Current check timestamp.
        """
        """Check if memory summarization is due (every 12 hours) and run it."""
        stored = self.state_db.get_state("last_memory_summarization_time")
        is_due = False
        if not stored:
            # First time running, let's trigger it
            is_due = True
        else:
            try:
                last_time = datetime.fromisoformat(stored)
                if last_time.tzinfo is None:
                    last_time = IST.localize(last_time)
                if (now - last_time) >= timedelta(hours=12):
                    is_due = True
            except Exception as e:
                logger.error("Failed to parse last_memory_summarization_time: %s", e)
                is_due = True

        if is_due:
            logger.info("Memory summarization is due. Running now...")
            try:
                self._run_memory_summarization()
                self.state_db.set_state("last_memory_summarization_time", now.isoformat())
                logger.info("Memory summarization completed successfully.")
            except Exception as e:
                logger.error("Failed to execute memory summarization: %s", e, exc_info=True)

    def _run_memory_summarization(self):
        """
        Uses Gemini to synthesize unarchived memories into active facts and archives parsed memories.
        """
        """Fetch unarchived memories, summarize them using gemini-3.5-flash to update or create facts."""
        t_start = time.time()
        with self.memory_db._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM agent_memories
                WHERE archived = 0
                ORDER BY id ASC
            """).fetchall()
            memories = [dict(r) for r in rows]

        if not memories:
            logger.info("No unarchived memories found. Skipping memory summarization.")
            return

        active_facts = self.memory_db.get_active_facts()

        # Get API keys from environment
        raw_keys = []
        if os.environ.get("PRIMARY_API_KEY"):
            raw_keys.append(os.environ["PRIMARY_API_KEY"])
        for k in sorted(os.environ.keys()):
            if k.startswith("BACKUP_API_KEY_"):
                raw_keys.append(os.environ[k])

        # Deduplicate keys while preserving order
        keys_to_try = []
        for key in raw_keys:
            if key and key not in keys_to_try:
                keys_to_try.append(key)

        if not keys_to_try:
            logger.error("No API keys found in environment variables for memory summarization.")
            return

        from google import genai
        from google.genai import types
        from pydantic import BaseModel, Field

        class FactItem(BaseModel):
            """
            Represents a single semantic fact item extracted from memory.
            """
            id: Optional[int] = Field(None, description="The ID of the existing active fact if this updates or supersedes it, otherwise null.")
            fact: str = Field(description="The semantic fact, constraint, architecture detail, convention, or bug pattern.")
            category: str = Field(description="The category of the fact. Must be one of: 'constraint', 'convention', 'architecture', 'bug_pattern'.")

        class FactList(BaseModel):
            """
            Represents a collection of semantic fact items extracted from memory.
            """
            facts: List[FactItem] = Field(description="List of semantic facts extracted or synthesized from the provided memories.")

        memories_formatted = ""
        for m in memories:
            memories_formatted += (
                f"- [ID: {m['id']}] Agent: {m['agent_id']}, Type: {m['memory_type']}, Scope: {m['scope']}, "
                f"Content: {m['content']}, Created: {m['created_at']}\n"
            )

        active_facts_formatted = ""
        for f in active_facts:
            active_facts_formatted += f"- [ID: {f['id']}] [{f['category']}] {f['fact']}\n"

        prompt = (
            "You are the memory summarization system for a team of autonomous software engineering agents.\n"
            "Your task is to analyze a list of raw, unarchived agent memories and the current list of active facts. "
            "You need to produce a list of updated or new facts that synthesize the raw memories.\n\n"
            "Existing Active Facts:\n"
            f"{active_facts_formatted if active_facts_formatted else '(None)'}\n\n"
            "New Raw Memories:\n"
            f"{memories_formatted}\n\n"
            "Instructions:\n"
            "1. Synthesize the new raw memories into high-level, meaningful, and actionable facts.\n"
            "2. If a new memory updates, refines, or supersedes one of the 'Existing Active Facts', output a fact item "
            "containing the `id` of that active fact. The system will archive the old fact and link it to this new one.\n"
            "3. If a memory introduces a brand new constraint, convention, architectural decision, or bug pattern that "
            "does not relate to any existing fact, output it with `id` set to null (or omit it).\n"
            "4. Each fact MUST belong to one of these categories: 'constraint', 'convention', 'architecture', 'bug_pattern'.\n"
            "5. Return the result as a JSON object matching the requested schema."
        )

        model_id = "gemini-3.5-flash"
        client = None
        resp = None
        last_err = None

        for key in keys_to_try:
            try:
                masked_key = key[:4] + "..." + key[-4:] if len(key) > 8 else "..."
                logger.info("Attempting summarization with API key %s", masked_key)
                client = genai.Client(api_key=key, http_options={'api_version': 'v1beta'})
                
                t_api = time.time()
                resp = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=FactList,
                        temperature=0.1,
                    )
                )
                duration_api = time.time() - t_api
                if resp and resp.text:
                    logger.info("Gemini summarization API call completed model=%s duration_sec=%.3f", model_id, duration_api)
                    break
            except Exception as e:
                logger.warning("Gemini summarization API call failed: %s", e)
                last_err = e

        if not resp or not resp.text:
            raise RuntimeError(f"All API keys failed for memory summarization. Last error: {last_err}")

        try:
            result = json.loads(resp.text)
        except Exception as je:
            logger.error("Failed to parse JSON response from Gemini: %s", resp.text)
            raise je

        facts_data = result.get("facts", [])
        logger.info("Successfully synthesized %d facts from %d memories.", len(facts_data), len(memories))

        for item in facts_data:
            fact_id = item.get("id")
            fact_content = item.get("fact")
            category = item.get("category")

            if not fact_content or not category:
                continue

            if fact_id is not None:
                try:
                    new_fid = self.memory_db.update_fact(
                        fact_id=int(fact_id),
                        new_fact=fact_content,
                        updated_by="orchestrator",
                        category=category
                    )
                    if new_fid != -1:
                        logger.info("Fact ID %s updated/superseded by new Fact ID %d", fact_id, new_fid)
                    else:
                        new_fid = self.memory_db.add_fact(
                            fact=fact_content,
                            added_by="orchestrator",
                            category=category
                        )
                        logger.info("Fact ID %s not found. Added as new Fact ID %d", fact_id, new_fid)
                except Exception as fe:
                    logger.error("Error updating fact ID %s: %s", fact_id, fe)
            else:
                new_fid = self.memory_db.add_fact(
                    fact=fact_content,
                    added_by="orchestrator",
                    category=category
                )
                logger.info("Added new Fact ID %d", new_fid)

        memory_ids = [m["id"] for m in memories]
        with self.memory_db._get_conn() as conn:
            placeholders = ",".join("?" for _ in memory_ids)
            conn.execute(f"""
                UPDATE agent_memories
                SET archived = 1
                WHERE id IN ({placeholders})
            """, memory_ids)
            conn.commit()

        logger.info("Archived %d summarized memories. Total summarization duration_sec=%.3f", len(memory_ids), time.time() - t_start)

    def _get_active_model(self, now: datetime) -> Optional[str]:
        """
        Selects a healthy model (Gemini or Claude fallback) for the agent run based on live container usage limits.
        Args:
            now (datetime): Current evaluation time.
        Returns:
            Optional[str]: Selected model identifier, or None if both are exhausted/throttled.
        """
        """Determines which model to use for the agent run based on current container quota limits."""
        # Clean up any expired cooldowns first
        if self.gemini_cooldown_until and now >= self.gemini_cooldown_until:
            logger.info("Gemini quota cooldown expired.")
            self.gemini_cooldown_until = None
            self.state_db.set_state("gemini_cooldown_until", "")
        if self.claude_cooldown_until and now >= self.claude_cooldown_until:
            logger.info("Claude quota cooldown expired.")
            self.claude_cooldown_until = None
            self.state_db.set_state("claude_cooldown_until", "")

        gemini_cooldown = self.gemini_cooldown_until is not None and now < self.gemini_cooldown_until
        claude_cooldown = self.claude_cooldown_until is not None and now < self.claude_cooldown_until

        try:
            from orchestrator.quota import fetch_quota_data_from_container, parse_quota_text
            # Fetch and parse quota from the container live
            raw_text = fetch_quota_data_from_container()
            quota_data = parse_quota_text(raw_text)
            
            # Save to state database
            import json
            quota_data["last_updated"] = now.isoformat()
            self.state_db.set_state("quota_data", json.dumps(quota_data))
            
            gemini_status = quota_data["gemini"]["status"]
            claude_status = quota_data["claude"]["status"]
            
            logger.info("Live quota status - Gemini: %s (Ratio: %s), Claude: %s (Ratio: %s)", 
                        gemini_status, quota_data["gemini"]["ratio"],
                        claude_status, quota_data["claude"]["ratio"])
            
            # 1. Prefer Gemini if it's healthy and not in reactive cooldown
            if gemini_status == "Healthy" and not gemini_cooldown:
                return config.MODEL_GEMINI
                
            # 2. Fallback to Claude if Claude is healthy, not in reactive cooldown, and Gemini is throttled/exhausted
            if claude_status == "Healthy" and not claude_cooldown:
                logger.info("Gemini is throttled/exhausted/cooldown (%s). Falling back to Claude Sonnet.", gemini_status)
                return config.MODEL_CLAUDE
                
            # 3. If both are throttled or exhausted, return None
            return None
        except Exception as e:
            # Live container query failed (pexpect timeout, container restart, etc.).
            # Do NOT blindly return a model — consult the cached DB quota_data instead
            # so we don't launch an agent when both models are throttled.
            logger.error("Error checking live quota in _get_active_model (pexpect/container failure): %s. Consulting DB cache.", e)
            cached_json = self.state_db.get_state("quota_data")
            if cached_json:
                try:
                    cached = json.loads(cached_json)
                    g_status = cached.get("gemini", {}).get("status", "Unknown")
                    c_status = cached.get("claude", {}).get("status", "Unknown")
                    logger.info("[FALLBACK] Cached quota status — Gemini: %s, Claude: %s", g_status, c_status)
                    if g_status == "Healthy" and not gemini_cooldown:
                        return config.MODEL_GEMINI
                    if c_status == "Healthy" and not claude_cooldown:
                        return config.MODEL_CLAUDE
                    # Both throttled/exhausted in cache — refuse to start
                    logger.warning("[FALLBACK] Both models are non-Healthy in DB cache. Deferring run.")
                    return None
                except Exception as parse_err:
                    logger.error("[FALLBACK] Failed to parse cached quota_data: %s", parse_err)
            # Last resort: if we have no cache at all and no hard cooldowns, allow Gemini
            logger.warning("[FALLBACK] No cached quota_data available. Defaulting conservatively to Gemini.")
            return config.MODEL_GEMINI if not gemini_cooldown else (config.MODEL_CLAUDE if not claude_cooldown else None)
