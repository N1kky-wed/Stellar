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


# ---------------------------------------------------------------------------
# TCP Trace Client
# ---------------------------------------------------------------------------

class AngelTracer:
    def __init__(self, host="127.0.0.1", port=9090):
        self.host = host
        self.port = port
        self.sock = None

    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(1.0)
            self.sock.connect((self.host, self.port))
        except Exception:
            self.sock = None

    def send_trace(self, node_id: str, latency_ns: int):
        if not self.sock:
            self.connect()
        if not self.sock:
            return
        
        caller = None
        try:
            # frame 0: send_trace
            # frame 1: the finally block (or wrapper)
            # frame 2: the calling function
            caller = sys._getframe(2).f_code.co_name
        except Exception:
            pass

        payload_dict = {"node_id": node_id, "latency_ns": latency_ns}
        if caller and caller != "<module>":
            payload_dict["caller"] = caller

        payload = json.dumps(payload_dict) + "\n"
        try:
            self.sock.sendall(payload.encode("utf-8"))
        except Exception:
            self.sock = None  # Force reconnection on next try

    def send_event(self, payload: dict):
        if not self.sock:
            self.connect()
        if not self.sock:
            return
        
        payload_str = json.dumps(payload) + "\n"
        try:
            self.sock.sendall(payload_str.encode("utf-8"))
        except Exception:
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
            start = time.perf_counter_ns()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.perf_counter_ns() - start
                tracer.send_trace(node_id, duration)
        return wrapper
    return decorator


# --- ZERO-CODE-CHANGE RUNTIME INSTRUMENTATION HOOK ---

class AngelASTTransformer(ast.NodeTransformer):
    def __init__(self, relative_path):
        self.relative_path = relative_path

    def visit_FunctionDef(self, node):
        return self.instrument_function(node)

    def visit_AsyncFunctionDef(self, node):
        return self.instrument_function(node)

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
            
        node_id = f"{self.relative_path}::{node.name}"
        
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
