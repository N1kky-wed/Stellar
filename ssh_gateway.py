#!/usr/bin/env python3
"""
Stellar SSH Gateway
===================
Secure SSH-based TUI for managing Docker container deployments.

Flow:
  1. User connects: ssh stellarai.live
  2. Sees auth screen → visits https://stellarai.live/auth/ssh
  3. Generates one-time code, pastes into SSH terminal
  4. Authenticated → TUI dashboard with repo list
  5. Select a repo → drops into interactive container shell

Security:
  - No host system shell access (custom SSH server, not OpenSSH)
  - Device-code auth via Redis (one-time, 5-min TTL)
  - Rate limiting per IP (connections + failed auths)
  - Session idle timeout (30 min)
  - All SSH forwarding disabled (port/agent/X11/TCP)
  - Audit logging of all connections and actions
  - Container ownership validation against database
"""

import paramiko
import threading
import socket
import os
import sys
import logging
import time
import json
import struct
import select
import signal
import sqlite3
import secrets
from io import StringIO
from datetime import datetime

import redis
import docker
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box

# ============================================================
# Configuration
# ============================================================
SSH_HOST = '0.0.0.0'
SSH_PORT = 2222
HOST_KEY_PATH = '/home/stellaradmin/my_app/ssh_gateway_host_key'
DATABASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stellar_local.db')
LOG_DIR = '/home/stellaradmin/my_app/logs'

# Security tuning
MAX_AUTH_ATTEMPTS = 3           # Max wrong codes per session
AUTH_CODE_TTL = 300             # 5 minutes
SESSION_IDLE_TIMEOUT = 1800    # 30 minutes
RATE_LIMIT_CONNECTIONS = 10    # Max connections per IP per window
RATE_LIMIT_WINDOW = 900        # 15 minutes
MAX_CONCURRENT_SESSIONS = 50   # Total concurrent SSH sessions

# Internal shared secret for verify-code API
GATEWAY_SECRET = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')

# ============================================================
# Logging
# ============================================================
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger('stellar_ssh')
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(os.path.join(LOG_DIR, 'ssh_gateway.log'))
_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
logger.addHandler(_handler)

audit = logging.getLogger('stellar_ssh_audit')
audit.setLevel(logging.INFO)
_audit_handler = logging.FileHandler(os.path.join(LOG_DIR, 'ssh_audit.log'))
_audit_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
audit.addHandler(_audit_handler)

# ============================================================
# Globals
# ============================================================
active_sessions = 0
sessions_lock = threading.Lock()

# ============================================================
# ANSI Helpers
# ============================================================
CLEAR_SCREEN = '\x1b[2J\x1b[H'
HIDE_CURSOR = '\x1b[?25l'
SHOW_CURSOR = '\x1b[?25h'
RESET_STYLE = '\x1b[0m'


def send_raw(channel, text):
    """Send raw text to SSH channel."""
    try:
        if isinstance(text, str):
            text = text.replace('\r\n', '\n').replace('\n', '\r\n')
            data = text.encode('utf-8')
        else:
            data = text
        channel.sendall(data)
    except Exception:
        pass


# ============================================================
# Rate Limiter (Redis-backed)
# ============================================================
class RateLimiter:
    def __init__(self):
        self.r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

    def check_connection_rate(self, ip: str) -> bool:
        """Returns True if connection is allowed."""
        key = f"ssh_conn_rate:{ip}"
        try:
            count = self.r.incr(key)
            if count == 1:
                self.r.expire(key, RATE_LIMIT_WINDOW)
            return count <= RATE_LIMIT_CONNECTIONS
        except Exception as e:
            logger.error(f"Rate limiter Redis error: {e}")
            return True  # Fail open to avoid lockout on Redis issues

    def record_auth_failure(self, ip: str) -> int:
        """Record a failed auth attempt. Returns total failures."""
        key = f"ssh_verify_fail:{ip}"
        try:
            count = self.r.incr(key)
            if count == 1:
                self.r.expire(key, RATE_LIMIT_WINDOW)
            return count
        except Exception:
            return 0

    def is_ip_blocked(self, ip: str) -> bool:
        """Check if IP has too many failed auths."""
        try:
            count = self.r.get(f"ssh_verify_fail:{ip}")
            return int(count or 0) >= 10
        except Exception:
            return False


rate_limiter = RateLimiter()


