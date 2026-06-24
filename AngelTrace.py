"""
AngelTrace.py — Angel Dynamic Runtime Tracer
===============================================
This module is the Python-side component of the Angel observability system.
It instruments Stellar's application code at import time using AST rewriting,
requiring zero changes to any application source file.

Architecture overview:
                                                  ┌─────────────────────┐
    gunicorn.conf.py                              │  Angel Rust Server  │
        └─ AngelFinder (sys.meta_path hook)       │  localhost:9090     │
              └─ AngelSourceLoader (per-module)   │  SQLite trace store │
                    └─ AngelASTTransformer        └────────┬────────────┘
                          └─ wraps every fn              TCP│socket
                                └─ AngelTracer ──────────────┘
                                      └─ send_trace(node_id, latency_ns)

Components:
    AngelTracer         — Lightweight TCP client. Sends JSON trace events to
                          the Angel Rust server on port 9090. Reconnects
                          automatically on failure. Fire-and-forget; never
                          raises exceptions into application code.

    trace_fn(node_id)   — Optional decorator for manual instrumentation of
                          functions not covered by the automatic AST hook
                          (e.g. lambda-heavy code, C extensions).

    AngelASTTransformer — ast.NodeTransformer that rewrites every function
                          definition (sync and async) to record its start
                          time and send a trace on exit via a try/finally.
                          Skips dunder methods (__init__, __repr__, etc.).

    AngelSourceLoader   — Subclass of SourceFileLoader. Intercepts module
                          source code, runs it through AngelASTTransformer,
                          and compiles the rewritten AST. Falls back to the
                          original source silently on any parse/compile error.

    AngelFinder         — sys.meta_path hook (MetaPathFinder). Intercepts
                          every Python import, checks if the file belongs to
                          the Stellar project root, and substitutes the normal
                          loader with AngelSourceLoader. Explicitly skips:
                            • AngelTrace itself (avoid self-instrumentation)
                            • venv/ directory (third-party packages)
                            • sandbox_runs/ and deployments/ directories

⚠️  DO NOT REMOVE THIS FILE.
    It must live in the Stellar project root alongside gunicorn.conf.py.
    It is loaded by gunicorn.conf.py before any application module is
    imported. Removing it will silently break all Angel CLI tracing.

⚠️  DO NOT INSTRUMENT THIS FILE.
    AngelFinder explicitly skips any module whose name contains "AngelTrace"
    to prevent infinite recursion during self-import.

Usage (automatic — no action needed):
    The tracer is activated automatically via gunicorn.conf.py on every
    Gunicorn startup. No application code needs to be modified.

Usage (manual decoration — optional):
    from AngelTrace import trace_fn

    @trace_fn("my_module::my_function")
    def my_function():
        ...

See also:
    - gunicorn.conf.py          — Bootstrap that registers AngelFinder
    - /home/stellaradmin/Angel/ — Angel Rust CLI server + SQLite store
"""

import time
import socket
import json
import functools
import os
import sys
import ast
import importlib.abc
import importlib.machinery
import importlib.util
import contextvars
import uuid
import builtins
import inspect
import asyncio

# Task-local trace-id context (kept for async correlation)
class ActiveTraceContextVar:
    def __init__(self, name, default=None):
        self._var = contextvars.ContextVar(name, default=default)
        self._active_count = 0

    def get(self, *args, **kwargs):
        if self._active_count == 0:
            return None
        return self._var.get(*args, **kwargs)

    def set(self, value):
        token = self._var.set(value)
        if value is not None:
            self._active_count += 1
        return token

    def reset(self, token):
        self._var.reset(token)
        if self._active_count > 0:
            self._active_count -= 1

active_trace_id = ActiveTraceContextVar("active_trace_id", default=None)

# Task-local call stack ContextVar (supports async isolation via immutable tuples)
active_call_stack = contextvars.ContextVar("active_call_stack", default=None)

# Thread-local cache for thread name
import threading as _threading_mod

class ThreadLocalCache(_threading_mod.local):
    def __init__(self):
        import threading
        self.name = threading.current_thread().name
        self.stack = []

_thread_local = ThreadLocalCache()

_angel_active_stack = contextvars.ContextVar("_angel_active_stack", default=None)

# Bind _angel_enter_fast and _angel_exit_fast
try:
    import sys
    import os
    _curr_dir = os.path.dirname(os.path.abspath(__file__))
    if _curr_dir not in sys.path:
        sys.path.insert(0, _curr_dir)
    import angel_speedup
    angel_speedup.init(_angel_active_stack)
    
    is_async_mode = False
    try:
        import asyncio
        if asyncio._get_running_loop() is not None:
            is_async_mode = True
    except Exception:
        pass
    if not is_async_mode:
        for mod in list(sys.modules.keys()):
            if any(x in mod for x in ("uvicorn", "hypercorn", "daphne", "fastapi", "starlette", "sanic", "tornado")):
                is_async_mode = True
                break

    if is_async_mode:
        _angel_enter_fast = angel_speedup.enter_async
        _angel_exit_fast = angel_speedup.exit_async
        _has_c_stack = False
    else:
        _angel_enter_fast = angel_speedup.enter_sync
        _angel_exit_fast = angel_speedup.exit_sync
        _has_c_stack = True
except ImportError:
    _has_c_stack = False
    def _angel_enter_fast(node_id: str):
        stack = _angel_active_stack.get()
        if stack is None:
            stack = []
            _angel_active_stack.set(stack)
        stack.append(node_id)
        if stack.count(node_id) <= 2:
            return time.perf_counter_ns()
        return None

    def _angel_exit_fast():
        stack = _angel_active_stack.get()
        if stack:
            stack.pop()

