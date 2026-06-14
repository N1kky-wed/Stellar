# state.py
"""
State database management for the Stellar Orchestrator.
Tracks details of each agent execution run, including startup times, models, statuses, and associated PRs.
Also stores general key-value settings.
"""
import sqlite3
import os
import datetime
from typing import Optional, List, Dict, Any

class StateDB:
    """
    Manages SQLite database operations for the orchestrator's state database (orchestrator.db).
    Saves and updates agent runs and key-value state settings.
    """
    def __init__(self, db_path: str):
        """
        Initializes StateDB and sets up the database schema.
        Args:
            db_path (str): File system path to the SQLite state database.
        """
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        """
        Gets a connection to the SQLite database with WAL mode and busy timeout set.
        Returns:
            sqlite3.Connection: Database connection.
        """
        import time
        import logging
        t0 = time.time()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        duration = time.time() - t0
        if duration > 0.05:
            logger = logging.getLogger("stellar-orchestrator")
            logger.warning("Slow database connection path=%s duration_sec=%.3f", self.db_path, duration)
        return conn

    def _init_db(self):
        """
        Creates schema tables (agent_runs, orchestrator_state) and applies migrations if necessary.
        """
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._get_conn() as conn:
            # Table for recording individual runs of agents
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL, -- RUNNING, COMPLETED, FAILED, TIMEOUT
                    pr_number INTEGER,
                    pr_url TEXT,
                    pr_status TEXT DEFAULT 'NONE', -- NONE, PENDING, MERGED, CLOSED
                    branch_name TEXT,
                    error_message TEXT
                )
            """)
            # Self-healing migration to add summary_message column if missing
            try:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN summary_message TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN quota_start_percent REAL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN model TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN quota_cost REAL")
            except sqlite3.OperationalError:
                pass
            # Table for general orchestrator state
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orchestrator_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()

    def start_run(self, agent_id: str, branch_name: str, started_at: str, quota_start_percent: Optional[float] = None, model: Optional[str] = None) -> int:
        """
        Inserts a new agent run row with a status of 'RUNNING' and publishes the startup event.
        Args:
            agent_id (str): The identifier of the agent.
            branch_name (str): The git branch where the agent works.
            started_at (str): ISO formatted start timestamp.
            quota_start_percent (Optional[float]): The API quota weekly percentage at start.
            model (Optional[str]): The model utilized for the run.
        Returns:
            int: The unique run ID in the database.
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_runs (agent_id, branch_name, started_at, status, quota_start_percent, model)
                VALUES (?, ?, ?, 'RUNNING', ?, ?)
            """, (agent_id, branch_name, started_at, quota_start_percent, model))
            conn.commit()
            lastrowid = cursor.lastrowid

        # Publish starting agent event to Redis
        try:
            import redis
            import json
            r = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)
            agent_name = agent_id.capitalize()
            start_ts = started_at.replace('T', ' ').split('.')[0]
            msg_payload = {
                'timestamp': start_ts,
                'sender': 'Orchestrator',
                'content': f"🚀 Starting agent **{agent_name}** on branch `{branch_name}`...",
                'type': 'system'
            }
            r.publish("agent_events", json.dumps(msg_payload))
        except Exception:
            pass

        return lastrowid

    def complete_run(self, run_id: int, finished_at: str, pr_number: Optional[int] = None, pr_url: Optional[str] = None, pr_status: str = 'NONE', summary_message: Optional[str] = None):
        """
        Marks an agent run as 'COMPLETED' and updates its finished timestamp and PR details.
        Args:
            run_id (int): The unique database run ID.
            finished_at (str): ISO formatted finish timestamp.
            pr_number (Optional[int]): The generated pull request number.
            pr_url (Optional[str]): The URL of the pull request on GitHub.
            pr_status (str): Current PR status ('NONE', 'PENDING', 'MERGED', 'CLOSED').
            summary_message (Optional[str]): The output final summary message from the agent.
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE agent_runs
                SET status = 'COMPLETED', finished_at = ?, pr_number = ?, pr_url = ?, pr_status = ?, summary_message = ?
                WHERE id = ?
            """, (finished_at, pr_number, pr_url, pr_status, summary_message, run_id))
            conn.commit()

    def fail_run(self, run_id: int, finished_at: str, error_message: str, summary_message: Optional[str] = None):
        """
        Marks an agent run as 'FAILED' and records the error message.
        Args:
            run_id (int): The unique database run ID.
            finished_at (str): ISO formatted finish timestamp.
            error_message (str): Detailed error trace or description.
            summary_message (Optional[str]): Final summary explaining failure reasons.
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE agent_runs
                SET status = 'FAILED', finished_at = ?, error_message = ?, summary_message = ?
                WHERE id = ?
            """, (finished_at, error_message, summary_message, run_id))
            conn.commit()

    def timeout_run(self, run_id: int, finished_at: str, summary_message: Optional[str] = None):
        """
        Marks an agent run as 'TIMEOUT' when it exceeds the runtime limit.
        Args:
            run_id (int): The unique database run ID.
            finished_at (str): ISO formatted finish timestamp.
            summary_message (Optional[str]): Final summary explaining the timeout.
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE agent_runs
                SET status = 'TIMEOUT', finished_at = ?, summary_message = ?
                WHERE id = ?
            """, (finished_at, summary_message, run_id))
            conn.commit()

    def interrupt_run(self, run_id: int, finished_at: str, error_message: str, summary_message: Optional[str] = None):
        """
        Marks a run as INTERRUPTED due to an orchestrator restart mid-run.
        Args:
            run_id (int): The unique database run ID.
            finished_at (str): ISO formatted finish timestamp.
            error_message (str): Description of the interruption event.
            summary_message (Optional[str]): Context or alert message about restart recovery.
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE agent_runs
                SET status = 'INTERRUPTED', finished_at = ?, error_message = ?, summary_message = ?
                WHERE id = ?
            """, (finished_at, error_message, summary_message, run_id))
            conn.commit()

    def set_pr_info(self, run_id: int, pr_number: int, pr_url: str, pr_status: str = 'PENDING'):
        """
        Updates the PR details for an active or completed run.
        Args:
            run_id (int): The unique database run ID.
            pr_number (int): Pull request number.
            pr_url (str): Pull request web URL.
            pr_status (str): The state status of the PR (e.g. 'PENDING').
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE agent_runs
                SET pr_number = ?, pr_url = ?, pr_status = ?
                WHERE id = ?
            """, (pr_number, pr_url, pr_status, run_id))
            conn.commit()

    def update_pr_status(self, run_id: int, pr_status: str):
        """
        Updates the PR status flag of a run.
        Args:
            run_id (int): The unique database run ID.
            pr_status (str): The new PR state status ('NONE', 'PENDING', 'MERGED', 'CLOSED').
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE agent_runs
                SET pr_status = ?
                WHERE id = ?
            """, (pr_status, run_id))
            conn.commit()

    def get_current_run(self) -> Optional[Dict[str, Any]]:
        """
        Fetches the most recent run with status 'RUNNING'.
        Returns:
            Optional[Dict[str, Any]]: A dictionary of the row if found, otherwise None.
        """
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM agent_runs WHERE status = 'RUNNING' ORDER BY id DESC LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    def get_last_run_for_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Gets the last run details recorded for a given agent.
        Args:
            agent_id (str): The ID of the agent (e.g. 'bolt', 'sentinel').
        Returns:
            Optional[Dict[str, Any]]: A dictionary representing the last run row, or None.
        """
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM agent_runs WHERE agent_id = ? ORDER BY id DESC LIMIT 1
            """, (agent_id,)).fetchone()
            return dict(row) if row else None

    def get_pending_prs(self) -> List[Dict[str, Any]]:
        """
        Retrieves all runs whose PR state status is currently 'PENDING'.
        Returns:
            List[Dict[str, Any]]: A list of dictionaries representing pending runs.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM agent_runs WHERE pr_status = 'PENDING'
            """).fetchall()
            return [dict(r) for r in rows]

    def get_state(self, key: str) -> Optional[str]:
        """
        Retrieves a value from the general orchestrator state key-value table.
        Args:
            key (str): The unique configuration key.
        Returns:
            Optional[str]: The string value if key is found, otherwise None.
        """
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM orchestrator_state WHERE key = ?", (key,)).fetchone()
            return row['value'] if row else None

    def set_state(self, key: str, value: str):
        """
        Saves or updates a key-value pair in the general orchestrator state table.
        Args:
            key (str): The unique configuration key.
            value (str): The value to store.
        """
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO orchestrator_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))
            conn.commit()