# ============================================================
# Database Helper
# ============================================================
def get_user_repos(user_id: int) -> list:
    """Fetch all repos for a user from the database."""
    repos = []
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            'SELECT id, project_name, process_id, container_id, status, '
            'subdomain, created_at, app_type FROM repo_history '
            'WHERE user_id = ? ORDER BY last_updated DESC',
            (user_id,)
        )
        for row in cursor:
            repos.append({
                'id': row['id'],
                'name': row['project_name'] or 'Untitled',
                'process_id': row['process_id'],
                'container_id': row['container_id'],
                'status': row['status'] or 'unknown',
                'subdomain': row['subdomain'] or '-',
                'created': row['created_at'][:10] if row['created_at'] else '-',
                'app_type': row['app_type'] or 'forge',
            })
        conn.close()
    except Exception as e:
        logger.error(f"DB error fetching repos for user {user_id}: {e}")
    return repos


def get_container(client, process_id: str, app_type: str):
    """Retrieve Docker container checking both the specific and fallback names."""
    try:
        return client.containers.get(f"stellar-{app_type}-{process_id}")
    except docker.errors.NotFound:
        return client.containers.get(f"stellar-repo-{process_id}")


def get_container_status(process_id: str, app_type: str = 'repo') -> str:
    """Get live Docker container status."""
    try:
        client = docker.from_env()
        container = get_container(client, process_id, app_type)
        return container.status
    except docker.errors.NotFound:
        return 'not_found'
    except Exception:
        return 'unknown'


# ============================================================
# Auth Code Verification (calls local API)
# ============================================================
def verify_auth_code(code: str) -> dict | None:
    """
    Verify an auth code against Redis directly.
    Returns user dict or None.
    """
    clean_code = code.strip().replace('-', '').replace(' ', '').upper()
    if len(clean_code) != 6:
        return None

    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        key = f"ssh_auth_code:{clean_code}"
        data = r.get(key)
        if data:
            r.delete(key)  # One-time use — immediately invalidate
            user_data = json.loads(data)
            # Decrement user's active code count to prevent lockout
            user_id = user_data.get('user_id')
            if user_id:
                user_code_key = f"ssh_auth_code:user:{user_id}"
                r.decr(user_code_key)
                remaining = r.get(user_code_key)
                if remaining and int(remaining) <= 0:
                    r.delete(user_code_key)
            audit.info(f"AUTH_SUCCESS | code={clean_code[:2]}**** | user_id={user_data.get('user_id')} | username={user_data.get('username')}")
            return user_data
        return None
    except Exception as e:
        logger.error(f"Auth code verification error: {e}")
        return None


# ============================================================
# Paramiko SSH Server Interface
# ============================================================
class StellarSSHServer(paramiko.ServerInterface):
    """
    Custom SSH server that:
    - Accepts 'none' auth (real auth via device code in TUI)
    - Grants PTY and shell
    - Blocks ALL forwarding (port, agent, X11, TCP, env)
    """

    def __init__(self, client_addr: str):
        self.client_addr = client_addr
        self.event = threading.Event()
        self.term_width = 80
        self.term_height = 24
        self.resize_event = threading.Event()

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        logger.warning(f"Rejected channel request kind='{kind}' from {self.client_addr}")
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_none(self, username):
        self.username = username
        # Accept none-auth; real auth happens in TUI
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_password(self, username, password):
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'none'

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.term_width = width
        self.term_height = height
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        self.term_width = width
        self.term_height = height
        self.resize_event.set()
        return True

    # --- SECURITY: Block all forwarding ---
    def check_port_forward_request(self, address, port):
        logger.warning(f"BLOCKED port forward request from {self.client_addr}: {address}:{port}")
        return False

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        logger.warning(f"BLOCKED direct-tcpip from {self.client_addr}: {origin} -> {destination}")
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_env_request(self, channel, name, value):
        return False

    def check_channel_x11_request(self, channel, single_connection, auth_protocol, auth_cookie, screen_number):
        logger.warning(f"BLOCKED X11 forwarding from {self.client_addr}")
        return False

    def check_channel_forward_agent_request(self, channel):
        logger.warning(f"BLOCKED agent forwarding from {self.client_addr}")
        return False


