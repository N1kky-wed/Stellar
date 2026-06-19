"""
angel_trace.py — Angel Dynamic Runtime Tracer
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
                            • angel_trace itself (avoid self-instrumentation)
                            • venv/ directory (third-party packages)
                            • sandbox_runs/ and deployments/ directories

⚠️  DO NOT REMOVE THIS FILE.
    It must live in the Stellar project root alongside gunicorn.conf.py.
    It is loaded by gunicorn.conf.py before any application module is
    imported. Removing it will silently break all Angel CLI tracing.

⚠️  DO NOT INSTRUMENT THIS FILE.
    AngelFinder explicitly skips any module whose name contains "angel_trace"
    to prevent infinite recursion during self-import.

Usage (automatic — no action needed):
    The tracer is activated automatically via gunicorn.conf.py on every
    Gunicorn startup. No application code needs to be modified.

Usage (manual decoration — optional):
    from angel_trace import trace_fn

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

# Thread-local / task-local tracing context
active_trace_id = contextvars.ContextVar("active_trace_id", default=None)


# ---------------------------------------------------------------------------
# TCP Trace Client
# ---------------------------------------------------------------------------

class AngelTracer:
    def __init__(self, host="127.0.0.1", port=9090):
        self.host = host
        self.port = port
        self.sock = None
        # Bolt - Performance/Stability: Track last connection attempt time to implement reconnect backoff cooldown
        self.last_connect_attempt = 0.0
        self.connect_cooldown = 5.0  # seconds

    def connect(self):
        # Bolt - Performance: Avoid socket creation/connection storm if server is unreachable
        now = time.time()
        if now - self.last_connect_attempt < self.connect_cooldown:
            return
        self.last_connect_attempt = now
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(1.0)
            self.sock.connect((self.host, self.port))
        except Exception:
            # Bolt - Stability: Explicitly close the socket to prevent file descriptor leaks
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None

    def send_trace(self, node_id: str, latency_ns: int, trace_id: str = None):
        if not self.sock:
            self.connect()
        if not self.sock:
            return
        
        caller = None
        try:
            # Extract target function name from node_id to skip the instrumented function's own frame
            target_fn = node_id
            if "::" in target_fn:
                target_fn = target_fn.split("::")[-1]
            elif ":" in target_fn:
                target_fn = target_fn.split(":")[-1]

            # Walk the call stack looking for the first user-code frame.
            # Start at depth=1 (frame 0 = send_trace itself).
            # Skip Python internals AND our AngelTrace/angel_trace wrappers.
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

                if hasattr(self, 'project_root') and co_filename.startswith(self.project_root):
                    rel_path = os.path.relpath(co_filename, self.project_root)
                    caller = f"{rel_path}::{co_name}"
                else:
                    caller = co_name
                break
        except Exception:
            pass


        if not trace_id:
            trace_id = active_trace_id.get()

        import threading
        thread_name = threading.current_thread().name
        is_async = False
        try:
            import asyncio
            task = asyncio.current_task()
            if task is not None:
                is_async = True
                thread_name = f"{thread_name}:{task.get_name()}"
        except Exception:
            pass

        payload_dict = {
            "node_id": node_id,
            "latency_ns": latency_ns,
            "thread_name": thread_name,
            "is_async": is_async,
        }
        if caller:
            payload_dict["caller"] = caller
        if trace_id:
            payload_dict["trace_id"] = trace_id

        payload = json.dumps(payload_dict) + "\n"
        try:
            self.sock.sendall(payload.encode("utf-8"))
        except Exception:
            # Bolt - Stability: Explicitly close failed socket to prevent fd leak before setting to None
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None  # Force reconnection on next try (cooldown will be respected)

    def send_event(self, payload: dict):
        if not self.sock:
            self.connect()
        if not self.sock:
            return
        
        payload_str = json.dumps(payload) + "\n"
        try:
            self.sock.sendall(payload_str.encode("utf-8"))
        except Exception:
            # Bolt - Stability: Explicitly close failed socket to prevent fd leak before setting to None
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None



# Global default instance
tracer = AngelTracer()

def trace_fn(node_id: str):
    """
    Decorator to instrument a python function manually.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            trace_id = active_trace_id.get()
            if not trace_id:
                trace_id = uuid.uuid4().hex
                token = active_trace_id.set(trace_id)
                try:
                    start = time.perf_counter_ns()
                    try:
                        return func(*args, **kwargs)
                    finally:
                        duration = time.perf_counter_ns() - start
                        tracer.send_trace(node_id, duration, trace_id)
                finally:
                    active_trace_id.reset(token)
            else:
                start = time.perf_counter_ns()
                try:
                    return func(*args, **kwargs)
                finally:
                    duration = time.perf_counter_ns() - start
                    tracer.send_trace(node_id, duration, trace_id)
        return wrapper
    return decorator