def _tl_stack():
    """Return the task-local/thread-local call stack tuple."""
    if _has_c_stack:
        try:
            return angel_speedup.get_stack() or ()
        except Exception:
            pass
    stack = _angel_active_stack.get()
    if stack is not None:
        return tuple(stack)
    if _tl_enter == _tl_enter_async:
        return active_call_stack.get() or ()
    else:
        return tuple(_thread_local.stack)

def _tl_enter_sync(node_id: str) -> int:
    """Ultra-fast synchronous push and depth count."""
    stack = _thread_local.stack
    stack.append(node_id)
    return stack.count(node_id)

def _tl_exit_sync(node_id: str):
    """Ultra-fast synchronous pop."""
    stack = _thread_local.stack
    if stack:
        stack.pop()

def _tl_enter_async(node_id: str) -> int:
    """Task-safe async push and depth count."""
    stack = active_call_stack.get() or ()
    new_stack = stack + (node_id,)
    active_call_stack.set(new_stack)
    return new_stack.count(node_id)

def _tl_exit_async(node_id: str):
    """Task-safe async pop."""
    stack = active_call_stack.get() or ()
    if stack:
        active_call_stack.set(stack[:-1])

# Bind to synchronous implementation by default for maximum performance
_tl_enter = _tl_enter_sync
_tl_exit = _tl_exit_sync


# ---------------------------------------------------------------------------
# TCP Trace Client
# ---------------------------------------------------------------------------