# ============================================================
# TUI Renderer (uses Rich → StringIO → channel)
# ============================================================
class TUI:
    """Renders beautiful terminal UI screens via Rich."""

    LOGO = r"""
   _____ _______ ______ _      _               _____  
  / ____|__   __|  ____| |    | |      /\     |  __ \ 
 | (___    | |  | |__  | |    | |     /  \    | |__) |
  \___ \   | |  |  __| | |    | |    / /\ \   |  _  / 
  ____) |  | |  | |____| |____| |___/ ____ \  | | \ \ 
 |_____/   |_|  |______|______|______/_/    \_\_|  \_\
"""

    @staticmethod
    def _render(width: int, callback) -> str:
        """Render rich output to string."""
        buf = StringIO()
        console = Console(
            file=buf,
            width=max(40, width),
            force_terminal=True,
            color_system="256",
            highlight=False,
        )
        callback(console)
        return buf.getvalue()

    @staticmethod
    def auth_screen(width: int, height: int, error_msg: str = "") -> str:
        def draw(c):
            c.print()
            c.print(Align.center(Text(TUI.LOGO, style="bold cyan")))
            c.print(Align.center(Text("SSH Terminal Gateway", style="bold white on dark_blue")))
            c.print()
            auth_text = "[bold yellow]Visit[/bold yellow] [bold underline cyan]https://stellarai.live/auth/ssh[/bold underline cyan] [bold yellow]· Generate Code · Paste it below[/bold yellow]"
            c.print(Align.center(Panel(
                auth_text,
                title="[bold white]Authentication Required[/bold white]",
                border_style="bright_blue",
                width=min(58, width - 4),
                padding=(1, 2),
            )))
            if error_msg:
                c.print()
                c.print(Align.center(Text(f"✗ {error_msg}", style="bold red")))
            c.print()

        return TUI._render(width, draw)

    @staticmethod
    def dashboard(repos: list, selected: int, username: str, width: int, height: int, status_msg: str = "") -> str:
        def draw(c):
            # Header
            c.print(Panel(
                Align.center(Text(f"  Stellar — {username}'s Deployments", style="bold white")),
                style="bright_blue",
                box=box.DOUBLE,
            ))

            if not repos:
                c.print()
                c.print(Align.center(Panel(
                    "[dim italic]No deployments found. Deploy something with Stellar first![/dim italic]",
                    border_style="dim",
                    width=min(60, width - 4),
                )))
            else:
                table = Table(
                    box=box.ROUNDED,
                    show_header=True,
                    header_style="bold cyan",
                    border_style="bright_blue",
                    width=min(78, width - 4),
                    pad_edge=True,
                )
                table.add_column("", width=2, no_wrap=True)
                table.add_column("Project", style="white", min_width=12, ratio=3)
                table.add_column("Status", justify="center", width=12, no_wrap=True)
                table.add_column("Subdomain", style="dim cyan", ratio=2)
                table.add_column("Type", justify="center", width=8)
                table.add_column("Created", style="dim", width=10)

                for i, repo in enumerate(repos):
                    is_sel = (i == selected)
                    marker = "▸" if is_sel else " "
                    row_style = "reverse bold" if is_sel else ""

                    live_status = get_container_status(repo['process_id'], repo.get('app_type', 'repo'))
                    if live_status == 'running':
                        status_text = Text("● Running", style="bold green")
                    elif live_status == 'exited':
                        status_text = Text("○ Stopped", style="bold red")
                    elif live_status == 'not_found':
                        status_text = Text("✗ Removed", style="dim red")
                    else:
                        status_text = Text(f"? {live_status}", style="yellow")

                    table.add_row(
                        Text(marker, style="bold cyan" if is_sel else ""),
                        Text(repo['name'], style=row_style),
                        status_text,
                        Text(repo['subdomain'], style=row_style if is_sel else "dim cyan"),
                        Text(repo['app_type'], style=row_style if is_sel else ""),
                        Text(repo['created'], style=row_style if is_sel else "dim"),
                    )

                c.print(Align.center(table))

            c.print()

            # Status message
            if status_msg:
                c.print(Align.center(Text(status_msg, style="bold yellow")))
                c.print()

            # Controls
            controls = Text()
            controls.append(" ↑↓ ", style="bold black on white")
            controls.append(" Navigate  ", style="dim")
            controls.append(" Enter ", style="bold black on cyan")
            controls.append(" Connect  ", style="dim")
            controls.append(" L ", style="bold black on yellow")
            controls.append(" Logs  ", style="dim")
            controls.append(" R ", style="bold black on green")
            controls.append(" Restart  ", style="dim")
            controls.append(" S ", style="bold black on red")
            controls.append(" Stop  ", style="dim")
            controls.append(" Q ", style="bold black on white")
            controls.append(" Quit", style="dim")
            c.print(Align.center(controls))

        return TUI._render(width, draw)

    @staticmethod
    def connecting_screen(repo_name: str, width: int) -> str:
        def draw(c):
            c.print()
            c.print(Align.center(Panel(
                f"[bold cyan]Connecting to [bold white]{repo_name}[/bold white]...[/bold cyan]\n\n"
                "[dim]Press [bold]Ctrl+D[/bold] or type [bold]exit[/bold] to return to dashboard[/dim]",
                border_style="bright_blue",
                width=min(55, width - 4),
                padding=(1, 2),
            )))

        return TUI._render(width, draw)

    @staticmethod
    def log_viewer_header(repo_name: str, width: int) -> str:
        def draw(c):
            c.print(Panel(
                Align.center(Text(f"  Logs: {repo_name}", style="bold white")),
                style="yellow",
                box=box.HEAVY,
            ))
            c.print(Align.center(Text(" Press Q or Ctrl+C to return to dashboard ", style="dim")))
            c.print()

        return TUI._render(width, draw)

    @staticmethod
    def goodbye_screen(username: str, width: int) -> str:
        def draw(c):
            c.print()
            c.print(Align.center(Panel(
                f"[bold white]Goodbye, {username}![/bold white]\n\n"
                "[dim]Session ended. Reconnect anytime with:[/dim]\n"
                "[bold cyan]ssh -p 2222 stellarai.live[/bold cyan]",
                border_style="bright_blue",
                width=min(50, width - 4),
                padding=(1, 2),
            )))
            c.print()

        return TUI._render(width, draw)


