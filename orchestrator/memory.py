# memory.py
"""
Memory database manager for the Stellar Orchestrator.
Maintains agent memories, messaging, task CRUD, and facts using a SQLite database (memory.db).
Publishes live events to Redis for SSE streaming.
"""
import sqlite3
import os
import json
import datetime
from typing import Optional, List, Dict, Any

class MemoryDB:
    """
    Manages database operations for the shared agent memory space, group chats, direct messages, tasks, and facts.
    """
    def __init__(self, db_path: str):
        """
        Initializes MemoryDB and sets up schema tables.
        Args:
            db_path (str): File system path to the memory database.
        """
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        """
        Retrieves a database connection with WAL mode and busy timeout configured.
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
        Builds the tables: agent_memories, agent_messages, agent_tasks, agent_facts.
        Also creates appropriate indexes for query performance optimization.
        """
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._get_conn() as conn:
            # 1. agent_memories
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    run_id INTEGER,
                    memory_type TEXT NOT NULL,       -- 'observation', 'decision', 'outcome', 'warning'
                    content TEXT NOT NULL,
                    scope TEXT DEFAULT 'global',     -- 'global' or agent_id
                    tags TEXT,                       -- JSON string
                    created_at TEXT NOT NULL,
                    archived INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_agent ON agent_memories(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope ON agent_memories(scope, archived)")

            # 2. agent_messages
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,           -- 'group' or 'dm'
                    thread_id TEXT,                  -- groups related DMs into a thread
                    sender_id TEXT NOT NULL,         -- agent_id, 'orchestrator', or 'admin'
                    recipient_id TEXT,               -- NULL for group, agent_id for DMs
                    content TEXT NOT NULL,
                    message_type TEXT DEFAULT 'text',-- 'text', 'task_ref', 'pr_ref', 'system'
                    ref_id TEXT,                     -- optional reference (PR number, task ID, etc.)
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON agent_messages(channel, recipient_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON agent_messages(thread_id)")

            # 3. agent_tasks
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    created_by TEXT NOT NULL,
                    assigned_to TEXT,                -- agent_id or NULL
                    status TEXT DEFAULT 'open',      -- 'open', 'fix_submitted', 'resolved'
                    priority TEXT DEFAULT 'normal',  -- 'low', 'normal', 'high', 'critical'
                    tags TEXT,                       -- JSON string
                    related_pr INTEGER,
                    related_file TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON agent_tasks(assigned_to, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON agent_tasks(status)")

            # 4. agent_facts
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact TEXT NOT NULL,
                    category TEXT,                   -- 'constraint', 'convention', 'architecture', 'bug_pattern'
                    added_by TEXT NOT NULL,          -- agent_id or 'admin'
                    last_updated_by TEXT,            -- agent who last changed this
                    superseded_by INTEGER,           -- FK to newer fact, NULL if current
                    created_at TEXT NOT NULL,
                    last_verified_at TEXT,
                    archived INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_active ON agent_facts(archived, superseded_by)")

            conn.commit()

    def add_memory(self, agent_id: str, run_id: Optional[int], memory_type: str, content: str, scope: str = 'global', tags: Optional[List[str]] = None) -> int:
        """
        Adds a new memory entry for an agent.
        Args:
            agent_id (str): The writing agent.
            run_id (Optional[int]): The run ID associated with the memory.
            memory_type (str): Category like 'observation' or 'decision'.
            content (str): The memory string text.
            scope (str): Visibility scope ('global' or agent_id).
            tags (Optional[List[str]]): Tags to categorize the memory.
        Returns:
            int: The unique row ID of the memory.
        """
        tags_str = json.dumps(tags) if tags else None
        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_memories (agent_id, run_id, memory_type, content, scope, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (agent_id, run_id, memory_type, content, scope, tags_str, now_str))
            conn.commit()
            return cursor.lastrowid

    def add_message(self, channel: str, sender_id: str, content: str, recipient_id: Optional[str] = None, thread_id: Optional[str] = None, message_type: str = 'text', ref_id: Optional[str] = None) -> int:
        """
        Sends/saves a message (group or DM) and publishes group events to Redis.
        Args:
            channel (str): 'group' or 'dm'.
            sender_id (str): Sender identifier.
            content (str): Text content of the message.
            recipient_id (Optional[str]): Recipient agent ID for DMs.
            thread_id (Optional[str]): Logical thread ID grouping related messages.
            message_type (str): Type of message ('text', 'system', etc.).
            ref_id (Optional[str]): Reference ID like PR number or task ID.
        Returns:
            int: The unique row ID of the message.
        """
        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_messages (channel, thread_id, sender_id, recipient_id, content, message_type, ref_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (channel, thread_id, sender_id, recipient_id, content, message_type, ref_id, now_str))
            conn.commit()
            lastrowid = cursor.lastrowid

        # Publish group messages to Redis channel for live streaming updates
        if channel == 'group':
            ts = now_str.replace('T', ' ').split('.')[0]
            sender_name = sender_id.capitalize() if sender_id not in ('admin', 'orchestrator') else sender_id.upper()
            msg_type = 'system' if message_type == 'system' else 'agent'
            if sender_id == 'admin':
                msg_type = 'admin'
            msg_payload = {
                'timestamp': ts,
                'sender': sender_name,
                'content': content,
                'type': msg_type
            }
            try:
                import redis
                import json
                r = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)
                r.publish("agent_events", json.dumps(msg_payload))
            except Exception:
                pass

        return lastrowid

    def create_task(self, title: str, description: Optional[str], created_by: str, assigned_to: Optional[str] = None, priority: str = 'normal', tags: Optional[List[str]] = None, related_pr: Optional[int] = None, related_file: Optional[str] = None) -> int:
        """
        Registers a new task for the engineering team.
        Args:
            title (str): Title of the task.
            description (Optional[str]): Detailed task summary.
            created_by (str): Creator agent ID or admin.
            assigned_to (Optional[str]): Assignee agent ID.
            priority (str): 'low', 'normal', 'high', or 'critical'.
            tags (Optional[List[str]]): Categorization tags.
            related_pr (Optional[int]): Associated PR number.
            related_file (Optional[str]): Associated file path.
        Returns:
            int: The unique task ID.
        """
        now_str = datetime.datetime.now().isoformat()
        tags_str = json.dumps(tags) if tags else None
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_tasks (title, description, created_by, assigned_to, status, priority, tags, related_pr, related_file, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
            """, (title, description, created_by, assigned_to, priority, tags_str, related_pr, related_file, now_str, now_str))
            conn.commit()
            return cursor.lastrowid

    def update_task_status(self, task_id: int, agent_id: str, requested_status: str) -> bool:
        """
        Updates task status with verification loop validation.
        - If requested_status is 'resolved':
          - If agent_id is the creator (created_by) or 'admin' or 'orchestrator', set to 'resolved'.
          - If agent_id is the assignee (assigned_to), set to 'fix_submitted' (verification needed).
          - Otherwise, reject.
        - If requested_status is 'fix_submitted':
          - If agent_id is assignee or creator, set to 'fix_submitted'.
        """
        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            task = conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                return False

            creator = task['created_by']
            assignee = task['assigned_to']
            current_status = task['status']

            new_status = None
            if requested_status == 'resolved':
                if agent_id in (creator, 'admin', 'orchestrator'):
                    new_status = 'resolved'
                elif agent_id == assignee:
                    new_status = 'fix_submitted'
                else:
                    return False  # unauthorized to resolve or fix
            elif requested_status == 'fix_submitted':
                if agent_id in (assignee, creator, 'admin', 'orchestrator'):
                    new_status = 'fix_submitted'
                else:
                    return False
            elif requested_status == 'open':
                new_status = 'open'

            if new_status:
                if new_status == 'resolved':
                    conn.execute("""
                        UPDATE agent_tasks
                        SET status = ?, resolved_by = ?, resolved_at = ?, updated_at = ?
                        WHERE id = ?
                    """, (new_status, agent_id, now_str, now_str, task_id))
                else:
                    conn.execute("""
                        UPDATE agent_tasks
                        SET status = ?, updated_at = ?
                        WHERE id = ?
                    """, (new_status, now_str, task_id))
                conn.commit()
                return True
            return False

    def add_fact(self, fact: str, added_by: str, category: Optional[str] = None) -> int:
        """
        Registers a new fact in the facts database.
        Args:
            fact (str): Fact text message.
            added_by (str): ID of the agent registering the fact.
            category (Optional[str]): Category ('constraint', 'convention', 'architecture', 'bug_pattern').
        Returns:
            int: The new fact row ID.
        """
        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_facts (fact, category, added_by, created_at, last_verified_at)
                VALUES (?, ?, ?, ?, ?)
            """, (fact, category, added_by, now_str, now_str))
            conn.commit()
            return cursor.lastrowid

    def update_fact(self, fact_id: int, new_fact: str, updated_by: str, category: Optional[str] = None) -> int:
        """
        Supersedes an existing fact with a new one by archiving the old and linking it to the new.
        Args:
            fact_id (int): The ID of the fact being superseded.
            new_fact (str): New fact text.
            updated_by (str): ID of the agent updating the fact.
            category (Optional[str]): Category updates if changed.
        Returns:
            int: The ID of the new fact.
        """
        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            old_fact = conn.execute("SELECT * FROM agent_facts WHERE id = ?", (fact_id,)).fetchone()
            if not old_fact:
                return -1

            # Insert new fact
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_facts (fact, category, added_by, last_updated_by, created_at, last_verified_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (new_fact, category or old_fact['category'], old_fact['added_by'], updated_by, now_str, now_str))
            new_id = cursor.lastrowid

            # Mark old fact as superseded and archived
            conn.execute("""
                UPDATE agent_facts
                SET superseded_by = ?, archived = 1
                WHERE id = ?
            """, (new_id, fact_id))
            conn.commit()
            return new_id

    def get_active_tasks(self, assigned_to: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves all unresolved tasks, optionally filtered by assignee.
        Args:
            assigned_to (Optional[str]): Assignee filter.
        Returns:
            List[Dict[str, Any]]: Unresolved task dicts, sorted by priority and ID.
        """
        with self._get_conn() as conn:
            if assigned_to:
                rows = conn.execute("""
                    SELECT * FROM agent_tasks
                    WHERE assigned_to = ? AND status != 'resolved'
                    ORDER BY priority = 'critical' DESC, priority = 'high' DESC, priority = 'normal' DESC, id ASC
                """, (assigned_to,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM agent_tasks
                    WHERE status != 'resolved'
                    ORDER BY priority = 'critical' DESC, priority = 'high' DESC, priority = 'normal' DESC, id ASC
                """).fetchall()
            return [dict(r) for r in rows]

    def get_resolved_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Retrieves recently resolved tasks.
        Args:
            limit (int): Max number of tasks to fetch.
        Returns:
            List[Dict[str, Any]]: Resolved task dictionaries.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM agent_tasks
                WHERE status = 'resolved'
                ORDER BY resolved_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_unread_dms(self, agent_id: str) -> List[Dict[str, Any]]:
        """
        Get all active DM messages for a specific agent.
        Only DM messages belonging to non-resolved tasks/threads.
        Args:
            agent_id (str): Recipient agent ID.
        Returns:
            List[Dict[str, Any]]: Unread DM message dictionaries.
        """
        with self._get_conn() as conn:
            # We exclude messages whose thread_id is resolved.
            # If thread_id starts with 'resolve:task:<id>' and the task is resolved, we exclude it.
            rows = conn.execute("""
                SELECT * FROM agent_messages
                WHERE channel = 'dm' AND recipient_id = ?
                ORDER BY id ASC
            """, (agent_id,)).fetchall()
            
            # Filter out resolved threads
            filtered = []
            for r in rows:
                tid = r['thread_id']
                if tid and tid.startswith('resolve:task:'):
                    try:
                        task_id = int(tid.split(':')[-1])
                        task = conn.execute("SELECT status FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
                        if task and task['status'] == 'resolved':
                            continue  # skip this resolved thread
                    except Exception:
                        pass
                filtered.append(dict(r))
            return filtered

    def get_recent_group_messages(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Retrieves group chat messages recorded within the specified hourly window.
        Args:
            hours (int): Number of hours of history to query.
        Returns:
            List[Dict[str, Any]]: Group message list.
        """
        limit_time = (datetime.datetime.now() - datetime.timedelta(hours=hours)).isoformat()
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM agent_messages
                WHERE channel = 'group' AND created_at >= ?
                ORDER BY id ASC
            """, (limit_time,)).fetchall()
            return [dict(r) for r in rows]

    def get_active_facts(self) -> List[Dict[str, Any]]:
        """
        Retrieves all currently active (non-superseded and non-archived) facts.
        Returns:
            List[Dict[str, Any]]: Active facts.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM agent_facts
                WHERE archived = 0 AND superseded_by IS NULL
                ORDER BY id ASC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_recent_memories(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Retrieves the most recent unarchived agent memories.
        Args:
            limit (int): Max memories to fetch.
        Returns:
            List[Dict[str, Any]]: Unarchived memories.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM agent_memories
                WHERE archived = 0
                ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