class AngelTracer:
    def __init__(self, host=None, port=None):
        self.host = host or os.environ.get("ANGEL_HOST", "127.0.0.1")
        try:
            self.port = int(port or os.environ.get("ANGEL_PORT", 9090))
        except (ValueError, TypeError):
            self.port = 9090
        self.sock = None
        import collections
        import threading
        self.lock = threading.Lock()
        self.last_connect_attempt = 0.0
        self.connect_cooldown = 5.0  # seconds
        self.registered_nodes = {}
        self.registered_strings = {}
        self.next_node_id = 1
        self.use_binary = os.environ.get("ANGEL_PROTOCOL", "").lower() != "json"
        # Initialised eagerly so that send_trace/send_event never need to call
        # _verify_worker() on the hot path once the worker is running. The hot
        # path checks the single bool self._worker_ready instead of doing
        # hasattr() + thread.is_alive() (which acquires a lock) on every call.
        self.queue = collections.deque(maxlen=50000)
        self.queue_append = self.queue.append
        self._drain_event = threading.Event()
        self._worker_ready = False
        self.worker = None

        # Register fork handler if supported (Python 3.7+ on Unix)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(
                after_in_child=self._handle_fork_in_child
            )

    def _handle_fork_in_child(self):
        # Prevent parent/child socket sharing
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

        # Reset registry state and queues
        self.registered_nodes.clear()
        self.registered_strings.clear()
        self.next_node_id = 1
        self.queue.clear()

        # Mark worker as unstarted so the next trace call spawns a new thread
        self.worker = None
        self._worker_ready = False

        # Reset C-level thread-local state: native_stack[] and node_counts[].
        # Without this, the child inherits the parent's call-stack depth at
        # the moment of fork — stale counts cause incorrect reentrancy gating
        # on the first request in the child process.
        try:
            import angel_speedup
            angel_speedup.reset_thread_state()
        except Exception:
            # C extension unavailable (ImportError) or already unloaded at
            # interpreter shutdown — safe to ignore.
            pass

    def _verify_worker(self):
        """Start the background worker thread if not already running.
        Called at most once per tracer lifetime from the hot path."""
        if not self._worker_ready:
            with self.lock:
                if not self._worker_ready:
                    import threading
                    self.worker = threading.Thread(target=self._worker_loop, daemon=True, name="AngelTracerWorker")
                    self.worker.start()
                    self._worker_ready = True

    def _worker_loop(self):
        drain = self._drain_event
        q = self.queue
        while True:
            # Wait up to 1 second for the hot-path to signal there is work,
            # then clear the flag and drain up to 500 items in one batch.
            drain.wait(timeout=1.0)
            drain.clear()

            batch = []
            try:
                for _ in range(500):
                    batch.append(q.popleft())
            except IndexError:
                pass  # deque exhausted

            if not batch:
                continue

            try:
                with self.lock:
                    if not self.sock:
                        self._connect_locked()
                    if not self.sock:
                        continue
                    serialized_payloads = b"".join(self._serialize_event(item) for item in batch)
                    try:
                        self.sock.sendall(serialized_payloads)
                    except Exception:
                        if self.sock:
                            try:
                                self.sock.close()
                            except Exception:
                                pass
                        self.sock = None
            except Exception:
                pass

    def _serialize_event(self, item):
        # Hot path appends a 6-tuple to avoid dict construction overhead.
        # Unpack it here in the worker thread where cost doesn't matter.
        if isinstance(item, tuple):
            node_id, latency_ns, caller, trace_id, thread_name, is_async = item
        else:
            if not isinstance(item, dict) or "node_id" not in item or "type" in item:
                # Fall back to JSON for non-telemetry events or general dicts
                try:
                    import orjson
                    return orjson.dumps(item) + b"\n"
                except ImportError:
                    try:
                        import json
                        return (json.dumps(item) + "\n").encode("utf-8")
                    except Exception:
                        return b""
            node_id = item.get("node_id")
            latency_ns = item.get("latency_ns", 0)
            caller = item.get("caller")
            trace_id = item.get("trace_id")
            thread_name = item.get("thread_name", "Thread-1")
            is_async = item.get("is_async", False)

        if not self.use_binary:
            d = {"node_id": node_id, "latency_ns": latency_ns,
                 "thread_name": thread_name, "is_async": is_async}
            if caller:     d["caller"]   = caller
            if trace_id:   d["trace_id"] = trace_id
            try:
                import orjson
                return orjson.dumps(d) + b"\n"
            except ImportError:
                try:
                    import json
                    return (json.dumps(d) + "\n").encode("utf-8")
                except Exception:
                    return b""

        import struct
        payloads = []

        # Get or register node_id
        if node_id not in self.registered_nodes:
            self.registered_nodes[node_id] = self.next_node_id
            self.next_node_id = (self.next_node_id + 1) & 0xffffffff
            if self.next_node_id == 0:
                self.next_node_id = 1
            node_bytes = node_id.encode('utf-8', errors='replace')
            payload_len = 7 + len(node_bytes)
            payloads.append(struct.pack(">B H B I H", 0xAB, payload_len, 0x02, self.registered_nodes[node_id], len(node_bytes)) + node_bytes)
        dynamic_id = self.registered_nodes[node_id]

        # Get or register caller
        caller_id = 0
        if caller:
            if caller not in self.registered_nodes:
                self.registered_nodes[caller] = self.next_node_id
                self.next_node_id = (self.next_node_id + 1) & 0xffffffff
                if self.next_node_id == 0:
                    self.next_node_id = 1
                caller_bytes = caller.encode('utf-8', errors='replace')
                payload_len = 7 + len(caller_bytes)
                payloads.append(struct.pack(">B H B I H", 0xAB, payload_len, 0x02, self.registered_nodes[caller], len(caller_bytes)) + caller_bytes)
            caller_id = self.registered_nodes[caller]

        # Get or register trace_id (Type 0x03)
        trace_hash = 0
        if trace_id:
            trace_hash = 0xcbf29ce484222325
            for b in trace_id.encode('utf-8', errors='replace'):
                trace_hash = (trace_hash ^ b) & 0xffffffffffffffff
                trace_hash = (trace_hash * 0x100000001b3) & 0xffffffffffffffff
            if trace_hash not in self.registered_strings:
                self.registered_strings[trace_hash] = trace_id
                trace_bytes = trace_id.encode('utf-8', errors='replace')
                payload_len = 11 + len(trace_bytes)
                payloads.append(struct.pack(">B H B Q H", 0xAB, payload_len, 0x03, trace_hash, len(trace_bytes)) + trace_bytes)

        # Get or register thread_name (Type 0x03)
        thread_hash = 0
        if thread_name:
            thread_hash = 0xcbf29ce484222325
            for b in thread_name.encode('utf-8', errors='replace'):
                thread_hash = (thread_hash ^ b) & 0xffffffffffffffff
                thread_hash = (thread_hash * 0x100000001b3) & 0xffffffffffffffff
            if thread_hash not in self.registered_strings:
                self.registered_strings[thread_hash] = thread_name
                thread_bytes = thread_name.encode('utf-8', errors='replace')
                payload_len = 11 + len(thread_bytes)
                payloads.append(struct.pack(">B H B Q H", 0xAB, payload_len, 0x03, thread_hash, len(thread_bytes)) + thread_bytes)

        # Telemetry packet
        flags = 0x01 if is_async else 0x00
        telemetry_payload = struct.pack(">B H B I Q I Q Q B", 0xAB, 34, 0x01, dynamic_id, latency_ns, caller_id, trace_hash, thread_hash, flags)
        payloads.append(telemetry_payload)

        return b"".join(payloads)

    def connect(self):
        now = time.monotonic()
        if self.last_connect_attempt != 0.0 and now - self.last_connect_attempt < self.connect_cooldown:
            return
        with self.lock:
            self._connect_locked()

    def _connect_locked(self):
        if self.sock:
            return
        now = time.monotonic()
        if self.last_connect_attempt != 0.0 and now - self.last_connect_attempt < self.connect_cooldown:
            return
        self.last_connect_attempt = now
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(1.0)
            self.sock.connect((self.host, self.port))
            self.registered_nodes.clear()
            self.registered_strings.clear()
            self.next_node_id = 1
        except Exception:
            # Bolt - Stability: Explicitly close the socket to prevent file descriptor leaks
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None

    def send_trace(self, node_id: str, latency_ns: int, trace_id: str = None, caller_id: str = None):
        """Hot-path trace emitter.

        caller_id: static string injected by the AST transformer at compile
        time. When present, the entire sys._getframe() stack walk is skipped.
        Falls back to the thread-local call stack and finally to frame
        inference only for uninstrumented callers.
        """
        # _worker_ready is a plain bool attribute read — ~20ns.
        # _verify_worker() (hasattr + thread.is_alive()) is only called once
        """Hot-path trace emitter."""
        if not self._worker_ready:
            self._verify_worker()

        caller = caller_id
        if not caller:
            stack = _tl_stack()
            if stack:
                if len(stack) >= 2:
                    caller = stack[-2]
                elif stack[0] != node_id:
                    caller = stack[0]
        if not caller:
            caller = self._infer_caller_from_frames(node_id)

        if trace_id is None and active_trace_id._active_count > 0:
            trace_id = active_trace_id._var.get()

        thread_name = _thread_local.name

        is_async = False
        if trace_id is not None:
            loop = asyncio._get_running_loop()
            if loop is not None:
                try:
                    task = asyncio.current_task(loop)
                    if task is not None:
                        is_async = True
                        thread_name = f"{thread_name}:{task.get_name()}"
                except Exception:
                    pass

        was_empty = not self.queue
        self.queue_append((node_id, latency_ns, caller, trace_id, thread_name, is_async))
        if was_empty:
            self._drain_event.set()

    def _infer_caller_from_frames(self, node_id: str):
        try:
            target_fn = node_id
            if "::" in target_fn:
                target_fn = target_fn.split("::")[-1]
            elif ":" in target_fn:
                target_fn = target_fn.split(":")[-1]

            # Walk the call stack looking for the first user-code frame.
            # Start at depth=1 (frame 0 = send_trace itself).
            # Skip frames from Python internals AND from AngelTrace / angel_trace
            # (our monkeypatching wrappers) to land on actual user-code.
            _INTERNAL_MODULES = {
                "threading", "asyncio", "asyncio.tasks", "asyncio.futures",
                "asyncio.base_events", "asyncio.coroutines", "concurrent.futures",
                "AngelTrace", "angel_trace",
            }
            _INTERNAL_NAMES = {
                "_bootstrap", "_bootstrap_inner", "_bootstrap_outer",
                "_call_with_frames_cleaned", "_patched_thread_run",
                "_patched_thread_init",
            }
            depth = 1
            skipped_target = False
            while True:
                try:
                    f = sys._getframe(depth)
                except ValueError:
                    break
                co_name = f.f_code.co_name
                co_filename = os.path.abspath(f.f_code.co_filename)
                module = f.f_globals.get("__name__", "")
                if (module in _INTERNAL_MODULES
                        or co_name in _INTERNAL_NAMES
                        or "lib/python" in co_filename.replace("\\", "/")
                        or co_name == "<module>"):
                    depth += 1
                    continue
                
                # Skip the instrumented function's own frame once to find its caller
                if co_name == target_fn and not skipped_target:
                    skipped_target = True
                    depth += 1
                    continue

                # Found a user-code frame
                if hasattr(self, 'project_root') and co_filename.startswith(self.project_root):
                    rel_path = os.path.relpath(co_filename, self.project_root)
                    return f"{rel_path}::{co_name}"
                else:
                    return co_name
        except Exception:
            return None
        return None

    def send_event(self, payload: dict):
        if not self._worker_ready:
            self._verify_worker()
        try:
            q = self.queue
            was_empty = len(q) == 0
            q.append(payload)
            if was_empty:
                self._drain_event.set()
        except Exception:
            pass