# ============================================================
# Input Reader
# ============================================================
def read_key(channel, timeout: float = 0.5) -> str | None:
    """Read a single keypress or escape sequence from SSH channel."""
    try:
        ready = select.select([channel], [], [], timeout)
        if not ready[0]:
            return None

        data = channel.recv(32)
        if not data:
            return 'EOF'

        # Escape sequences
        if data == b'\x1b[A' or data == b'\x1bOA':
            return 'UP'
        if data == b'\x1b[B' or data == b'\x1bOB':
            return 'DOWN'
        if data == b'\x1b[C' or data == b'\x1bOC':
            return 'RIGHT'
        if data == b'\x1b[D' or data == b'\x1bOD':
            return 'LEFT'
        if data == b'\r' or data == b'\n':
            return 'ENTER'
        if data == b'\x7f' or data == b'\x08':
            return 'BACKSPACE'
        if data == b'\x03':
            return 'CTRL_C'
        if data == b'\x04':
            return 'CTRL_D'
        if data == b'\x1b':
            return 'ESC'

        # Regular characters
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return None

    except Exception:
        return 'EOF'


def read_line(channel, prompt: str, mask: bool = False, max_len: int = 20) -> str | None:
    """Read a line of input with echo (or masked echo)."""
    send_raw(channel, prompt)
    buffer = []

    while True:
        key = read_key(channel, timeout=30.0)
        if key is None:
            continue
        if key == 'EOF' or key == 'CTRL_C':
            return None
        if key == 'ENTER':
            send_raw(channel, '\r\n')
            return ''.join(buffer)
        if key == 'BACKSPACE':
            if buffer:
                buffer.pop()
                send_raw(channel, '\x08 \x08')
            continue

        # Handle characters (including pasted strings or rapid typing)
        if isinstance(key, str) and key not in ('UP', 'DOWN', 'LEFT', 'RIGHT', 'ESC'):
            for char in key:
                if char in ('\r', '\n'):
                    send_raw(channel, '\r\n')
                    return ''.join(buffer)
                if char in ('\x7f', '\x08'):
                    if buffer:
                        buffer.pop()
                        send_raw(channel, '\x08 \x08')
                    continue
                if char.isprintable():
                    if len(buffer) < max_len:
                        buffer.append(char)
                        send_raw(channel, '*' if mask else char)