# --- DISTRIBUTED TRACING MONKEYPATCHES ---
# Strategy: use a background daemon thread that polls sys.modules and applies
# patches once the target libraries are fully initialized. This avoids all
# circular-import and recursion issues caused by hooking builtins.__import__.

_patched_flask = False
_patched_requests = False

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

def _patch_worker():
    """Background thread: retries patching until both targets are patched."""
    import threading
    import time as _time
    for _ in range(120):   # up to ~60 s
        if _patched_flask and _patched_requests:
            break
        if not _patched_flask:
            patch_flask()
        if not _patched_requests:
            patch_requests()
        _time.sleep(0.5)

def start_patcher_thread():
    global _patched_flask, _patched_requests
    _patched_flask = False
    _patched_requests = False

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

class AngelASTTransformer(ast.NodeTransformer):
    def __init__(self, relative_path):
        self.relative_path = relative_path
        self.func_stack = []

    def visit_FunctionDef(self, node):
        self.func_stack.append(node.name)
        res = self.instrument_function(node)
        self.func_stack.pop()
        return res

    def visit_AsyncFunctionDef(self, node):
        self.func_stack.append(node.name)
        res = self.instrument_function(node)
        self.func_stack.pop()
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

    def instrument_function(self, node):
        self.generic_visit(node)
        
        # Skip special/internal dunder functions
        if node.name.startswith("__") and node.name.endswith("__"):
            return node
            
        qname = "::".join(self.func_stack)
        node_id = f"{self.relative_path}::{qname}"
        
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
                                value=ast.Name(id="angel_trace", ctx=ast.Load()),
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

        # 1. _angel_t0 = time.perf_counter_ns()
        timer_start = ast.Assign(
            targets=[ast.Name(id="_angel_t0", ctx=ast.Store())],
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="time", ctx=ast.Load()),
                    attr="perf_counter_ns",
                    ctx=ast.Load()
                ),
                args=[],
                keywords=[]
            )
        )
        
        # 2. timer_diff = time.perf_counter_ns() - _angel_t0
        timer_diff = ast.BinOp(
            left=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="time", ctx=ast.Load()),
                    attr="perf_counter_ns",
                    ctx=ast.Load()
                ),
                args=[],
                keywords=[]
            ),
            op=ast.Sub(),
            right=ast.Name(id="_angel_t0", ctx=ast.Load())
        )
        
        # 3. angel_trace.tracer.send_trace(node_id, timer_diff)
        send_call = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Attribute(
                        value=ast.Name(id="angel_trace", ctx=ast.Load()),
                        attr="tracer",
                        ctx=ast.Load()
                    ),
                    attr="send_trace",
                    ctx=ast.Load()
                ),
                args=[
                    ast.Constant(value=node_id),
                    timer_diff
                ],
                keywords=[]
            )
        )
        
        # Wrap body in Try-Finally
        try_node = ast.Try(
            body=node.body,
            handlers=[],
            orelse=[],
            finalbody=[send_call]
        )
        
        node.body = [timer_start, try_node]
        
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
            
            # Prepend logging dependencies at the top of the file namespace
            imp_time = ast.Import(names=[ast.alias(name="time", asname=None)])
            imp_angel = ast.Import(names=[ast.alias(name="angel_trace", asname=None)])
            tree.body.insert(0, imp_time)
            tree.body.insert(1, imp_angel)

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

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec and spec.origin and os.path.isabs(spec.origin):
            origin_path = os.path.abspath(spec.origin)
            if origin_path.startswith(self.project_root):
                if "angel_trace" in fullname:
                    return None
                if "venv" in origin_path or "sandbox_runs" in origin_path or "deployments" in origin_path:
                    return None
                
                rel_path = os.path.relpath(origin_path, self.project_root)
                spec.loader = AngelSourceLoader(fullname, spec.origin, rel_path)
                return spec
        return None


# CLI Run Launcher Entry
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Angel Trace Launcher")
        print("Usage: python3 -m angel_trace <script.py> [args...]")
        sys.exit(1)

    script_path = os.path.abspath(sys.argv[1])
    project_root = os.path.dirname(script_path)

    # Register in Python import path
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