# Global default instance
tracer = AngelTracer()
_angel_send_fast = tracer.send_trace

def trace_fn(node_id: str):
    """
    Decorator to instrument a python function manually.
    """
    def decorator(func):
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                trace_id = active_trace_id.get()
                if not trace_id:
                    trace_id = uuid.uuid4().hex
                    token = active_trace_id.set(trace_id)
                    try:
                        stack_token = active_call_stack.set(active_call_stack.get() + (node_id,))
                        start = time.perf_counter_ns()
                        try:
                            try:
                                return await func(*args, **kwargs)
                            finally:
                                duration = time.perf_counter_ns() - start
                                tracer.send_trace(node_id, duration, trace_id)
                        finally:
                            active_call_stack.reset(stack_token)
                    finally:
                        active_trace_id.reset(token)
                else:
                    stack_token = active_call_stack.set(active_call_stack.get() + (node_id,))
                    start = time.perf_counter_ns()
                    try:
                        try:
                            return await func(*args, **kwargs)
                        finally:
                            duration = time.perf_counter_ns() - start
                            tracer.send_trace(node_id, duration, trace_id)
                    finally:
                        active_call_stack.reset(stack_token)
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            trace_id = active_trace_id.get()
            if not trace_id:
                trace_id = uuid.uuid4().hex
                token = active_trace_id.set(trace_id)
                try:
                    stack_token = active_call_stack.set(active_call_stack.get() + (node_id,))
                    start = time.perf_counter_ns()
                    try:
                        try:
                            return func(*args, **kwargs)
                        finally:
                            duration = time.perf_counter_ns() - start
                            tracer.send_trace(node_id, duration, trace_id)
                    finally:
                        active_call_stack.reset(stack_token)
                finally:
                    active_trace_id.reset(token)
            else:
                stack_token = active_call_stack.set(active_call_stack.get() + (node_id,))
                start = time.perf_counter_ns()
                try:
                    try:
                        return func(*args, **kwargs)
                    finally:
                        duration = time.perf_counter_ns() - start
                        tracer.send_trace(node_id, duration, trace_id)
                finally:
                    active_call_stack.reset(stack_token)
        return wrapper
    return decorator


# --- DISTRIBUTED TRACING MONKEYPATCHES ---
# Strategy: use a background daemon thread that polls sys.modules and applies
# patches once the target libraries are fully initialized. This avoids all
# circular-import and recursion issues caused by hooking builtins.__import__.

_patched_flask = False
_patched_django = False
_patched_fastapi = False
_patched_requests = False
_patched_httpx = False
_patched_aiohttp = False