# ============================================================
# Container Shell (Docker exec PTY ↔ SSH channel)
# ============================================================
def attach_container_shell(channel, server: StellarSSHServer, process_id: str, app_type: str, user_id: int):
    """
    Attach an interactive shell to a Docker container.
    Pipes SSH channel ↔ Docker exec PTY bidirectionally.
    """
    try:
        client = docker.from_env()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"SHELL_ATTACH | user_id={user_id} | container={container_name}")

        if container.status != 'running':
            send_raw(channel, "\r\n\x1b[31m  Container is not running. Start it first.\x1b[0m\r\n")
            time.sleep(1.5)
            return

        # Create exec instance with PTY
        exec_instance = client.api.exec_create(
            container.id,
            '/bin/bash',
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            environment={
                'TERM': 'xterm-256color',
                'COLUMNS': str(server.term_width),
                'LINES': str(server.term_height),
            },
        )

        # Start exec and get raw socket
        sock_response = client.api.exec_start(
            exec_instance['Id'],
            tty=True,
            socket=True,
            demux=False,
        )

        # Get the underlying socket
        if hasattr(sock_response, '_sock'):
            raw_sock = sock_response._sock
        elif hasattr(sock_response, 'raw') and hasattr(sock_response.raw, '_sock'):
            raw_sock = sock_response.raw._sock
        else:
            raw_sock = sock_response

        raw_sock.setblocking(False)

        # Resize to match SSH terminal
        try:
            client.api.exec_resize(exec_instance['Id'], height=server.term_height, width=server.term_width)
        except Exception:
            pass

        # Bidirectional pipe
        last_activity = time.time()

        while True:
            # Check for resize
            if server.resize_event.is_set():
                server.resize_event.clear()
                try:
                    client.api.exec_resize(exec_instance['Id'], height=server.term_height, width=server.term_width)
                except Exception:
                    pass

            # Check idle timeout
            if time.time() - last_activity > SESSION_IDLE_TIMEOUT:
                send_raw(channel, "\r\n\x1b[33m  Session timed out due to inactivity.\x1b[0m\r\n")
                audit.info(f"SHELL_TIMEOUT | user_id={user_id} | container={container_name}")
                break

            try:
                r_list, _, _ = select.select([channel, raw_sock], [], [], 1.0)
            except Exception:
                break

            if channel in r_list:
                try:
                    data = channel.recv(4096)
                    if not data:
                        break
                    last_activity = time.time()
                    raw_sock.sendall(data)
                except Exception:
                    break

            if raw_sock in r_list:
                try:
                    data = raw_sock.recv(4096)
                    if not data:
                        break
                    last_activity = time.time()
                    channel.sendall(data)
                except Exception:
                    break

        audit.info(f"SHELL_DETACH | user_id={user_id} | container={container_name}")

    except docker.errors.NotFound:
        send_raw(channel, "\r\n\x1b[31m  Container not found. It may have been removed.\x1b[0m\r\n")
        time.sleep(1.5)
    except Exception as e:
        logger.error(f"Shell attach error for {container_name}: {e}", exc_info=True)
        send_raw(channel, f"\r\n\x1b[31m  Error: {str(e)[:80]}\x1b[0m\r\n")
        time.sleep(1.5)


# ============================================================
# Container Logs Viewer
# ============================================================
def view_container_logs(channel, server: StellarSSHServer, process_id: str, app_type: str, user_id: int):
    """Stream Docker container logs to the SSH channel."""
    try:
        client = docker.from_env()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"LOGS_VIEW | user_id={user_id} | container={container_name}")

        send_raw(channel, CLEAR_SCREEN)
        send_raw(channel, TUI.log_viewer_header(container_name, server.term_width))

        # Stream logs
        log_stream = container.logs(stream=True, follow=True, tail=50, timestamps=True)

        for chunk in log_stream:
            # Check if user wants to quit
            ready = select.select([channel], [], [], 0.05)
            if ready[0]:
                key_data = channel.recv(32)
                if key_data in (b'q', b'Q', b'\x03'):
                    break

            try:
                line = chunk.decode('utf-8', errors='replace').rstrip('\n')
                send_raw(channel, f"  {line}\r\n")
            except Exception:
                pass

    except docker.errors.NotFound:
        send_raw(channel, "\r\n\x1b[31m  Container not found.\x1b[0m\r\n")
        time.sleep(1.5)
    except Exception as e:
        logger.error(f"Log viewer error: {e}")
        send_raw(channel, f"\r\n\x1b[31m  Error: {str(e)[:80]}\x1b[0m\r\n")
        time.sleep(1.5)


