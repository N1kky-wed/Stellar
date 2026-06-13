# test_orchestrator_memory.py
import pytest
import os
import json
import tempfile
import datetime
from orchestrator.memory import MemoryDB

@pytest.fixture
def temp_db():
    """Fixture to create and clean up a temporary SQLite database for MemoryDB testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    yield path
    os.close(fd)
    if os.path.exists(path):
        os.unlink(path)

def test_init_db(temp_db):
    """Asserts that initializing MemoryDB creates the proper tables and enables WAL mode."""
    memory_db = MemoryDB(temp_db)
    with memory_db._get_conn() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        assert "agent_memories" in table_names
        assert "agent_messages" in table_names
        assert "agent_tasks" in table_names
        assert "agent_facts" in table_names

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

def test_add_memory(temp_db):
    """Asserts that memories can be added, tagged, and queried properly, verifying JSON serialization of tags."""
    memory_db = MemoryDB(temp_db)
    
    mem_id = memory_db.add_memory("newton", 123, "observation", "Found a bug", tags=["test", "unit"])
    assert mem_id > 0
    
    memories = memory_db.get_recent_memories(limit=10)
    assert len(memories) == 1
    assert memories[0]["agent_id"] == "newton"
    assert memories[0]["content"] == "Found a bug"
    assert json.loads(memories[0]["tags"]) == ["test", "unit"]
    
    mem_id2 = memory_db.add_memory("newton", 123, "warning", "High CPU usage")
    assert mem_id2 > mem_id
    
    memories2 = memory_db.get_recent_memories(limit=10)
    assert len(memories2) == 2
    assert memories2[0]["memory_type"] == "warning"
    assert memories2[0]["tags"] is None

def test_add_message(temp_db):
    """Asserts that group messages and direct messages are correctly recorded and retrievable by channel."""
    memory_db = MemoryDB(temp_db)
    
    msg_id = memory_db.add_message(
        channel="group",
        sender_id="newton",
        content="Hello team",
        message_type="text"
    )
    assert msg_id > 0
    
    dm_id = memory_db.add_message(
        channel="dm",
        sender_id="admin",
        content="Fix this bug",
        recipient_id="newton",
        thread_id="resolve:task:1",
        message_type="task_ref",
        ref_id="1"
    )
    assert dm_id > msg_id
    
    group_msgs = memory_db.get_recent_group_messages(hours=1)
    assert len(group_msgs) == 1
    assert group_msgs[0]["sender_id"] == "newton"
    assert group_msgs[0]["content"] == "Hello team"

def test_create_task(temp_db):
    """Asserts that agent tasks can be successfully created with metadata and defaults."""
    memory_db = MemoryDB(temp_db)
    
    task_id = memory_db.create_task(
        title="Fix core leak",
        description="A memory leak in state",
        created_by="lucios",
        assigned_to="bolt",
        priority="critical",
        tags=["performance", "leak"],
        related_pr=42,
        related_file="state.py"
    )
    assert task_id > 0
    
    tasks = memory_db.get_active_tasks()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Fix core leak"
    assert tasks[0]["priority"] == "critical"
    assert json.loads(tasks[0]["tags"]) == ["performance", "leak"]
    assert tasks[0]["related_pr"] == 42
    assert tasks[0]["related_file"] == "state.py"
    assert tasks[0]["status"] == "open"

def test_update_task_status(temp_db):
    """Asserts that task status changes follow validation and authorization rules for resolution/verification."""
    memory_db = MemoryDB(temp_db)
    
    task_id = memory_db.create_task(
        title="Fix core leak",
        description="A memory leak in state",
        created_by="lucios",
        assigned_to="bolt",
        priority="critical"
    )
    
    # 1. Update task that does not exist
    assert memory_db.update_task_status(999, "lucios", "resolved") is False
    
    # 2. Update status to 'resolved' by assignee (should automatically set to 'fix_submitted')
    assert memory_db.update_task_status(task_id, "bolt", "resolved") is True
    tasks = memory_db.get_active_tasks()
    assert tasks[0]["status"] == "fix_submitted"
    
    # Reset to open
    assert memory_db.update_task_status(task_id, "admin", "open") is True
    tasks = memory_db.get_active_tasks()
    assert tasks[0]["status"] == "open"
    
    # Try to resolve by unauthorized agent -> returns False
    assert memory_db.update_task_status(task_id, "newton", "resolved") is False
    
    # Resolve by creator
    assert memory_db.update_task_status(task_id, "lucios", "resolved") is True
    assert len(memory_db.get_active_tasks()) == 0
    
    resolved = memory_db.get_resolved_tasks(limit=10)
    assert len(resolved) == 1
    assert resolved[0]["status"] == "resolved"
    assert resolved[0]["resolved_by"] == "lucios"
    
    # 3. Test update status to 'fix_submitted'
    task_id2 = memory_db.create_task(
        title="Another task",
        description="description",
        created_by="admin",
        assigned_to="bolt"
    )
    assert memory_db.update_task_status(task_id2, "newton", "fix_submitted") is False
    assert memory_db.update_task_status(task_id2, "bolt", "fix_submitted") is True
    
    # 4. Test update with invalid status
    assert memory_db.update_task_status(task_id2, "admin", "invalid_status") is False

def test_facts_and_superseding(temp_db):
    """Asserts that facts can be added, updated, and superseded, archiving older duplicates."""
    memory_db = MemoryDB(temp_db)
    
    fact_id = memory_db.add_fact("Original Fact Content", "sentinel", category="convention")
    assert fact_id > 0
    
    active = memory_db.get_active_facts()
    assert len(active) == 1
    assert active[0]["fact"] == "Original Fact Content"
    assert active[0]["category"] == "convention"
    
    assert memory_db.update_fact(999, "New Fact", "bolt") == -1
    
    new_fact_id = memory_db.update_fact(fact_id, "Superseded Fact Content", "bolt")
    assert new_fact_id > fact_id
    
    active = memory_db.get_active_facts()
    assert len(active) == 1
    assert active[0]["fact"] == "Superseded Fact Content"
    assert active[0]["last_updated_by"] == "bolt"
    
    with memory_db._get_conn() as conn:
        old_fact = conn.execute("SELECT * FROM agent_facts WHERE id = ?", (fact_id,)).fetchone()
        assert old_fact["archived"] == 1
        assert old_fact["superseded_by"] == new_fact_id

def test_get_active_tasks(temp_db):
    """Asserts that active tasks are retrieved in descending order of critical -> high -> normal priorities."""
    memory_db = MemoryDB(temp_db)
    
    memory_db.create_task("T1", "desc", "admin", assigned_to="bolt", priority="normal")
    memory_db.create_task("T2", "desc", "admin", assigned_to="bolt", priority="critical")
    memory_db.create_task("T3", "desc", "admin", assigned_to="newton", priority="high")
    
    bolt_tasks = memory_db.get_active_tasks(assigned_to="bolt")
    assert len(bolt_tasks) == 2
    assert bolt_tasks[0]["title"] == "T2"
    assert bolt_tasks[1]["title"] == "T1"
    
    all_tasks = memory_db.get_active_tasks()
    assert len(all_tasks) == 3
    assert all_tasks[0]["title"] == "T2"
    assert all_tasks[1]["title"] == "T3"
    assert all_tasks[2]["title"] == "T1"

def test_get_unread_dms(temp_db):
    """Asserts that active unread DMs are returned, excluding those belonging to resolved threads/tasks."""
    memory_db = MemoryDB(temp_db)
    
    task_id = memory_db.create_task("Task for DMs", "desc", "admin", assigned_to="newton")
    
    memory_db.add_message(
        channel="dm",
        sender_id="admin",
        content="Important instruction",
        recipient_id="newton",
        thread_id=f"resolve:task:{task_id}"
    )
    
    memory_db.add_message(
        channel="dm",
        sender_id="admin",
        content="Other instruction",
        recipient_id="newton",
        thread_id="resolve:invalid"
    )
    
    dms = memory_db.get_unread_dms("newton")
    assert len(dms) == 2
    
    memory_db.update_task_status(task_id, "admin", "resolved")
    
    dms = memory_db.get_unread_dms("newton")
    assert len(dms) == 1
    assert dms[0]["content"] == "Other instruction"