def patch_flask():
    global _patched_flask
    if _patched_flask:
        return True
    try:
        flask = sys.modules.get('flask')
        if flask is None or not hasattr(flask, 'Flask'):
            return False

        original_wsgi_app = flask.Flask.wsgi_app
        # Guard against double-patching
        if getattr(original_wsgi_app, '_angel_patched', False):
            _patched_flask = True
            return True

        sys.stderr.write("[angel] Patching flask.Flask.wsgi_app...\n")

        def wrapped_wsgi_app(self, environ, start_response):
            # WSGI header names come in as HTTP_X_ANGEL_TRACE_ID
            trace_id = (
                environ.get('HTTP_X_ANGEL_TRACE_ID') or
                environ.get('HTTP_X_ANGEL_TRACEID') or
                environ.get('x-angel-trace-id')
            )
            if not trace_id:
                trace_id = uuid.uuid4().hex
                sys.stderr.write(f"[angel] WSGI: no incoming trace_id, generated {trace_id}\n")
            else:
                sys.stderr.write(f"[angel] WSGI: extracted incoming trace_id={trace_id}\n")

            token = active_trace_id.set(trace_id)
            try:
                return original_wsgi_app(self, environ, start_response)
            finally:
                active_trace_id.reset(token)

        wrapped_wsgi_app._angel_patched = True
        flask.Flask.wsgi_app = wrapped_wsgi_app

        # Patch stream_with_context to propagate trace ID context to SSE generators
        if hasattr(flask, 'stream_with_context'):
            sys.stderr.write("[angel] Patching flask.stream_with_context...\n")
            original_swc = flask.stream_with_context

            def wrap_generator(gen, t_id):
                try:
                    while True:
                        if t_id:
                            token = active_trace_id.set(t_id)
                            try:
                                val = next(gen)
                            finally:
                                active_trace_id.reset(token)
                        else:
                            val = next(gen)
                        yield val
                except StopIteration:
                    return

            def patched_swc(generator_or_function):
                t_id = active_trace_id.get()
                import inspect
                
                # If it's a generator iterator
                if inspect.isgenerator(generator_or_function):
                    return original_swc(wrap_generator(generator_or_function, t_id))
                
                # If it's a generator function
                elif inspect.isgeneratorfunction(generator_or_function):
                    @functools.wraps(generator_or_function)
                    def wrapped_func(*args, **kwargs):
                        gen = generator_or_function(*args, **kwargs)
                        return wrap_generator(gen, t_id)
                    return original_swc(wrapped_func)
                
                else:
                    # Generic callback/callable
                    res = original_swc(generator_or_function)
                    if inspect.isgenerator(res):
                        return wrap_generator(res, t_id)
                    return res

            flask.stream_with_context = patched_swc

        _patched_flask = True
        sys.stderr.write("[angel] Flask patched successfully!\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[angel] Warning: Failed to patch flask: {e}\n")
        return False

def patch_django():
    global _patched_django
    if _patched_django:
        return True
    try:
        django_wsgi = sys.modules.get('django.core.handlers.wsgi')
        if django_wsgi is None or not hasattr(django_wsgi, 'WSGIHandler'):
            return False

        original_wsgi_call = django_wsgi.WSGIHandler.__call__
        if getattr(original_wsgi_call, '_angel_patched', False):
            _patched_django = True
            return True

        sys.stderr.write("[angel] Patching django.core.handlers.wsgi.WSGIHandler.__call__...\n")

        def wrapped_wsgi_call(self, environ, start_response):
            trace_id = (
                environ.get('HTTP_X_ANGEL_TRACE_ID') or
                environ.get('HTTP_X_ANGEL_TRACEID') or
                environ.get('x-angel-trace-id')
            )
            if not trace_id:
                trace_id = uuid.uuid4().hex
            token = active_trace_id.set(trace_id)
            try:
                return original_wsgi_call(self, environ, start_response)
            finally:
                active_trace_id.reset(token)

        wrapped_wsgi_call._angel_patched = True
        django_wsgi.WSGIHandler.__call__ = wrapped_wsgi_call
        _patched_django = True
        sys.stderr.write("[angel] Django patched successfully!\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[angel] Warning: Failed to patch django: {e}\n")
        return False

def patch_fastapi():
    global _patched_fastapi
    if _patched_fastapi:
        return True
    try:
        starlette_app = sys.modules.get('starlette.applications')
        if starlette_app is None or not hasattr(starlette_app, 'Starlette'):
            return False

        original_asgi_call = starlette_app.Starlette.__call__
        if getattr(original_asgi_call, '_angel_patched', False):
            _patched_fastapi = True
            return True

        sys.stderr.write("[angel] Patching starlette.applications.Starlette.__call__...\n")

        async def wrapped_asgi_call(self, scope, receive, send):
            if scope.get("type") == "http":
                headers = dict(scope.get("headers", []))
                trace_id_bytes = headers.get(b"x-angel-trace-id") or headers.get(b"x-angel-traceid")
                trace_id = trace_id_bytes.decode("utf-8") if trace_id_bytes else None
                if not trace_id:
                    trace_id = uuid.uuid4().hex
                token = active_trace_id.set(trace_id)
                try:
                    await original_asgi_call(self, scope, receive, send)
                finally:
                    active_trace_id.reset(token)
            else:
                await original_asgi_call(self, scope, receive, send)

        wrapped_asgi_call._angel_patched = True
        starlette_app.Starlette.__call__ = wrapped_asgi_call
        _patched_fastapi = True
        sys.stderr.write("[angel] FastAPI/Starlette patched successfully!\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[angel] Warning: Failed to patch FastAPI/Starlette: {e}\n")
        return False

def patch_requests():
    global _patched_requests
    if _patched_requests:
        return True
    try:
        requests = sys.modules.get('requests')
        if requests is None or not hasattr(requests, 'Session'):
            return False

        original_request = requests.Session.request
        if getattr(original_request, '_angel_patched', False):
            _patched_requests = True
            return True

        sys.stderr.write("[angel] Patching requests.Session.request...\n")

        def wrapped_request(self, method, url, *args, **kwargs):
            trace_id = active_trace_id.get()
            if trace_id:
                headers = kwargs.get('headers')
                if headers is None:
                    headers = {}
                    kwargs['headers'] = headers
                has_header = any(
                    k.lower() == 'x-angel-trace-id'
                    for k in (headers.keys() if hasattr(headers, 'keys') else [])
                )
                if not has_header:
                    headers['x-angel-trace-id'] = trace_id
            return original_request(self, method, url, *args, **kwargs)

        wrapped_request._angel_patched = True
        requests.Session.request = wrapped_request
        _patched_requests = True
        sys.stderr.write("[angel] Requests patched successfully!\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[angel] Warning: Failed to patch requests: {e}\n")
        return False