# ============================================================
# Container Management Actions
# ============================================================
def restart_container(process_id: str, app_type: str, user_id: int) -> str:
    """Restart a Docker container."""
    try:
        client = docker.from_env()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"CONTAINER_RESTART | user_id={user_id} | container={container_name}")
        container.restart(timeout=10)
        return f"✓ {container_name} restarted successfully"
    except docker.errors.NotFound:
        return "✗ Container not found"
    except Exception as e:
        return f"✗ Restart failed: {str(e)[:60]}"


def stop_container(process_id: str, app_type: str, user_id: int) -> str:
    """Stop a Docker container."""
    try:
        client = docker.from_env()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"CONTAINER_STOP | user_id={user_id} | container={container_name}")
        container.stop(timeout=10)
        return f"✓ {container_name} stopped"
    except docker.errors.NotFound:
        return "✗ Container not found"
    except Exception as e:
        return f"✗ Stop failed: {str(e)[:60]}"


def start_container(process_id: str, app_type: str, user_id: int) -> str:
    """Start a stopped Docker container."""
    try:
        client = docker.from_env()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"CONTAINER_START | user_id={user_id} | container={container_name}")
        container.start()
        return f"✓ {container_name} started"
    except docker.errors.NotFound:
        return "✗ Container not found"
    except Exception as e:
        return f"✗ Start failed: {str(e)[:60]}"


