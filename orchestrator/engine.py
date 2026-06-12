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
            logger.info("Recovering active run state run_id=%s agent_id=%s branch_name=%s", current['id'], current['agent_id'], current['branch_name'])
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
                logger.info("Associated agy process is still active inside container container_name=%s", config.CONTAINER_NAME)
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
                logger.warning("No active agy process found in container for recovered run marking_failed=true run_id=%s", self.current_run_id)
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
                        "[STARTUP] Restored quota cooldown from DB remaining_min=%.1f cooldown_until=%s",
                        remaining, cooldown_dt.isoformat()
                    )
                else:
                    logger.info("[STARTUP] Stored quota cooldown has already expired clearing=true")
                    self.state_db.set_state("quota_cooldown_until", "")
            except Exception as e:
                logger.error("Failed to restore cooldown from DB error=%s", str(e))

    def _get_agy_log_tail(self, start_time: datetime) -> str:
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

    def _is_in_cooldown(self, now: datetime) -> bool:
        """Return True if the engine is currently in quota cooldown."""
        if self.quota_cooldown_until and now < self.quota_cooldown_until:
            remaining = (self.quota_cooldown_until - now).total_seconds() / 60
            logger.info(
                "[COOLDOWN] Quota not yet refreshed remaining_min=%.1f cooldown_until=%s action=skip_tick",
                remaining, self.quota_cooldown_until.isoformat()
            )
            return True
        if self.quota_cooldown_until and now >= self.quota_cooldown_until:
            logger.info("[COOLDOWN] Quota cooldown lifted resuming_scheduling=true")
            self.quota_cooldown_until = None
        return False

    def _tick(self):
        now = datetime.now(IST)
        
        # Verify container is running
        if not container.is_container_running():
            logger.error(f"Docker container {config.CONTAINER_NAME} is not running! Skipping tick.")
            return

        # 1. Always monitor a running agent (even during cooldown — shouldn't happen, but be safe)
        if self.current_process:
            self._check_running_agent(now)
            return

        # 2. Always check for merged PRs and trigger service reloads — even during cooldown.
        #    Agent fixes should be deployed immediately regardless of quota state.
        next_agent = self._check_for_merge_trigger()
        if next_agent and not self._is_in_cooldown(now):
            logger.info(f"PR MERGE EVENT DETECTED: Starting next agent {next_agent['name']} immediately!")
            self._start_agent(next_agent)
            return

        # 3. Quota cooldown guard — don't schedule new agents until quota refreshes
        if self._is_in_cooldown(now):
            return

        # 4. Check if any agent's scheduled time is due
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
            
            logger.info("Agent process finished agent_id=%s run_id=%s exit_code=%d", self.current_agent_id, self.current_run_id, retcode)
            now_str = now.isoformat()

            # --- Quota check: read cli.log BEFORE deciding success/failure ---
            log_tail = self._get_agy_log_tail(self.agent_start_time)
            quota_reset_at = self._check_quota_error(log_tail)

            if quota_reset_at:
                # Agent hit quota — treat as failed and enter cooldown
                logger.warning("Agent terminated by quota exhaustion agent_id=%s run_id=%s cooldown_until=%s", self.current_agent_id, self.current_run_id, quota_reset_at.isoformat())
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

                    # If already merged (fast auto-merge), trigger reload immediately
                    # so we don't wait for _check_for_merge_trigger to catch it
                    if db_pr_status == 'MERGED':
                        logger.info("PR already merged triggering immediate service reload pr_num=%d", pr_num)
                        self._pull_and_reload_services(pr_num)
                else:
                    logger.warning("Agent completed but no PR was detected branch_name=%s", self.branch_name)
                    self.state_db.complete_run(self.current_run_id, now_str, pr_status='NONE', summary_message=summary)
            else:
                self.state_db.fail_run(self.current_run_id, now_str, f"Process exited with non-zero code {retcode}.", summary_message=f"Agent process exited with non-zero code {retcode}.")

            container.unload_agent_prompt()
            self._clear_active_run()
            return

        # Watchdog timeout check
        elapsed_minutes = (now - self.agent_start_time).total_seconds() / 60
        if elapsed_minutes > config.MAX_AGENT_RUNTIME_MINUTES:
            logger.warning("Agent exceeded max runtime killing_agent=true agent_id=%s run_id=%s max_runtime_min=%d elapsed_min=%.1f", self.current_agent_id, self.current_run_id, config.MAX_AGENT_RUNTIME_MINUTES, elapsed_minutes)
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
                logger.info("PR merged pr_num=%d agent_id=%s", pr_num, run['agent_id'])
                self.state_db.update_pr_status(run['id'], 'MERGED')
                
                # Auto pull and reload affected services
                self._pull_and_reload_services(pr_num)
                
                # Get the NEXT agent in pipeline
                return self._get_next_pipeline_agent(run['agent_id'])
            elif current_status == 'CLOSED':
                logger.info("PR closed without merging pr_num=%d agent_id=%s", pr_num, run['agent_id'])
                self.state_db.update_pr_status(run['id'], 'CLOSED')
                
        return None

    def _pull_and_reload_services(self, pr_num: int):
        logger.info("PR merged starting host service update pr_num=%d", pr_num)
        try:
            repo = "/home/stellaradmin/my_app"

            # 1. Ensure we're on main (orchestrator may have left the repo on an agent branch)
            checkout_res = subprocess.run(
                ["sudo", "-u", "stellaradmin", "git", "checkout", "main"],
                cwd=repo, capture_output=True, text=True
            )
            if checkout_res.returncode != 0:
                logger.warning("git checkout main failed error=%s", checkout_res.stderr.strip())

            # 2. Pull as stellaradmin — they own the SSH key
            pull_res = subprocess.run(
                ["sudo", "-u", "stellaradmin", "git", "pull"],
                cwd=repo, capture_output=True, text=True, check=True
            )
            logger.info("git pull completed output=%s", pull_res.stdout.strip())

            # 3. Get list of files modified in the merge commit
            diff_res = subprocess.run(
                ["sudo", "-u", "stellaradmin", "git", "diff", "HEAD~1", "HEAD", "--name-only"],
                cwd=repo, capture_output=True, text=True, check=True
            )
            files = [f.strip() for f in diff_res.stdout.strip().split('\n') if f.strip()]
            logger.info("Files modified in merge commit files=%s", str(files))

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
                logger.info("Auto-reloading stellar.service")
                subprocess.run(["sudo", "systemctl", "reload", "stellar"], check=True)

            if restart_ssh:
                logger.info("Auto-restarting stellar-ssh.service")
                subprocess.run(["sudo", "systemctl", "restart", "stellar-ssh"], check=True)

            if restart_orchestrator:
                logger.info("Auto-restarting stellar_orchestrator.service")
                # Run detached so it doesn't kill this process before finishing the tick
                subprocess.Popen(["sudo", "systemctl", "restart", "stellar_orchestrator"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        except Exception as e:
            logger.error("Failed to auto-pull or reload services for PR pr_num=%d error=%s", pr_num, str(e), exc_info=True)


    def _get_next_pipeline_agent(self, current_agent_id: str) -> Optional[Dict[str, Any]]:
        for i, agent in enumerate(config.AGENT_PIPELINE):
            if agent['id'] == current_agent_id:
                next_idx = (i + 1) % len(config.AGENT_PIPELINE)
                return config.AGENT_PIPELINE[next_idx]
        return None

    def _get_due_agent(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Checks schedules to see if any agent is due to run and hasn't successfully completed today."""
        for agent in config.AGENT_PIPELINE:
            sched_str = agent['schedule']
            sched_time = datetime.strptime(sched_str, "%H:%M").time()
            sched_dt = datetime.combine(now.date(), sched_time).replace(tzinfo=IST)

            if now >= sched_dt:
                last_run = self.state_db.get_last_run_for_agent(agent['id'])
                if last_run:
                    last_started = datetime.fromisoformat(last_run['started_at']).astimezone(IST)
                    if last_started.date() == now.date():
                        # Only skip if it COMPLETED successfully today.
                        # FAILED / TIMEOUT / INTERRUPTED / RUNNING all allow a retry.
                        if last_run['status'] == 'COMPLETED':
                            continue
                        elif last_run['status'] == 'RUNNING':
                            # Already being handled by _check_running_agent
                            continue
                        else:
                            logger.info("Agent last run today failed scheduling retry agent_id=%s last_status=%s", agent['id'], last_run['status'])
                            
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
            logger.error("Failed to start agent agent_id=%s run_id=%s error=%s", self.current_agent_id, self.current_run_id, str(e))
            now_str = datetime.now(IST).isoformat()
            self.state_db.fail_run(self.current_run_id, now_str, str(e), summary_message=f"Failed to start agent: {e}")
            container.unload_agent_prompt()
            self._clear_active_run()