def patch_httpx():
    global _patched_httpx
    if _patched_httpx:
        return True
    try:
        httpx = sys.modules.get('httpx')
        if httpx is None:
            return False

        # Patch sync client
        if hasattr(httpx, 'Client'):
            original_request_sync = httpx.Client.request
            if not getattr(original_request_sync, '_angel_patched', False):
                def wrapped_request_sync(self, method, url, *args, **kwargs):
                    trace_id = active_trace_id.get()
                    if trace_id:
                        headers = kwargs.get('headers')
                        if headers is None:
                            headers = {}
                            kwargs['headers'] = headers
                        if hasattr(headers, 'keys'):
                            has_header = any(k.lower() == 'x-angel-trace-id' for k in headers.keys())
                            if not has_header:
                                headers['x-angel-trace-id'] = trace_id
                        elif isinstance(headers, list):
                            has_header = any(k.lower() == b'x-angel-trace-id' or k.lower() == 'x-angel-trace-id' for k, v in headers)
                            if not has_header:
                                headers.append(('x-angel-trace-id', trace_id))
                    return original_request_sync(self, method, url, *args, **kwargs)
                wrapped_request_sync._angel_patched = True
                httpx.Client.request = wrapped_request_sync

        # Patch async client
        if hasattr(httpx, 'AsyncClient'):
            original_request_async = httpx.AsyncClient.request
            if not getattr(original_request_async, '_angel_patched', False):
                async def wrapped_request_async(self, method, url, *args, **kwargs):
                    trace_id = active_trace_id.get()
                    if trace_id:
                        headers = kwargs.get('headers')
                        if headers is None:
                            headers = {}
                            kwargs['headers'] = headers
                        if hasattr(headers, 'keys'):
                            has_header = any(k.lower() == 'x-angel-trace-id' for k in headers.keys())
                            if not has_header:
                                headers['x-angel-trace-id'] = trace_id
                        elif isinstance(headers, list):
                            has_header = any(k.lower() == b'x-angel-trace-id' or k.lower() == 'x-angel-trace-id' for k, v in headers)
                            if not has_header:
                                headers.append(('x-angel-trace-id', trace_id))
                    return await original_request_async(self, method, url, *args, **kwargs)
                wrapped_request_async._angel_patched = True
                httpx.AsyncClient.request = wrapped_request_async

        _patched_httpx = True
        sys.stderr.write("[angel] HTTPX patched successfully!\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[angel] Warning: Failed to patch HTTPX: {e}\n")
        return False

def patch_aiohttp():
    global _patched_aiohttp
    if _patched_aiohttp:
        return True
    try:
        aiohttp = sys.modules.get('aiohttp')
        if aiohttp is None or not hasattr(aiohttp, 'ClientSession'):
            return False

        original_request = aiohttp.ClientSession._request
        if getattr(original_request, '_angel_patched', False):
            _patched_aiohttp = True
            return True

        sys.stderr.write("[angel] Patching aiohttp.ClientSession._request...\n")

        async def wrapped_request(self, method, str_or_url, *args, **kwargs):
            trace_id = active_trace_id.get()
            if trace_id:
                headers = kwargs.get('headers')
                if headers is None:
                    headers = {}
                    kwargs['headers'] = headers
                if hasattr(headers, 'keys'):
                    has_header = any(k.lower() == 'x-angel-trace-id' for k in headers.keys())
                    if not has_header:
                        headers['x-angel-trace-id'] = trace_id
                elif isinstance(headers, list):
                    has_header = any(k.lower() == b'x-angel-trace-id' or k.lower() == 'x-angel-trace-id' for k, v in headers)
                    if not has_header:
                        headers.append(('x-angel-trace-id', trace_id))
            return await original_request(self, method, str_or_url, *args, **kwargs)

        wrapped_request._angel_patched = True
        aiohttp.ClientSession._request = wrapped_request
        _patched_aiohttp = True
        sys.stderr.write("[angel] aiohttp patched successfully!\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[angel] Warning: Failed to patch aiohttp: {e}\n")
        return False

def _patch_worker():
    """Background thread: retries patching until targets are patched."""
    import time as _time
    for _ in range(120):   # up to ~60 s
        patch_requests()
        patch_httpx()
        patch_aiohttp()
        patch_flask()
        patch_django()
        patch_fastapi()
        _time.sleep(0.5)

def start_patcher_thread():
    global _patched_flask, _patched_django, _patched_fastapi, _patched_requests, _patched_httpx, _patched_aiohttp
    _patched_flask = False
    _patched_django = False
    _patched_fastapi = False
    _patched_requests = False
    _patched_httpx = False
    _patched_aiohttp = False

    import threading as _threading
    for t in _threading.enumerate():
        if t.name == "angel-patcher":
            return

    _t = _threading.Thread(target=_patch_worker, daemon=True, name="angel-patcher")
    _t.start()

start_patcher_thread()


# --- THREAD CONTEXT PROPAGATION PATCH ---
import threading as _threading_orig

_orig_thread_init = _threading_orig.Thread.__init__
_orig_thread_run = _threading_orig.Thread.run

def _patched_thread_init(self, *args, **kwargs):
    if not hasattr(self, '_angel_trace_id') or getattr(self, '_angel_trace_id') is None:
        self._angel_trace_id = active_trace_id.get()
    _orig_thread_init(self, *args, **kwargs)