# ============================================================
# Session Handler (main per-connection logic)
# ============================================================
def handle_session(channel, server: StellarSSHServer, client_addr: str):
    """
    Main session handler for one SSH connection.
    Runs: Auth → Dashboard → Container Shell/Logs → Goodbye
    """
    global active_sessions
    user_info = None

    try:
        # Wait for shell request
        if not server.event.wait(timeout=10):
            logger.warning(f"Client {client_addr} didn't request shell in time")
            return

        channel.settimeout(SESSION_IDLE_TIMEOUT)
        real_ip = server.username if (server.username and client_addr == '127.0.0.1') else client_addr
        
        # Now check if this real_ip is blocked!
        if real_ip != '127.0.0.1' and client_addr == '127.0.0.1':
            if rate_limiter.is_ip_blocked(real_ip):
                send_raw(channel, "\r\n\x1b[31m  ✗ Your IP is blocked due to too many failed attempts.\x1b[0m\r\n")
                time.sleep(2)
                return

        # ---- PHASE 1: Authentication ----
        auth_attempts = 0
        while auth_attempts < MAX_AUTH_ATTEMPTS:
            error_msg = ""
            if auth_attempts > 0:
                remaining = MAX_AUTH_ATTEMPTS - auth_attempts
                error_msg = f"Invalid code. {remaining} attempt{'s' if remaining > 1 else ''} remaining."

            # Render auth screen
            send_raw(channel, CLEAR_SCREEN + SHOW_CURSOR)
            send_raw(channel, TUI.auth_screen(server.term_width, server.term_height, error_msg))

            # Read code
            code = read_line(channel, "  Enter code: ")
            if code is None:
                audit.info(f"AUTH_DISCONNECT | ip={real_ip} | phase=code_entry")
                return

            # Verify
            user_info = verify_auth_code(code)
            if user_info:
                break
            else:
                auth_attempts += 1
                rate_limiter.record_auth_failure(real_ip)
                audit.info(f"AUTH_FAIL | ip={real_ip} | attempt={auth_attempts} | code_prefix={code[:2] if code else '??'}****")

        if not user_info:
            send_raw(channel, "\r\n\x1b[31m  ✗ Too many failed attempts. Disconnecting.\x1b[0m\r\n")
            audit.info(f"AUTH_LOCKOUT | ip={real_ip} | attempts={MAX_AUTH_ATTEMPTS}")
            time.sleep(2)
            return

        user_id = user_info['user_id']
        username = user_info.get('display_name') or user_info.get('username', 'User')
        audit.info(f"SESSION_START | ip={real_ip} | user_id={user_id} | username={username}")

        # ---- PHASE 2: Dashboard ----
        selected_index = 0
        status_msg = f"Welcome, {username}!"
        last_activity = time.time()

        while True:
            # Check idle timeout
            if time.time() - last_activity > SESSION_IDLE_TIMEOUT:
                send_raw(channel, CLEAR_SCREEN)
                send_raw(channel, "\r\n\x1b[33m  Session timed out due to inactivity.\x1b[0m\r\n")
                audit.info(f"SESSION_TIMEOUT | user_id={user_id} | idle_minutes={SESSION_IDLE_TIMEOUT // 60}")
                time.sleep(2)
                break

            # Fetch repos and render
            repos = get_user_repos(user_id)
            if selected_index >= len(repos):
                selected_index = max(0, len(repos) - 1)

            screen = TUI.dashboard(repos, selected_index, username, server.term_width, server.term_height, status_msg)
            send_raw(channel, CLEAR_SCREEN + HIDE_CURSOR)
            send_raw(channel, screen)
            status_msg = ""  # Clear one-shot status

            # Read keypress
            key = read_key(channel, timeout=5.0)
            if key is None:
                continue

            last_activity = time.time()

            if key == 'UP':
                if repos and selected_index > 0:
                    selected_index -= 1
            elif key == 'DOWN':
                if repos and selected_index < len(repos) - 1:
                    selected_index += 1
            elif key == 'ENTER':
                if repos:
                    repo = repos[selected_index]
                    app_type = repo.get('app_type', 'repo')
                    live_status = get_container_status(repo['process_id'], app_type)
                    if live_status == 'running':
                        send_raw(channel, CLEAR_SCREEN + SHOW_CURSOR)
                        send_raw(channel, TUI.connecting_screen(repo['name'], server.term_width))
                        time.sleep(0.5)
                        send_raw(channel, CLEAR_SCREEN)
                        attach_container_shell(channel, server, repo['process_id'], app_type, user_id)
                        status_msg = f"Disconnected from {repo['name']}"
                    elif live_status in ('exited', 'created'):
                        status_msg = start_container(repo['process_id'], app_type, user_id)
                    else:
                        status_msg = f"Cannot connect: container is {live_status}"
            elif key in ('q', 'Q', 'CTRL_C', 'CTRL_D', 'EOF'):
                break
            elif key in ('l', 'L'):
                if repos:
                    repo = repos[selected_index]
                    app_type = repo.get('app_type', 'repo')
                    send_raw(channel, SHOW_CURSOR)
                    view_container_logs(channel, server, repo['process_id'], app_type, user_id)
                    status_msg = f"Exited log viewer for {repo['name']}"
            elif key in ('r', 'R'):
                if repos:
                    repo = repos[selected_index]
                    app_type = repo.get('app_type', 'repo')
                    status_msg = f"Restarting {repo['name']}..."
                    send_raw(channel, CLEAR_SCREEN + HIDE_CURSOR)
                    send_raw(channel, TUI.dashboard(repos, selected_index, username, server.term_width, server.term_height, status_msg))
                    status_msg = restart_container(repo['process_id'], app_type, user_id)
            elif key in ('s', 'S'):
                if repos:
                    repo = repos[selected_index]
                    app_type = repo.get('app_type', 'repo')
                    status_msg = stop_container(repo['process_id'], app_type, user_id)

        # ---- PHASE 3: Goodbye ----
        send_raw(channel, CLEAR_SCREEN + SHOW_CURSOR)
        send_raw(channel, TUI.goodbye_screen(username, server.term_width))
        audit.info(f"SESSION_END | user_id={user_id} | username={username}")
        time.sleep(1)

    except socket.timeout:
        audit.info(f"SESSION_SOCKET_TIMEOUT | ip={client_addr}")
    except Exception as e:
        logger.error(f"Session error for {client_addr}: {e}", exc_info=True)
    finally:
        send_raw(channel, SHOW_CURSOR + RESET_STYLE)
        try:
            channel.close()
        except Exception:
            pass
        with sessions_lock:
            active_sessions -= 1
        logger.info(f"Session closed for {client_addr}")


