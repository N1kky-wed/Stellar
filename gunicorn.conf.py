"""
gunicorn.conf.py — Angel Runtime Tracer Bootstrap
===================================================
This file is automatically loaded by Gunicorn as its configuration module
(via the --config flag or by convention when named gunicorn.conf.py in the
working directory).

Its sole purpose is to register the Angel dynamic tracer BEFORE Gunicorn
forks worker processes and imports any application modules. This ensures
every module loaded by the app (app.py, orchestrator/*, etc.) is
automatically instrumented at import time via AST rewriting — with zero
changes to the application source code itself.

How it works:
    1. Gunicorn loads this file in the master process context.
    2. AngelFinder is inserted at the front of sys.meta_path.
    3. When any project module is subsequently imported, AngelFinder
       intercepts the import and runs AngelSourceLoader, which rewrites
       the module's AST to wrap every function with a timing + trace call.
    4. Trace events are sent over a local TCP socket to the Angel Rust
       server (default: 127.0.0.1:9090), which stores them in SQLite.

⚠️  DO NOT REMOVE THIS FILE or the import below.
    Without this bootstrap, Angel receives no runtime trace data and all
    `angel recent`, `angel trace`, and `angel relations` CLI queries will
    return empty results for live traffic.

Dependencies:
    - angel_trace.py  (must live alongside this file in the project root)
    - Angel Rust server running on localhost:9090 (systemd: angel.service)

See also:
    - angel_trace.py       — The AST transformer, finder, and TCP tracer
    - /home/stellaradmin/Angel/  — The Angel Rust CLI + server
"""

import os
import sys

# Insert AngelFinder at the front of the import machinery so it intercepts
# every subsequent project module import and instruments it automatically.
# angel_trace.py must be importable from this directory (project root).
import angel_trace
sys.meta_path.insert(0, angel_trace.AngelFinder(os.path.dirname(os.path.abspath(__file__))))