def _patched_thread_run(self):
    trace_id = getattr(self, '_angel_trace_id', None)
    if trace_id:
        token = active_trace_id.set(trace_id)
        try:
            _orig_thread_run(self)
        finally:
            active_trace_id.reset(token)
    else:
        _orig_thread_run(self)

_threading_orig.Thread.__init__ = _patched_thread_init
_threading_orig.Thread.run = _patched_thread_run


# --- ZERO-CODE-CHANGE RUNTIME INSTRUMENTATION HOOK ---

class GeneratorChecker(ast.NodeVisitor):
    def __init__(self):
        self.found = False
    def visit_FunctionDef(self, node):
        pass
    def visit_AsyncFunctionDef(self, node):
        pass
    def visit_Yield(self, node):
        self.found = True
    def visit_YieldFrom(self, node):
        self.found = True


class AngelASTTransformer(ast.NodeTransformer):
    def __init__(self, relative_path):
        self.relative_path = relative_path
        self.scope_stack = []

    def visit_ClassDef(self, node):
        self.scope_stack.append(node.name)
        res = self.generic_visit(node)
        self.scope_stack.pop()
        return res

    def visit_FunctionDef(self, node):
        self.scope_stack.append(node.name)
        res = self.instrument_function(node)
        self.scope_stack.pop()
        return res

    def visit_AsyncFunctionDef(self, node):
        self.scope_stack.append(node.name)
        res = self.instrument_function(node)
        self.scope_stack.pop()
        return res

    def get_route_info(self, decorator):
        if not isinstance(decorator, ast.Call):
            return None
        
        # Check if decorator.func is something.route or route
        is_route = False
        if isinstance(decorator.func, ast.Attribute):
            if decorator.func.attr == "route":
                is_route = True
        elif isinstance(decorator.func, ast.Name):
            if decorator.func.id == "route":
                is_route = True
                
        if not is_route:
            return None
            
        # Get path expression (first positional arg)
        path_expr = None
        if len(decorator.args) > 0:
            path_expr = decorator.args[0]
        else:
            path_expr = ast.Constant(value="/")
            
        # Get methods expression (methods=...)
        methods_expr = None
        for kw in decorator.keywords:
            if kw.arg == "methods":
                methods_expr = kw.value
                break
                
        if methods_expr is None:
            methods_expr = ast.List(elts=[ast.Constant(value="GET")], ctx=ast.Load())
            
        return path_expr, methods_expr

    def is_generator(self, node):
        checker = GeneratorChecker()
        for child in node.body:
            checker.visit(child)
        return checker.found

    def instrument_function(self, node):
        self.generic_visit(node)

        # Skip special/internal dunder functions
        if node.name.startswith("__") and node.name.endswith("__"):
            return node

        # Skip generator functions to avoid generator GC/lifetime timing pollution
        if self.is_generator(node):
            return node

        qname = "::".join(self.scope_stack)
        node_id = f"{self.relative_path}::{qname}"

        # The static caller_id is the node_id of the *enclosing* scope — the
        # parent frame in scope_stack. For top-level functions this is None
        # (caller is external/uninstrumented). This is resolved at AST-rewrite
        # time (import time) so send_trace pays zero cost at call time.
        if len(self.scope_stack) >= 2:
            parent_qname = "::".join(self.scope_stack[:-1])
            static_caller_id = f"{self.relative_path}::{parent_qname}"
        else:
            static_caller_id = None

        # Collect route registrations to emit at import time
        route_send_statements = []
        for decorator in node.decorator_list:
            route_info = self.get_route_info(decorator)
            if route_info:
                path_expr, methods_expr = route_info

                dict_keys = [
                    ast.Constant(value="type"),
                    ast.Constant(value="path"),
                    ast.Constant(value="methods"),
                    ast.Constant(value="handler")
                ]
                dict_values = [
                    ast.Constant(value="route"),
                    path_expr,
                    methods_expr,
                    ast.Constant(value=node_id)
                ]

                route_dict = ast.Dict(keys=dict_keys, values=dict_values)

                route_send_stmt = ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(
                                value=ast.Name(id="_angel_trace_mod", ctx=ast.Load()),
                                attr="tracer",
                                ctx=ast.Load()
                            ),
                            attr="send_event",
                            ctx=ast.Load()
                        ),
                        args=[route_dict],
                        keywords=[]
                    )
                )
                route_send_statements.append(route_send_stmt)

        # _angel_t0 = _angel_enter(node_id)
        enter_call = ast.Assign(
            targets=[ast.Name(id="_angel_t0", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Name(id="_angel_enter", ctx=ast.Load()),
                args=[ast.Constant(value=node_id)],
                keywords=[]
            )
        )

        # 3. timer_diff = _angel_perf() - _angel_t0
        timer_diff = ast.BinOp(
            left=ast.Call(
                func=ast.Name(id="_angel_perf", ctx=ast.Load()),
                args=[],
                keywords=[]
            ),
            op=ast.Sub(),
            right=ast.Name(id="_angel_t0", ctx=ast.Load())
        )

        # 4. _angel_send(node_id, timer_diff, None, caller_id)
        #    Pass parameters positionally to bypass all keyword argument overhead.
        send_args = [
            ast.Constant(value=node_id),
            timer_diff
        ]
        if static_caller_id is not None:
            send_args.append(ast.Constant(value=None))
            send_args.append(ast.Constant(value=static_caller_id))
        send_call = ast.Expr(
            value=ast.Call(
                func=ast.Name(id="_angel_send", ctx=ast.Load()),
                args=send_args,
                keywords=[]
            )
        )

        # _angel_exit()
        exit_call = ast.Expr(
            value=ast.Call(
                func=ast.Name(id="_angel_exit", ctx=ast.Load()),
                args=[],
                keywords=[]
            )
        )

        # if _angel_t0 is not None:
        #     _angel_send(...)
        send_if = ast.If(
            test=ast.Compare(
                left=ast.Name(id="_angel_t0", ctx=ast.Load()),
                ops=[ast.IsNot()],
                comparators=[ast.Constant(value=None)]
            ),
            body=[send_call],
            orelse=[]
        )

        inner_try = ast.Try(
            body=node.body,
            handlers=[],
            orelse=[],
            finalbody=[exit_call, send_if]
        )

        node.body = [enter_call, inner_try]

        if route_send_statements:
            return [node] + route_send_statements
        else:
            return node


class AngelSourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, relative_path):
        self.relative_path = relative_path
        super().__init__(fullname, path)

    def get_code(self, fullname):
        filepath = self.get_filename(fullname)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                source = f.read()
        except Exception:
            return super().get_code(fullname)

        try:
            tree = ast.parse(source)
            
            # Insert dependencies after module docstring and future imports.
            imp_time = ast.Import(names=[ast.alias(name="time", asname="_angel_time_mod")])
            imp_angel = ast.Import(names=[ast.alias(name="AngelTrace", asname="_angel_trace_mod")])
            
            perf_assign = ast.Assign(
                targets=[ast.Name(id="_angel_perf", ctx=ast.Store())],
                value=ast.Attribute(
                    value=ast.Name(id="_angel_time_mod", ctx=ast.Load()),
                    attr="perf_counter_ns",
                    ctx=ast.Load()
                )
            )
            
            send_assign = ast.Assign(
                targets=[ast.Name(id="_angel_send", ctx=ast.Store())],
                value=ast.Attribute(
                    value=ast.Name(id="_angel_trace_mod", ctx=ast.Load()),
                    attr="_angel_send_fast",
                    ctx=ast.Load()
                )
            )
            
            enter_assign = ast.Assign(
                targets=[ast.Name(id="_angel_enter", ctx=ast.Store())],
                value=ast.Attribute(
                    value=ast.Name(id="_angel_trace_mod", ctx=ast.Load()),
                    attr="_angel_enter_fast",
                    ctx=ast.Load()
                )
            )

            exit_assign = ast.Assign(
                targets=[ast.Name(id="_angel_exit", ctx=ast.Store())],
                value=ast.Attribute(
                    value=ast.Name(id="_angel_trace_mod", ctx=ast.Load()),
                    attr="_angel_exit_fast",
                    ctx=ast.Load()
                )
            )

            insert_at = 0
            if (
                tree.body
                and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)
            ):
                insert_at = 1
            while (
                insert_at < len(tree.body)
                and isinstance(tree.body[insert_at], ast.ImportFrom)
                and tree.body[insert_at].module == "__future__"
            ):
                insert_at += 1
            tree.body.insert(insert_at, imp_time)
            tree.body.insert(insert_at + 1, imp_angel)
            tree.body.insert(insert_at + 2, perf_assign)
            tree.body.insert(insert_at + 3, send_assign)
            tree.body.insert(insert_at + 4, enter_assign)
            tree.body.insert(insert_at + 5, exit_assign)

            # Instrument the file's AST
            transformer = AngelASTTransformer(self.relative_path)
            transformer.visit(tree)
            ast.fix_missing_locations(tree)
            
            return compile(tree, filepath, 'exec')
        except Exception as e:
            sys.stderr.write(f"[angel] Warning: Failed to instrument {filepath}: {e}\n")
            return super().get_code(fullname)