# ============================================================
# Connection Handler
# ============================================================
def handle_connection(client_socket, client_addr):
    """Handle a new TCP connection, set up SSH transport."""
    global active_sessions
    ip = client_addr[0]

    # Rate limit check
    if ip != '127.0.0.1':
        if rate_limiter.is_ip_blocked(ip):
            audit.info(f"CONNECTION_BLOCKED | ip={ip} | reason=auth_failures")
            client_socket.close()
            return

        if not rate_limiter.check_connection_rate(ip):
            audit.info(f"CONNECTION_BLOCKED | ip={ip} | reason=rate_limit")
            client_socket.close()
            return

    with sessions_lock:
        if active_sessions >= MAX_CONCURRENT_SESSIONS:
            logger.warning(f"Max concurrent sessions reached ({MAX_CONCURRENT_SESSIONS}), rejecting {ip}")
            client_socket.close()
            return
        active_sessions += 1

    audit.info(f"CONNECTION_NEW | ip={ip} | active_sessions={active_sessions}")

    try:
        transport = paramiko.Transport(client_socket)

        # Security: Set strict banner, timeouts
        transport.banner_timeout = 15
        transport.auth_timeout = 30

        # Load host key
        host_key = paramiko.Ed25519Key(filename=HOST_KEY_PATH)
        transport.add_server_key(host_key)

        server = StellarSSHServer(ip)

        try:
            transport.start_server(server=server)
        except paramiko.SSHException as e:
            logger.error(f"SSH negotiation failed for {ip}: {e}")
            return

        # Wait for a channel
        channel = transport.accept(timeout=20)
        if channel is None:
            logger.warning(f"No channel opened by {ip}")
            return

        # Handle the session
        handle_session(channel, server, ip)

    except Exception as e:
        logger.error(f"Connection handler error for {ip}: {e}", exc_info=True)
    finally:
        try:
            transport.close()
        except Exception:
            pass


# ============================================================
# Host Key Generation
# ============================================================
def ensure_host_key():
    """Generate Ed25519 host key if it doesn't exist."""
    if os.path.exists(HOST_KEY_PATH):
        key = paramiko.Ed25519Key(filename=HOST_KEY_PATH)
        logger.info(f"Using existing host key: {HOST_KEY_PATH} (fingerprint: {key.get_fingerprint().hex()})")
        return

    logger.info(f"Generating new Ed25519 host key at {HOST_KEY_PATH}")
    import subprocess
    result = subprocess.run(
        ['ssh-keygen', '-t', 'ed25519', '-f', HOST_KEY_PATH, '-N', '', '-q'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.critical(f"Failed to generate host key: {result.stderr}")
        sys.exit(1)
    os.chmod(HOST_KEY_PATH, 0o600)
    key = paramiko.Ed25519Key(filename=HOST_KEY_PATH)
    logger.info(f"Host key generated. Fingerprint: {key.get_fingerprint().hex()}")


# ============================================================
# Main Server
# ============================================================
def main():
    """Start the SSH gateway server."""
    print(f"Stellar SSH Gateway starting on {SSH_HOST}:{SSH_PORT}")
    logger.info(f"=== Stellar SSH Gateway starting on {SSH_HOST}:{SSH_PORT} ===")

    # Generate host key if needed
    ensure_host_key()

    # Create server socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind((SSH_HOST, SSH_PORT))
    except OSError as e:
        logger.critical(f"Cannot bind to {SSH_HOST}:{SSH_PORT}: {e}")
        print(f"FATAL: Cannot bind to {SSH_HOST}:{SSH_PORT}: {e}")
        sys.exit(1)

    server_socket.listen(10)
    server_socket.settimeout(1.0)  # For graceful shutdown

    logger.info(f"Listening for SSH connections on port {SSH_PORT}")
    print(f"Listening on port {SSH_PORT}. Press Ctrl+C to stop.")

    # Graceful shutdown
    shutdown_event = threading.Event()

    def signal_handler(signum, frame):
        print("\nShutting down...")
        logger.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while not shutdown_event.is_set():
            try:
                client_socket, client_addr = server_socket.accept()
                logger.info(f"New connection from {client_addr[0]}:{client_addr[1]}")

                # Handle each connection in a thread
                t = threading.Thread(
                    target=handle_connection,
                    args=(client_socket, client_addr),
                    daemon=True,
                    name=f"ssh-session-{client_addr[0]}",
                )
                t.start()

            except socket.timeout:
                continue
            except OSError:
                if not shutdown_event.is_set():
                    raise
                break

    except Exception as e:
        logger.critical(f"Server error: {e}", exc_info=True)
    finally:
        server_socket.close()
        logger.info("SSH Gateway stopped.")
        print("SSH Gateway stopped.")


if __name__ == '__main__':
    main()
