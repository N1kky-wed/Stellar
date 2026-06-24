import os
import subprocess
import time
import socket
import json
import sqlite3
import tempfile
import shutil
import pytest

def _resolve_angel_bin():
    # 1. Check if angel is on PATH (installed system-wide or via cargo install)
    which = shutil.which("angel")
    if which:
        return which
    # 2. Fall back to common local build locations relative to this repo
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "Angel", "target", "release", "angel"),
        os.path.join(os.path.dirname(__file__), "..", "..", "Angel", "target", "debug", "angel"),
        os.path.expanduser("~/.cargo/bin/angel"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None

ANGEL_BIN = _resolve_angel_bin()

@pytest.mark.skipif(ANGEL_BIN is None, reason="angel binary not found — skipping integration test (not available in this environment)")
def test_dropped_telemetry_integration():
    # 1. Create a temporary directory and populate it with a dummy python file
    dummy_dir = tempfile.mkdtemp()
    with open(os.path.join(dummy_dir, "dummy.py"), "w") as f:
        f.write("def dummy_func():\n    pass\n")
    
    # 2. Run scan on that directory, which builds and populates dummy_dir/angel.db
    subprocess.run([ANGEL_BIN, "scan", dummy_dir], capture_output=True)
    db_file = os.path.join(dummy_dir, "angel.db")
    
    # 3. Find a free port
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    
    # 4. Start the angel serve daemon on that port using our seeded database
    proc = subprocess.Popen(
        [ANGEL_BIN, "serve", "--db", db_file, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for the server to start
    time.sleep(1.0)
    
    try:
        # 5. Connect to the telemetry socket and send an unknown node_id trace
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_sock.connect(('127.0.0.1', port))
        
        # Send telemetry payload for an unknown node
        payload = {
            "node_id": "unknown_func_123",
            "latency_ns": 15000000,
            "caller": None,
            "trace_id": "trace_abc",
            "thread_name": "Thread-1",
            "is_async": False
        }
        client_sock.sendall((json.dumps(payload) + "\n").encode('utf-8'))
        client_sock.close()
        
        # Give collector some time to process
        time.sleep(0.5)
        
    finally:
        # 6. Stop the daemon
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            
        stdout, stderr = proc.communicate()
        print("Collector stdout:", stdout)
        print("Collector stderr:", stderr)
        
    # 7. Verify warning is printed in stderr
    assert "Dropped telemetry event for unknown node_id 'unknown_func_123'" in stderr
    
    # 8. Check database content directly
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT node_id, drop_count FROM dropped_telemetry")
    rows = cursor.fetchall()
    conn.close()
    
    assert len(rows) == 1
    assert rows[0][0] == "unknown_func_123"
    assert rows[0][1] == 1
    
    # 9. Run angel stats and verify it reports dropped telemetry
    env = os.environ.copy()
    env["ANGEL_DB_PATH"] = db_file
    stats_proc = subprocess.run(
        [ANGEL_BIN, "stats"],
        capture_output=True,
        text=True,
        env=env
    )
    
    print("Stats stdout:", stats_proc.stdout)
    print("Stats stderr:", stats_proc.stderr)
    
    assert stats_proc.returncode == 0
    assert "Dropped Unknown Node ID" in stats_proc.stdout
    assert "unknown_func_123" in stats_proc.stdout
    assert "1" in stats_proc.stdout  # Count

    # Clean up the entire temporary directory
    if os.path.exists(dummy_dir):
        shutil.rmtree(dummy_dir)
