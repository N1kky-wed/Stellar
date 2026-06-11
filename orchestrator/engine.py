# engine.py
import re
import time
import logging
from datetime import datetime, timedelta
import pytz
import subprocess
from typing import Optional, Dict, Any

import orchestrator.config as config
from orchestrator.state import StateDB
import orchestrator.container as container

logger = logging.getLogger("stellar-orchestrator")
IST = pytz.timezone(config.TIMEZONE)

class OrchestratorEngine:
    def __init__(self):
        self.state_db = StateDB(config.DB_PATH)
        self.current_process: Optional[subprocess.Popen] = None
        self.current_agent_id: Optional[str] = None
        self.current_run_id: Optional[int] = None
        self.agent_start_time: Optional[datetime] = None
        self.branch_name: Optional[str] = None
        self.prompt_file: Optional[str] = None
        # Quota cooldown: don't start any agent until this time
        self.quota_cooldown_until: Optional[datetime] = None

        # Recover from previous crash/restart if there's a running run in database
        self._recover_state()

        # Restore persisted cooldown (survives service restarts)
        self._restore_cooldown_from_db()

    def _recover_state(self):
        current = self.state_db.get_current_run()
        if current:
            logger.info(f"Recovering active run state: {current}")
            self.current_agent_id = current['agent_id']
            self.current_run_id = current['id']
            self.branch_name = current['branch_name']
            
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
                logger.info("Associated agy process is still active inside the container. Monitoring it...")
                # We can't poll a non-existent Popen, but we can periodically check the container process list.
                # However, for simplicity, we will wrap the check using a dummy process or let the watchdog handle it
                # if it hangs, or check process presence inside self._check_running_agent.
                # Let's mock a process handle that checks process list on poll()
                class RecoveredProcess:
                    def poll(self):
                        inner_rc, inner_out, _ = container.exec_in_container("ps aux | grep -i agy | grep -v grep")
                        if inner_rc != 0 or "agy" not in inner_out:
                            return 0 # Completed/stopped
                        return None # Still running
                    def kill(self):
                        container.exec_in_container("pkill -f agy")
                self.current_process = RecoveredProcess()
                self.agent_start_time = datetime.fromisoformat(current['started_at'])
            else:
                logger.warn("No active agy process found in container for recovered run. Marking failed.")
                now_str = datetime.now(IST).isoformat()
                self.state_db.fail_run(self.current_run_id, now_str, "Orchestrator restarted and container process was not found.")
                container.unload_agent_prompt()
                self._clear_active_run()

    def _clear_active_run(self):
        self.current_process = None
        self.current_agent_id = None
        self.current_run_id = None
        self.agent_start_time = None
        self.branch_name = None
        self.prompt_file = None

    def run(self):
        logger.info("Stellar Agent Orchestrator Engine Started.")
        while True:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"Error in orchestrator engine tick: {e}", exc_info=True)
            
            if self.current_process:
                time.sleep(2)
            else:
                time.sleep(config.PR_CHECK_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Quota / Rate-limit helpers
    # ------------------------------------------------------------------

    def _restore_cooldown_from_db(self):
        """On startup, restore any persisted quota cooldown so restarts don't bypass it."""
        stored = self.state_db.get_state("quota_cooldown_until")
        if stored:
            try:
                cooldown_dt = datetime.fromisoformat(stored)
                now = datetime.now(IST)
                if cooldown_dt > now:
                    self.quota_cooldown_until = cooldown_dt
                    remaining = (cooldown_dt - now).total_seconds() / 60
                    logger.warning(
                        f"[STARTUP] Restored quota cooldown from DB. "
                        f"{remaining:.1f} min remaining until {cooldown_dt.strftime('%H:%M:%S %Z')}."
                    )
                else:
                    logger.info("[STARTUP] Stored quota cooldown has already expired. Clearing.")
                    self.state_db.set_state("quota_cooldown_until", "")
            except Exception as e:
                logger.error(f"Failed to restore cooldown from DB: {e}")

    def _get_agy_log_tail(self) -> str:
        """Read the last 100 lines of the agy cli.log from inside the container."""
        cmd = "cat /root/.gemini/antigravity-cli/cli.log 2>/dev/null | tail -100"
        rc, stdout, _ = container.exec_in_container(cmd, timeout=10)
        return stdout if rc == 0 else ""

    def _check_quota_error(self, log_tail: str) -> Optional[datetime]:
        """
        Scan the agy cli.log tail for RESOURCE_EXHAUSTED / 429 errors.
        If found, parse 'Resets in Xh Ym Zs' and return the datetime when
        quota will be restored. Returns None if no quota error found.
        """
        if "RESOURCE_EXHAUSTED" not in log_tail and "429" not in log_tail:
            return None

        logger.warning("QUOTA ERROR detected in agy log!")

        # Try to parse 'Resets in Xh Ym Zs' or 'Resets in Xm Ys'
        match = re.search(
            r"Resets in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?",
            log_tail
        )
        now = datetime.now(IST)
        if match:
            hours   = int(match.group(1) or 0)
            minutes = int(match.group(2) or 0)
            seconds = int(match.group(3) or 0)
            delta   = timedelta(hours=hours, minutes=minutes, seconds=seconds)
            # Add a small buffer so we don't retry right at the boundary
            reset_at = now + delta + timedelta(minutes=2)
            logger.warning(
                f"Quota resets in {hours}h {minutes}m {seconds}s. "
                f"Cooldown until {reset_at.strftime('%Y-%m-%d %H:%M:%S %Z')}"
            )
            return reset_at
        else:
            # Couldn't parse reset time — default to 4-hour cooldown
            fallback = now + timedelta(hours=4)
            logger.warning(f"Could not parse reset time. Defaulting 4-hour cooldown until {fallback}")
            return fallback

    def _is_in_cooldown(self, now: datetime) -> bool:
        """Return True if the engine is currently in quota cooldown."""
        if self.quota_cooldown_until and now < self.quota_cooldown_until:
            remaining = (self.quota_cooldown_until - now).total_seconds() / 60
            logger.info(
                f"[COOLDOWN] Quota not yet refreshed. "
                f"{remaining:.1f} min remaining until {self.quota_cooldown_until.strftime('%H:%M:%S %Z')}. "
                "Skipping tick."
            )
            return True
        if self.quota_cooldown_until and now >= self.quota_cooldown_until:
            logger.info("[COOLDOWN] Quota cooldown lifted. Resuming normal scheduling.")
            self.quota_cooldown_until = None
        return False

    def _tick(self):
        now = datetime.now(IST)
        
        # Verify container is running
        if not container.is_container_running():
            logger.error(f"Docker container {config.CONTAINER_NAME} is not running! Skipping tick.")
            return

        # 0. Quota cooldown guard — do nothing until quota refreshes
        if self._is_in_cooldown(now):
            return

        # 1. If an agent is running, monitor it
        if self.current_process:
            self._check_running_agent(now)
            return

        # 2. Check if the previous agent's PR just got MERGED (Event-driven trigger)
        # We find the last COMPLETED run, check if its PR status is PENDING, and if it just got MERGED,
        # we start the next agent immediately.
        next_agent = self._check_for_merge_trigger()
        if next_agent:
            logger.info(f"PR MERGE EVENT DETECTED: Triggering next agent {next_agent['name']} immediately!")
            self._start_agent(next_agent)
            return

        # 3. Check if any agent's scheduled time is due
        due_agent = self._get_due_agent(now)
        if due_agent:
            logger.info(f"SCHEDULE DUE EVENT: Starting scheduled agent {due_agent['name']}...")
            self._start_agent(due_agent)
            return

    def _drain_stdout(self):
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
                logger.info(f"[{self.current_agent_id}] {line.strip()}")
        except BlockingIOError:
            pass
        except Exception as e:
            logger.error(f"Error reading process stdout: {e}")

    def _get_summary(self) -> Optional[str]:
        try:
            return container.get_agent_final_summary()
        except Exception as e:
            logger.error(f"Error extracting agent final summary: {e}")
            return None

    def _check_running_agent(self, now: datetime):
        # Read some stdout/stderr to prevent buffer block if using pipes
        self._drain_stdout()
        
        retcode = self.current_process.poll()
        if retcode is not None:
            # Drain one last time to get final output
            self._drain_stdout()
            
            logger.info(f"Agent {self.current_agent_id} process finished with exit code {retcode}.")
            now_str = now.isoformat()

            # --- Quota check: read cli.log BEFORE deciding success/failure ---
            log_tail = self._get_agy_log_tail()
            quota_reset_at = self._check_quota_error(log_tail)

            if quota_reset_at:
                # Agent hit quota — treat as failed and enter cooldown
                logger.warning(
                    f"Agent {self.current_agent_id} was terminated by quota exhaustion (429). "
                    f"Marking FAILED. Cooldown until {quota_reset_at.strftime('%H:%M:%S %Z')}."
                )
                self.quota_cooldown_until = quota_reset_at
                self.state_db.set_state("quota_cooldown_until", quota_reset_at.isoformat())
                self.state_db.fail_run(
                    self.current_run_id, now_str,
                    f"RESOURCE_EXHAUSTED (429): quota resets at {quota_reset_at.isoformat()}",
                    summary_message="Agent aborted due to API quota exhaustion."
                )
                container.unload_agent_prompt()
                self._clear_active_run()
                return
            
            if retcode == 0:
                summary = self._get_summary()
                # Query container for any newly created PRs on this branch
                prs = container.check_new_prs(self.branch_name)
                if prs:
                    # Found PR
                    pr = prs[0]
                    pr_num = pr.get("number")
                    pr_url = pr.get("url")
                    pr_state = pr.get("state")
                    logger.info(f"Detected PR #{pr_num} created by agent: {pr_url} (State: {pr_state})")
                    
                    db_pr_status = 'PENDING'
                    if pr_state == 'MERGED':
                        db_pr_status = 'MERGED'
                    elif pr_state == 'CLOSED':
                        db_pr_status = 'CLOSED'
                        
                    self.state_db.complete_run(self.current_run_id, now_str, pr_number=pr_num, pr_url=pr_url, pr_status=db_pr_status, summary_message=summary)
                else:
                    logger.warn(f"Agent completed but no PR was detected on branch {self.branch_name}.")
                    self.state_db.complete_run(self.current_run_id, now_str, pr_status='NONE', summary_message=summary)
            else:
                self.state_db.fail_run(self.current_run_id, now_str, f"Process exited with non-zero code {retcode}.", summary_message=f"Agent process exited with non-zero code {retcode}.")
            
            container.unload_agent_prompt()
            self._clear_active_run()
            return

        # Watchdog timeout check
        elapsed_minutes = (now - self.agent_start_time).total_seconds() / 60
        if elapsed_minutes > config.MAX_AGENT_RUNTIME_MINUTES:
            logger.warn(f"Agent {self.current_agent_id} has exceeded max runtime ({config.MAX_AGENT_RUNTIME_MINUTES}m). Killing it.")
            self._handle_timeout(now)

    def _handle_timeout(self, now: datetime):
        try:
            self.current_process.kill()
        except Exception as e:
            logger.error(f"Error killing agent process: {e}")
        
        # Kill any runaway agy inside container
        container.exec_in_container("pkill -f agy")
        
        now_str = now.isoformat()
        self.state_db.timeout_run(self.current_run_id, now_str, summary_message="Agent execution timed out after exceeding limits.")
        container.unload_agent_prompt()
        self._clear_active_run()

    def _check_for_merge_trigger(self) -> Optional[Dict[str, Any]]:
        """Checks if the PR of the most recently run agent was merged."""
        pending_runs = self.state_db.get_pending_prs()
        for run in pending_runs:
            pr_num = run['pr_number']
            current_status = container.check_pr_status(pr_num)
            
            if current_status == 'MERGED':
                logger.info(f"PR #{pr_num} for agent {run['agent_id']} was MERGED.")
                self.state_db.update_pr_status(run['id'], 'MERGED')
                
                # Auto pull and reload affected services
                self._pull_and_reload_services(pr_num)
                
                # Get the NEXT agent in pipeline
                return self._get_next_pipeline_agent(run['agent_id'])
            elif current_status == 'CLOSED':
                logger.info(f"PR #{pr_num} for agent {run['agent_id']} was CLOSED without merging.")
                self.state_db.update_pr_status(run['id'], 'CLOSED')
                
        return None

    def _pull_and_reload_services(self, pr_num: int):
        logger.info(f"PR #{pr_num} merged. Pulling changes and checking for service updates on host...")
        try:
            # 1. Pull changes on host
            pull_res = subprocess.run(["git", "pull"], cwd="/home/stellaradmin/my_app", capture_output=True, text=True, check=True)
            logger.info(f"Git pull output:\n{pull_res.stdout.strip()}")
            
            # 2. Get list of files modified in the merge commit
            diff_res = subprocess.run(["git", "diff", "HEAD~1", "HEAD", "--name-only"], cwd="/home/stellaradmin/my_app", capture_output=True, text=True, check=True)
            files = [f.strip() for f in diff_res.stdout.strip().split('\n') if f.strip()]
            logger.info(f"Files modified in merge commit: {files}")
            
            reload_stellar = False
            restart_ssh = False
            restart_orchestrator = False
            
            for file in files:
                if file.startswith("orchestrator/") or file == "stellar_orchestrator.service":
                    restart_orchestrator = True
                elif file == "ssh_gateway.py":
                    restart_ssh = True
                elif file in ["app.py", "sentinel_healer.py", "webscrapper.py", "agent_tools.py"] or file.startswith("templates/") or file.startswith("static/"):
                    reload_stellar = True
                    
            if reload_stellar:
                logger.info("Auto-reloading stellar.service...")
                subprocess.run(["sudo", "systemctl", "reload", "stellar"], check=True)
                
            if restart_ssh:
                logger.info("Auto-restarting stellar-ssh.service...")
                subprocess.run(["sudo", "systemctl", "restart", "stellar-ssh"], check=True)
                
            if restart_orchestrator:
                logger.info("Auto-restarting stellar_orchestrator.service...")
                # Run detached so it doesn't kill this process before finishing the tick
                subprocess.Popen(["sudo", "systemctl", "restart", "stellar_orchestrator"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
        except Exception as e:
            logger.error(f"Failed to auto-pull or reload services for PR #{pr_num}: {e}", exc_info=True)

    def _get_next_pipeline_agent(self, current_agent_id: str) -> Optional[Dict[str, Any]]:
        for i, agent in enumerate(config.AGENT_PIPELINE):
            if agent['id'] == current_agent_id:
                next_idx = (i + 1) % len(config.AGENT_PIPELINE)
                return config.AGENT_PIPELINE[next_idx]
        return None

    def _get_due_agent(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Checks schedules to see if any agent is due to run and hasn't already run today."""
        for agent in config.AGENT_PIPELINE:
            sched_str = agent['schedule']
            sched_time = datetime.strptime(sched_str, "%H:%M").time()
            sched_dt = datetime.combine(now.date(), sched_time).replace(tzinfo=IST)
            
            if now >= sched_dt:
                # Check if it ran today (since local midnight)
                last_run = self.state_db.get_last_run_for_agent(agent['id'])
                if last_run:
                    last_started = datetime.fromisoformat(last_run['started_at']).astimezone(IST)
                    if last_started.date() == now.date():
                        # Already run today, skip
                        continue
                
                # Verify that no agent ran *after* this one's scheduled time today, to avoid backward runs
                return agent
        return None

    def _start_agent(self, agent: Dict[str, Any]):
        self.current_agent_id = agent['id']
        self.prompt_file = agent['prompt_file']
        
        now = datetime.now(IST)
        date_str = now.strftime("%Y-%m-%d-%H%M")
        self.branch_name = f"{config.GITHUB_BRANCH_PREFIX}{self.current_agent_id}/{date_str}"
        self.agent_start_time = now
        
        # Register in database
        self.current_run_id = self.state_db.start_run(
            agent_id=self.current_agent_id,
            branch_name=self.branch_name,
            started_at=now.isoformat()
        )
        
        try:
            self.current_process = container.run_agent(
                agent_id=self.current_agent_id,
                prompt_file=self.prompt_file,
                branch_name=self.branch_name
            )
        except Exception as e:
            logger.error(f"Failed to start agent {self.current_agent_id}: {e}")
            now_str = datetime.now(IST).isoformat()
            self.state_db.fail_run(self.current_run_id, now_str, str(e), summary_message=f"Failed to start agent: {e}")
            container.unload_agent_prompt()
            self._clear_active_run()