class AngelFinder(importlib.abc.MetaPathFinder):
    def __init__(self, project_root):
        self.project_root = os.path.abspath(project_root)
        tracer.project_root = self.project_root
        
        # Load ignore patterns from .angelignore or use standard defaults
        self.ignore_patterns = {".git", "venv", "node_modules", "__pycache__"}
        ignore_file = os.path.join(self.project_root, ".angelignore")
        if os.path.exists(ignore_file):
            try:
                with open(ignore_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            self.ignore_patterns.add(line)
            except Exception as e:
                sys.stderr.write(f"[angel] Warning: Failed to read .angelignore: {e}\n")

    def is_ignored(self, origin_path):
        try:
            rel_path = os.path.relpath(origin_path, self.project_root)
        except Exception:
            return True
            
        parts = rel_path.split(os.sep)
        for part in parts:
            if part in self.ignore_patterns:
                return True
                
        for pattern in self.ignore_patterns:
            if pattern in rel_path or pattern in origin_path:
                return True
                
        return False

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec and spec.origin and os.path.isabs(spec.origin):
            origin_path = os.path.abspath(spec.origin)
            if origin_path.startswith(self.project_root):
                if "AngelTrace" in fullname:
                    return None
                if self.is_ignored(origin_path):
                    return None
                
                rel_path = os.path.relpath(origin_path, self.project_root)
                spec.loader = AngelSourceLoader(fullname, spec.origin, rel_path)
                return spec
        return None


# CLI Run Launcher Entry
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Angel Trace Launcher")
        print("Usage: python3 AngelTrace.py <script.py> [args...]")
        sys.exit(1)

    script_path = os.path.abspath(sys.argv[1])
    project_root = os.path.dirname(script_path)

    # Register in Python import path
    sys.modules["AngelTrace"] = sys.modules["__main__"]
    sys.meta_path.insert(0, AngelFinder(project_root))

    # Shift CLI arguments so target script reads its parameters correctly
    sys.argv = sys.argv[1:]
    sys.path.insert(0, project_root)

    import runpy
    try:
        runpy.run_path(script_path, run_name="__main__")
    except SystemExit as se:
        sys.exit(se.code)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
