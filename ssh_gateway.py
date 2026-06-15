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
import shutil
from io import StringIO
from datetime import datetime

# Lazy loading proxy to avoid importing redis on startup
class LazyRedis:
    """
    Lazy proxy for redis.Redis to delay importing 'redis' until first access.
    """
    def __init__(self, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs
        self._client = None

    def _init_client(self):
        if self._client is None:
            import redis
            self._client = redis.Redis(*self._args, **self._kwargs)
        return self._client

    def __getattr__(self, name):
        client = self._init_client()
        return getattr(client, name)

from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.rule import Rule
from rich.columns import Columns
from rich import box

# ============================================================
# Configuration
# ============================================================
SSH_HOST = '0.0.0.0'
SSH_PORT = 2222
HOST_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ssh_gateway_host_key')
DATABASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stellar_local.db')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

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
# Logging & Thread-Local Storage
# ============================================================
os.makedirs(LOG_DIR, exist_ok=True)
_thread_local = threading.local()

class GatewayFormatter(logging.Formatter):
    """
    Custom logging formatter for the SSH gateway.
    Injects the thread-local SSH session ID into the log record to ensure traceability of actions.
    """
    def format(self, record):
        """
        Format the specified log record, injecting the thread-local SSH session ID.

        Args:
            record (logging.LogRecord): The log record to format.

        Returns:
            str: The formatted log record string.
        """
        try:
            session_id = getattr(_thread_local, 'session_id', 'system')
            record.session_id = session_id
        except Exception:
            record.session_id = 'error'
        return super().format(record)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(GatewayFormatter('%(asctime)s [%(levelname)s] %(name)s [session=%(session_id)s]: %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_console_handler], force=True)

logger = logging.getLogger('stellar_ssh')
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(os.path.join(LOG_DIR, 'ssh_gateway.log'))
_handler.setFormatter(GatewayFormatter('%(asctime)s [%(levelname)s] %(name)s [session=%(session_id)s]: %(message)s'))
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

# Shared Redis client connection pool to prevent socket descriptor leaks and connection overhead
if os.environ.get('TESTING') == 'true':
    import redis
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
else:
    redis_client = LazyRedis(host='localhost', port=6379, db=0, decode_responses=True)

def get_console(width: int) -> Console:
    """Get a thread-local Console instance to avoid expensive creation overhead."""
    if not hasattr(_thread_local, 'console'):
        _thread_local.console = Console(
            force_terminal=True,
            color_system="256",
            highlight=False,
            legacy_windows=False
        )
    # Use width - 1 to prevent auto-wrap layout bugs in the terminal
    _thread_local.console.width = max(20, width - 1)
    return _thread_local.console

# Optimize container status caching using a background thread and non-blocking reads to eliminate UI lag & flickering.
_container_statuses_cache = {}
_cache_lock = threading.Lock()
_refresher_started = False
_refresher_lock = threading.Lock()
_refresh_event = threading.Event()

def _docker_status_refresher():
    """
    Background worker loop that periodically queries the Docker daemon for container statuses.
    Caches the statuses in a thread-safe global cache and blocks/wakes up on invalidation events.
    """
    global _container_statuses_cache
    logger.info("Docker status refresher thread started")
    while True:
        try:
            client = get_docker_client()
            containers = client.containers.list(all=True)
            new_cache = {c.name: c.status for c in containers}
            with _cache_lock:
                _container_statuses_cache = new_cache
        except Exception as e:
            logger.error("Failed to list containers in background error=%s", e)
        # Wait for 2 seconds or until woken up (e.g. by cache invalidation)
        _refresh_event.wait(timeout=2.0)
        _refresh_event.clear()

def start_refresher_thread_if_needed():
    """
    Initializes and starts the background Docker status refresher thread if it is not already running.
    Performs a synchronous pre-warm status query on first invocation so the initial dashboard load is populated.
    """
    global _refresher_started
    if not _refresher_started:
        with _refresher_lock:
            if not _refresher_started:
                # Run the first status query synchronously so the initial page load has data
                try:
                    client = get_docker_client()
                    containers = client.containers.list(all=True)
                    global _container_statuses_cache
                    _container_statuses_cache = {c.name: c.status for c in containers}
                except Exception as e:
                    logger.error("Initial container status fetch failed error=%s", e)
                
                t = threading.Thread(target=_docker_status_refresher, daemon=True, name="docker-status-refresher")
                t.start()
                _refresher_started = True

def get_all_container_statuses() -> dict:
    """
    Retrieves the current state mapping of all Docker containers from the thread-safe status cache.

    Returns:
        dict: A dictionary mapping container names to their active status (e.g., 'running', 'exited').
    """
    start_refresher_thread_if_needed()
    with _cache_lock:
        return _container_statuses_cache.copy()

def invalidate_container_cache():
    """
    Signals the background status refresher thread to refresh the Docker container cache immediately.
    """
    _refresh_event.set()


# ============================================================
# ANSI Helpers
# ============================================================
CLEAR_SCREEN = '\x1b[2J\x1b[H'
HIDE_CURSOR = '\x1b[?25l'
SHOW_CURSOR = '\x1b[?25h'
RESET_STYLE = '\x1b[0m'


def send_raw(channel, text):
    """
    Transmits raw text or byte data over the SSH channel.
    Normalizes line endings to '\r\n' for proper carriage return rendering on terminal clients.

    Args:
        channel (paramiko.Channel): The active SSH channel.
        text (str or bytes): The string content or raw bytes to send.
    """
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
    """
    Redis-backed rate limiter for handling SSH connection rate limiting and authentication failure tracking.
    """
    def __init__(self):
        """
        Initialize the Redis rate limiter client connection.
        """
        # Re-use global Redis client connection to optimize performance
        self.r = redis_client

    def check_connection_rate(self, ip: str) -> bool:
        """
        Check if the connection rate limit has been exceeded for a given IP address.

        Args:
            ip (str): The client IP address.

        Returns:
            bool: True if connection is within the limit and allowed; False otherwise.
        """
        key = f"ssh_conn_rate:{ip}"
        try:
            count = self.r.incr(key)
            if count == 1:
                self.r.expire(key, RATE_LIMIT_WINDOW)
            return count <= RATE_LIMIT_CONNECTIONS
        except Exception as e:
            logger.error("Rate limiter Redis error error=%s", e)
            return True  # Fail open to avoid lockout on Redis issues

    def record_auth_failure(self, ip: str) -> int:
        """
        Record a failed authentication attempt for the given IP address.

        Args:
            ip (str): The client IP address.

        Returns:
            int: The cumulative count of auth failures within the window, or 0 on error.
        """
        key = f"ssh_verify_fail:{ip}"
        try:
            count = self.r.incr(key)
            if count == 1:
                self.r.expire(key, RATE_LIMIT_WINDOW)
            return count
        except Exception as e:
            logger.error("Rate limiter Redis failure in record_auth_failure ip=%s error=%s", ip, e, exc_info=True)
            return 0

    def is_ip_blocked(self, ip: str) -> bool:
        """
        Check if the given IP address is blocked due to excessive authentication failures.

        Args:
            ip (str): The client IP address.

        Returns:
            bool: True if the IP is blocked (failures >= 10); False otherwise.
        """
        try:
            count = self.r.get(f"ssh_verify_fail:{ip}")
            return int(count or 0) >= 10
        except Exception as e:
            logger.error("Rate limiter Redis failure in is_ip_blocked ip=%s error=%s", ip, e, exc_info=True)
            return False


rate_limiter = RateLimiter()


# ============================================================
# Database Helper
# ============================================================
def get_user_repos(user_id: int) -> list:
    """
    Fetches all repository deployment records from the SQLite database for a specific user.
    Enables WAL mode and handles lock contentions with a busy timeout.

    Args:
        user_id (int): The unique database identifier of the user.

    Returns:
        list of dict: A list of dicts containing project details (id, name, process_id, container_id, status, subdomain, created, app_type).
    """
    repos = []
    conn = None
    try:
        t0 = time.time()
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        # Set WAL mode and busy timeout to avoid database locked errors under load
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        duration = time.time() - t0
        if duration > 0.05:
            logger.warning("Slow SSH Gateway database connection duration_sec=%.3f", duration)
        
        t_query = time.time()
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
        query_duration = time.time() - t_query
        if query_duration > 0.1:
            logger.warning("Slow SSH Gateway query duration_sec=%.3f user_id=%d", query_duration, user_id)
    except Exception as e:
        logger.error("DB error fetching repos user_id=%s error=%s", user_id, e, exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return repos


def get_container(client, process_id: str, app_type: str):
    """
    Looks up a Docker container associated with a deployment using standard naming patterns.
    Checks the specific app type name prefix first and falls back to generic repo naming conventions if not found.

    Args:
        client (docker.DockerClient): The Docker client instance.
        process_id (str): The process identifier of the application.
        app_type (str): The application type configuration.

    Returns:
        docker.models.containers.Container: The container instance matching the process ID.
    """
    import docker
    try:
        return client.containers.get(f"stellar-{app_type}-{process_id}")
    except docker.errors.NotFound:
        return client.containers.get(f"stellar-repo-{process_id}")


_docker_client = None
_docker_client_lock = threading.Lock()

def get_docker_client():
    """
    Returns a thread-safe, shared Docker client instance initialized from the system environment.

    Returns:
        docker.DockerClient: The system-wide Docker client.
    """
    global _docker_client
    if _docker_client is None:
        with _docker_client_lock:
            if _docker_client is None:
                import docker
                _docker_client = docker.from_env()
    return _docker_client


def get_container_status(process_id: str, app_type: str = 'repo') -> str:
    """
    Looks up the cached container status for a specific deployment process from the background-refreshed status map.

    Args:
        process_id (str): The process identifier of the application.
        app_type (str, optional): The application type. Defaults to 'repo'.

    Returns:
        str: The status of the container (e.g. 'running', 'exited'), or 'not_found'.
    """
    statuses = get_all_container_statuses()
    name1 = f"stellar-{app_type}-{process_id}"
    name2 = f"stellar-repo-{process_id}"
    if name1 in statuses:
        return statuses[name1]
    if name2 in statuses:
        return statuses[name2]
    return 'not_found'



# ============================================================
# Auth Code Verification (calls local API)
# ============================================================
def verify_auth_code(code: str) -> dict | None:
    """
    Verify a user's one-time authentication code against Redis directly.
    Invalidates the code immediately on successful validation to ensure one-time usage security,
    decrements the active code count, and records the event to the audit log.

    Args:
        code (str): The 6-character authentication code entered by the user.

    Returns:
        dict | None: The user session info dictionary containing user_id and username if valid; None otherwise.
    """
    clean_code = code.strip().replace('-', '').replace(' ', '').upper()
    if len(clean_code) != 6:
        return None

    try:
        # Re-use global Redis client connection to optimize performance
        r = redis_client
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
        logger.error("Auth code verification error error=%s", e)
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
        """
        Initialize the SSH server interface.

        Args:
            client_addr (str): The IP address of the incoming SSH client.
        """
        self.client_addr = client_addr
        self.event = threading.Event()
        self.term_width = 80
        self.term_height = 24
        self.resize_event = threading.Event()

    def check_channel_request(self, kind, chanid):
        """
        Determine if the requested channel type is supported. Only 'session' is allowed.

        Args:
            kind (str): The kind of channel requested.
            chanid (int): The channel identifier.

        Returns:
            int: paramiko.OPEN_SUCCEEDED if allowed; paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED otherwise.
        """
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        logger.warning("Rejected channel request kind=%s from=%s", kind, self.client_addr)
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_none(self, username):
        """
        Accept authentication without password or key at the transport layer.
        Actual authentication occurs inside the interactive TUI.

        Args:
            username (str): The requested SSH username.

        Returns:
            int: paramiko.AUTH_SUCCESSFUL.
        """
        self.username = username
        # Accept none-auth; real auth happens in TUI
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_password(self, username, password):
        """
        Reject password authentication attempts at the transport layer.

        Args:
            username (str): The username trying to authenticate.
            password (str): The password credential.

        Returns:
            int: paramiko.AUTH_FAILED.
        """
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        """
        Reject public-key authentication attempts at the transport layer.

        Args:
            username (str): The username trying to authenticate.
            key (paramiko.PKey): The public key credential.

        Returns:
            int: paramiko.AUTH_FAILED.
        """
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        """
        Declare the list of allowed authentication methods.

        Args:
            username (str): The username querying authentication methods.

        Returns:
            str: 'none' to indicate only transport-level open auth is allowed.
        """
        return 'none'

    def check_channel_shell_request(self, channel):
        """
        Acknowledge a shell channel request by triggering the server event.

        Args:
            channel (paramiko.Channel): The active SSH channel.

        Returns:
            bool: True.
        """
        self.event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        """
        Configure the terminal dimensions requested by the client.

        Args:
            channel (paramiko.Channel): The active SSH channel.
            term (str): The terminal emulation type.
            width (int): The terminal column width.
            height (int): The terminal row height.
            pixelwidth (int): Pixel width.
            pixelheight (int): Pixel height.
            modes (dict): Terminal modes.

        Returns:
            bool: True.
        """
        self.term_width = width
        self.term_height = height
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        """
        Update terminal dimensions and set resize event upon window size changes.

        Args:
            channel (paramiko.Channel): The active SSH channel.
            width (int): The new terminal column width.
            height (int): The new terminal row height.
            pixelwidth (int): New pixel width.
            pixelheight (int): New pixel height.

        Returns:
            bool: True.
        """
        self.term_width = width
        self.term_height = height
        self.resize_event.set()
        return True

    # --- SECURITY: Block all forwarding ---
    def check_port_forward_request(self, address, port):
        """
        Deny port forwarding requests for security compliance.

        Args:
            address (str): Target address.
            port (int): Target port.

        Returns:
            bool: False.
        """
        logger.warning("BLOCKED port forward request from=%s target=%s:%s", self.client_addr, address, port)
        return False

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        """
        Deny direct TCP/IP channel requests to prevent tunneling.

        Args:
            chanid (int): Channel ID.
            origin (tuple): Source socket address.
            destination (tuple): Destination socket address.

        Returns:
            int: paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED.
        """
        logger.warning("BLOCKED direct-tcpip from=%s origin=%s destination=%s", self.client_addr, origin, destination)
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_env_request(self, channel, name, value):
        """
        Reject client environment variable setup requests.

        Args:
            channel (paramiko.Channel): The active SSH channel.
            name (str): Env variable name.
            value (str): Env variable value.

        Returns:
            bool: False.
        """
        return False

    def check_channel_x11_request(self, channel, single_connection, auth_protocol, auth_cookie, screen_number):
        """
        Deny X11 forwarding requests for security compliance.

        Args:
            channel (paramiko.Channel): The active SSH channel.
            single_connection (bool): Single connection flag.
            auth_protocol (str): Auth protocol.
            auth_cookie (str): Auth cookie.
            screen_number (int): Screen number.

        Returns:
            bool: False.
        """
        logger.warning("BLOCKED X11 forwarding from=%s", self.client_addr)
        return False

    def check_channel_forward_agent_request(self, channel):
        """
        Deny SSH agent forwarding requests.

        Args:
            channel (paramiko.Channel): The active SSH channel.

        Returns:
            bool: False.
        """
        logger.warning("BLOCKED agent forwarding from=%s", self.client_addr)
        return False


# ============================================================
# TUI Renderer (uses Rich → StringIO → channel)
# ============================================================
from rich.columns import Columns
from rich.console import Group

class TUI:
    """
    Terminal User Interface rendering engine for the SSH Gateway.
    Uses the Rich library to assemble panels, layouts, tables, and color themes.
    """

    THEMES = [
        {
            "name": "Stellar Classic",
            "bg": "black",
            "border": "white",
            "primary": "white",
            "accent": "grey74",
            "text": "white",
            "dim": "grey50"
        },
        {
            "name": "Midnight Cyan",
            "bg": "#001A33",
            "border": "#007ACC",
            "primary": "#00FFFF",
            "accent": "white",
            "text": "white",
            "dim": "#6699CC"
        },
        {
            "name": "Matrix Green",
            "bg": "black",
            "border": "green",
            "primary": "bright_green",
            "accent": "#CCFF00",
            "text": "green",
            "dim": "dark_green"
        },
        {
            "name": "Claude Classic",
            "bg": "#0E0E0E",
            "border": "#444444",
            "primary": "#E38B68",
            "accent": "#6ECFFF",
            "text": "white",
            "dim": "#888888"
        }
    ]

    BORDER_COLORS = [
        {"name": "Theme Default", "value": None},
        {"name": "Bright Blue", "value": "bright_blue"},
        {"name": "Cyan", "value": "cyan"},
        {"name": "Green", "value": "green"},
        {"name": "Magenta", "value": "magenta"},
        {"name": "Red", "value": "red"},
        {"name": "White", "value": "white"}
    ]

    @staticmethod
    def get_logo(theme: dict) -> str:
        """
        Generate a styled micro-logo string using theme primary and dim colors.

        Args:
            theme (dict): The active theme configuration dictionary.

        Returns:
            str: Rich-markup formatted logo string.
        """
        return f"[bold {theme['primary']}]Stellar[/bold {theme['primary']}] [{theme['dim']}]Code[/{theme['dim']}]"

    @staticmethod
    def get_big_logo(theme: dict) -> str:
        """
        Generate the large ASCII art banner styled with theme colors.

        Args:
            theme (dict): The active theme configuration dictionary.

        Returns:
            str: Rich-markup formatted ASCII art logo.
        """
        c = theme['primary']
        return f"""
 [bold {c}]███████╗████████╗███████╗██╗     ██╗      █████╗ ██████╗ [/bold {c}]
 [bold {c}]██╔════╝╚══██╔══╝██╔════╝██║     ██║     ██╔══██╗██╔══██╗[/bold {c}]
 [bold {c}]███████╗   ██║   █████╗  ██║     ██║     ███████║██████╔╝[/bold {c}]
 [bold {c}]╚════██║   ██║   ██╔══╝  ██║     ██║     ██╔══██║██╔══██╗[/bold {c}]
 [bold {c}]███████║   ██║   ███████╗███████╗███████╗██║  ██║██║  ██║[/bold {c}]
 [bold {c}]╚══════╝   ╚═╝   ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝[/bold {c}]
"""

    @staticmethod
    def _render(width: int, height: int, content, theme: dict) -> str:
        """
        Wraps the given UI component in a themed box and prints it to the console buffer.

        Args:
            width (int): Current terminal column width.
            height (int): Current terminal row height.
            content (rich.console.RenderableType): The rich UI renderable content.
            theme (dict): The active color theme mapping.

        Returns:
            str: The rendered ANSI escape-coded output string buffer.
        """
        from rich.align import Align
        buf = StringIO()
        
        # Reuse thread-local Console to avoid creation overhead
        console = get_console(width)
        console.file = buf
        
        # Catch narrow or short terminal dimensions to avoid Panel layout crash
        if height < 5 or width < 20:
            console.print(content)
            return buf.getvalue()
            
        # Wrap everything in the master "app box"
        # Optimize: Reduce height by 1 and use end="" to prevent terminal scrolling/flickering
        panel = Panel(
            content,
            style=f"{theme['text']} on {theme['bg']}",
            border_style=theme['border'],
            box=box.DOUBLE,
            padding=(1, 2),
            expand=True,
            height=height - 1
        )
        console.print(panel, end="")
        return buf.getvalue().rstrip('\r\n')

    @staticmethod
    def theme_picker(selected_theme: int, selected_border: int, focus: str, width: int, height: int, is_default: bool = False) -> str:
        """
        Render the interactive theme selection UI page.

        Args:
            selected_theme (int): The index of currently selected theme in THEMES.
            selected_border (int): The index of currently selected border color in BORDER_COLORS.
            focus (str): The list currently focused ('theme' or 'border').
            width (int): Current terminal column width.
            height (int): Current terminal row height.
            is_default (bool): True if the user flagged this selection as default.

        Returns:
            str: Rendered string buffer representing the theme picker UI.
        """
        base_theme = TUI.THEMES[selected_theme]
        border_override = TUI.BORDER_COLORS[selected_border]["value"]
        
        theme = base_theme.copy()
        if border_override:
            theme["border"] = border_override
        
        # Left side: Lists
        list_text = Text()
        list_text.append("\n  Select Theme:\n\n", style=f"bold {theme['text']}")
        for i, t in enumerate(TUI.THEMES):
            if i == selected_theme:
                marker = "▸" if focus == "theme" else " "
                list_text.append(f"    {marker} {t['name']}\n", style=f"bold {theme['accent']}")
            else:
                list_text.append(f"      {t['name']}\n", style=theme['dim'])
                
        list_text.append("\n  Select Border Color:\n\n", style=f"bold {theme['text']}")
        for i, b in enumerate(TUI.BORDER_COLORS):
            if i == selected_border:
                marker = "▸" if focus == "border" else " "
                color_style = b["value"] if b["value"] else theme['text']
                list_text.append(f"    {marker} {b['name']}\n", style=f"bold {color_style}")
            else:
                color_style = b["value"] if b["value"] else theme['dim']
                list_text.append(f"      {b['name']}\n", style=color_style)

        checkbox = "[x]" if is_default else "[ ]"
        list_text.append(f"\n  {checkbox} Set as Default\n", style=f"bold {theme['accent']}" if is_default else theme['dim'])

        list_text.append(f"\n  ↑↓ Navigate | ←→ Switch List | Space Toggle Default | Enter Confirm", style=theme['dim'])

        # Right side: Preview Box
        preview_header = Text.from_markup(f"{TUI.get_logo(theme)}\n[{theme['dim']}]──────────────────────────────[/{theme['dim']}]\n")
        
        preview_table = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style=f"bold {theme['primary']}",
            border_style=theme['border'],
            expand=True,
            padding=(0, 1)
        )
        preview_table.add_column("Project", style=theme['text'])
        preview_table.add_column("Status")
        
        preview_table.add_row(f"[bold {theme['text']}]▸ Project Alpha[/bold {theme['text']}]", f"[green]●[/green] [{theme['text']}]Running[/{theme['text']}]")
        preview_table.add_row("", "")
        preview_table.add_row(f"  Project Beta", f"[red]○[/red] [{theme['dim']}]Stopped[/{theme['dim']}]")

        preview_group = Group(preview_header, preview_table)

        preview_panel = Panel(
            preview_group,
            title=f"Preview",
            title_align="left",
            style=f"{theme['text']} on {theme['bg']}",
            border_style=theme['border'],
            box=box.ROUNDED,
            width=40,
            padding=(1, 2)
        )

        cols = Columns([list_text, preview_panel], expand=True)
        from rich.align import Align
        return TUI._render(width, height, Align(cols, vertical="middle"), theme)

    @staticmethod
    def auth_screen(width: int, height: int, typed_code: str = "", error_msg: str = "", theme: dict = None) -> str:
        """
        Render the authentication screen where the user enters their 6-digit one-time access code.

        Args:
            width (int): Current terminal column width.
            height (int): Current terminal row height.
            typed_code (str): The code typed so far by the user.
            error_msg (str): Error message to display, if any.
            theme (dict): The active theme configuration dictionary.

        Returns:
            str: Rendered string buffer representing the authentication page.
        """
        if not theme: theme = TUI.THEMES[0]
        content = Text.from_markup(TUI.get_big_logo(theme) + "\n", justify="center")
        content.append("\nAuthentication required\n", style=f"bold {theme['text']}")
        content.append(f"Visit ", style=theme['dim'])
        content.append("https://stellarai.live/auth/ssh", style=f"bold underline {theme['accent']}")
        content.append(f"\nto generate your one-time access code.\n\n", style=theme['dim'])
        
        # Format code
        display_chars = []
        for i in range(6):
            if i < len(typed_code):
                display_chars.append(f"[bold {theme['accent']}]{typed_code[i]}[/bold {theme['accent']}]")
            else:
                display_chars.append(f"[{theme['dim']}]_[/{theme['dim']}]")
                
        formatted = f"{display_chars[0]} {display_chars[1]} {display_chars[2]} [{theme['dim']}]-[/{theme['dim']}] {display_chars[3]} {display_chars[4]} {display_chars[5]}"
        content.append(Text.from_markup(f"Enter Code: {formatted}"))
        
        if error_msg:
            content.append(Text.from_markup(f"\n\n[bold red]![/bold red] {error_msg}"))
            
        from rich.align import Align
        return TUI._render(width, height, Align(content, vertical="middle", align="center"), theme)

    @staticmethod
    def dashboard(repos: list, selected: int, username: str, width: int, height: int, status_msg: str = "", theme: dict = None, search_query: str = "", filter_state: str = "All", sort_state: str = "Name", mode: str = "NORMAL", status_map: dict = None) -> str:
        """
        Render the primary dashboard screen showing deployments list, filters, sorting options, and controls.

        Args:
            repos (list): List of user repository configurations.
            selected (int): Currently selected index of the repository in the list.
            username (str): The active session username.
            width (int): Current terminal column width.
            height (int): Current terminal row height.
            status_msg (str): General status notification/alert message to display in the footer.
            theme (dict): Active theme dictionary.
            search_query (str): Active search filter query.
            filter_state (str): Current filter condition state ('All', 'Running', 'Stopped').
            sort_state (str): Current sorting criteria ('Name', 'Status', 'Created').
            mode (str): Active navigation/entry mode ('NORMAL' or 'SEARCH').
            status_map (dict): Cached container status mapping dictionary.

        Returns:
            str: Rendered string buffer representing the dashboard UI.
        """
        if not theme: theme = TUI.THEMES[0]
        
        header_table = Table.grid(expand=True)
        header_table.add_column(justify="left")
        header_table.add_column(justify="right")
        
        user_info = Text.from_markup(f"  {TUI.get_logo(theme)} [{theme['dim']}]›[/{theme['dim']}] [{theme['text']}]{username}[/{theme['text']}]")
        
        filters_disp = Text()
        filters_disp.append(f"Filter: {filter_state}  ", style=f"bold {theme['accent']}" if filter_state != "All" else theme['dim'])
        filters_disp.append(f"Sort: {sort_state}  ", style=f"bold {theme['accent']}" if sort_state != "Name" else theme['dim'])
        
        header_table.add_row(user_info, filters_disp)

        # Persistent Search Box
        search_border = theme['accent'] if mode == "SEARCH" else theme['border']
        search_content = Text()
        search_content.append("Search: ", style=f"bold {theme['text']}")
        if mode == "SEARCH":
            search_content.append(f" {search_query}", style=theme['text'])
            search_content.append("█", style=theme['accent'])
        elif search_query:
            search_content.append(f" {search_query}", style=theme['text'])
        else:
            search_content.append(" Type / to search...", style=theme['dim'])
            
        search_panel = Panel(
            search_content,
            box=box.ROUNDED,
            border_style=search_border,
            padding=(0, 2),
            expand=True
        )
        
        header = Group(header_table, Text("\n"), search_panel, Text("\n"))

        if not repos:
            empty_msg = f"No deployments found matching current filters." if (search_query or filter_state != "All") else f"No active deployments found."
            table_or_empty = Text.from_markup(f"  [italic {theme['dim']}]{empty_msg}[/italic {theme['dim']}]\n")
        else:
            table = Table(
                box=box.ROUNDED,
                show_header=True,
                header_style=f"bold {theme['primary']}",
                border_style=theme['border'],
                expand=True,
                padding=(0, 2)
            )
            table.add_column(" ")
            table.add_column("Project")
            table.add_column("Status", justify="left")
            table.add_column("Subdomain", style=theme['dim'])
            table.add_column("Created", style=theme['dim'])

            for i, repo in enumerate(repos):
                is_sel = (i == selected)
                marker = f"[bold {theme['accent']}]▸[/bold {theme['accent']}]" if is_sel else " "
                
                status_raw = status_map.get(repo['process_id']) if status_map else None
                if not status_raw:
                    status_raw = get_container_status(repo['process_id'], repo.get('app_type', 'repo'))
                
                status_disp_raw = status_raw
                if status_raw == 'running':
                    status_icon_sel = "[bold green]●[/bold green]"
                    status_icon_dim = "[dim green]●[/dim green]"
                else:
                    status_disp_raw = 'stopped'
                    status_icon_sel = "[bold red]○[/bold red]"
                    status_icon_dim = "[dim red]○[/dim red]"
                
                if is_sel:
                    status_disp = f"{status_icon_sel} [{theme['text']}]{status_disp_raw}[/{theme['text']}]"
                else:
                    status_disp = f"[{theme['dim']}]{status_icon_dim} {status_disp_raw}[/{theme['dim']}]"
                
                name_style = f"bold {theme['text']}" if is_sel else theme['text']
                name_disp = f"[{name_style}]{repo['name']}[/{name_style}]"
                
                sub_style = theme['text'] if is_sel else theme['dim']
                sub_disp = f"[{sub_style}]{repo['subdomain']}[/{sub_style}]"
                
                created = repo.get('created', '-')
                if len(created) > 10: created = created[:10]
                created_disp = f"[{sub_style}]{created}[/{sub_style}]"

                table.add_row(marker, name_disp, status_disp, sub_disp, created_disp)
                # Add gap row if not the last item
                if i < len(repos) - 1:
                    table.add_row("", "", "", "", "")

            table_or_empty = table

        nav_footer = Table.grid(expand=True)
        nav_footer.add_column(justify="left", no_wrap=True)
        nav_footer.add_column(justify="right", no_wrap=True)

        if mode == "SEARCH":
            controls = Text.from_markup(f"  [bold {theme['text']}]ESC[/bold {theme['text']}] [{theme['dim']}]Exit Search[/{theme['dim']}]   [bold {theme['text']}]Backspace[/bold {theme['text']}] [{theme['dim']}]Delete[/{theme['dim']}]")
        else:
            controls = Text.from_markup(
                f"\n  [bold {theme['text']}]↑↓[/bold {theme['text']}] [{theme['dim']}]Nav[/{theme['dim']}]  "
                f"[bold {theme['text']}]Enter[/bold {theme['text']}] [{theme['dim']}]Open[/{theme['dim']}]  "
                f"[bold {theme['text']}]/[/bold {theme['text']}] [{theme['dim']}]Search[/{theme['dim']}]  "
                f"[bold {theme['text']}]F[/bold {theme['text']}] [{theme['dim']}]Filter[/{theme['dim']}]  "
                f"[bold {theme['text']}]O[/bold {theme['text']}] [{theme['dim']}]Sort[/{theme['dim']}]  "
                f"[bold {theme['text']}]L[/bold {theme['text']}] [{theme['dim']}]Logs[/{theme['dim']}]  "
                f"[bold {theme['text']}]R/S[/bold {theme['text']}] [{theme['dim']}]Restart/Stop[/{theme['dim']}]  "
                f"[bold {theme['text']}]T[/bold {theme['text']}] [{theme['dim']}]Theme[/{theme['dim']}]  "
                f"[bold {theme['text']}]Q[/bold {theme['text']}] [{theme['dim']}]Quit[/{theme['dim']}]"
            )
        
        mode_disp = f"[{theme['accent']}]{mode} MODE[/{theme['accent']}]  " if mode != "NORMAL" else ""
        nav_footer.add_row(controls, Text.from_markup(mode_disp))

        status_disp = Text.from_markup(f"\n  [bold yellow]![/bold yellow] [{theme['text']}]{status_msg}[/{theme['text']}]") if status_msg else Text("")

        from rich.align import Align
        group = Group(header, table_or_empty, Align.center(nav_footer), Align.center(status_disp))
        
        return TUI._render(width, height, group, theme)

    @staticmethod
    def connecting_screen(repo_name: str, width: int, height: int, theme: dict = None) -> str:
        """
        Render a temporary transition screen shown while connecting to a container shell.

        Args:
            repo_name (str): The name of the target repository/container.
            width (int): Current terminal column width.
            height (int): Current terminal row height.
            theme (dict): Active theme configuration dictionary.

        Returns:
            str: Rendered string buffer representing the connection status screen.
        """
        if not theme: theme = TUI.THEMES[0]
        content = Text.from_markup(f"\n\n[{theme['dim']}]Connecting to[/{theme['dim']}] [bold {theme['text']}]{repo_name}[/bold {theme['text']}]\n\n", justify="center")
        from rich.align import Align
        return TUI._render(width, height, Align(content, vertical="middle", align="center"), theme)

    @staticmethod
    def logs_screen(repo_name: str, logs: list, width: int, height: int, theme: dict = None) -> str:
        """
        Render the real-time container log viewer screen.

        Args:
            repo_name (str): The name of the repository/container.
            logs (list): List of logs lines to show.
            width (int): Current terminal column width.
            height (int): Current terminal row height.
            theme (dict): Active theme configuration dictionary.

        Returns:
            str: Rendered string buffer representing the logs viewer UI.
        """
        if not theme: theme = TUI.THEMES[0]
        
        header = Text.from_markup(f"  {TUI.get_logo(theme)} [{theme['dim']}]› Logs ›[/{theme['dim']}] [{theme['text']}]{repo_name}[/{theme['text']}]\n")
        
        log_text = Text()
        for log in logs[-15:]: # Show last 15 lines safely within the box
            log_text.append(f"{log}\n", style=theme['text'])
            
        table_or_empty = Panel(log_text, box=box.ROUNDED, border_style=theme['border'], padding=(1, 2), expand=True)

        nav = Text.from_markup(f"\n  [bold {theme['text']}]Q / ESC[/bold {theme['text']}] [{theme['dim']}]Back to Dashboard[/{theme['dim']}]")
        
        from rich.align import Align
        group = Group(header, table_or_empty, Align.center(nav))
        return TUI._render(width, height, group, theme)

    @staticmethod
    def goodbye_screen(username: str, width: int, height: int, theme: dict = None) -> str:
        """
        Render the session termination / goodbye screen.

        Args:
            username (str): The active session username.
            width (int): Current terminal column width.
            height (int): Current terminal row height.
            theme (dict): Active theme configuration dictionary.

        Returns:
            str: Rendered string buffer representing the goodbye page.
        """
        if not theme: theme = TUI.THEMES[0]
        content = Text.from_markup(f"\n\n[{theme['dim']}]Goodbye,[/{theme['dim']}] [bold {theme['text']}]{username}[/bold {theme['text']}][{theme['dim']}]. Session terminated.[/{theme['dim']}]\n\n", justify="center")
        from rich.align import Align
        return TUI._render(width, height, Align(content, vertical="middle", align="center"), theme)


# ============================================================
# Input Reader
# ============================================================
def read_key(channel, timeout: float = 0.5) -> str | None:
    """Read a single keypress or escape sequence from SSH channel, using a thread-local buffer to prevent dropped keys and support pastes."""
    # Ensure thread-local input buffer exists
    if not hasattr(_thread_local, 'input_buffer'):
        _thread_local.input_buffer = bytearray()

    buf = _thread_local.input_buffer

    # If buffer is empty, block up to timeout to read some data from channel
    if not buf:
        try:
            # Bolt - Performance optimization: check if paramiko has data buffered internally
            # to avoid blocking on select.select() when data is already available.
            if channel.recv_ready():
                data = channel.recv(1024)
                if not data:
                    return 'EOF'
                buf.extend(data)
            else:
                ready = select.select([channel], [], [], timeout)
                if not ready[0]:
                    return None
                data = channel.recv(1024)  # Read up to 1024 bytes to handle rapid inputs/pastes
                if not data:
                    return 'EOF'
                buf.extend(data)
        except Exception:
            return 'EOF'

    # If the buffer starts with ESC but is incomplete, wait briefly for the rest of the sequence
    if len(buf) > 0 and buf[0] == 0x1b and len(buf) < 3:
        try:
            # Bolt - Performance optimization: check if paramiko has data buffered internally
            # to avoid blocking on select.select() for escape sequences when data is already available.
            if channel.recv_ready():
                data = channel.recv(1024)
                if data:
                    buf.extend(data)
            else:
                # Wait up to 50ms for the rest of the sequence (e.g. arrow keys)
                ready = select.select([channel], [], [], 0.05)
                if ready[0]:
                    data = channel.recv(1024)
                    if data:
                        buf.extend(data)
        except Exception:
            pass

    # Now parse from the buffer
    if not buf:
        return None

    # Check for recognized 3-byte escape sequences
    if buf[0] == 0x1b:
        if len(buf) >= 3:
            seq = bytes(buf[:3])
            if seq in (b'\x1b[A', b'\x1bOA'):
                del buf[:3]
                return 'UP'
            if seq in (b'\x1b[B', b'\x1bOB'):
                del buf[:3]
                return 'DOWN'
            if seq in (b'\x1b[C', b'\x1bOC'):
                del buf[:3]
                return 'RIGHT'
            if seq in (b'\x1b[D', b'\x1bOD'):
                del buf[:3]
                return 'LEFT'
        
        # If we have at least 3 bytes and it's not a recognized arrow key,
        # or if we waited 50ms and it's still less than 3 bytes:
        # Treat the 0x1b as 'ESC' and leave the rest in the buffer.
        del buf[:1]
        return 'ESC'

    first = buf[0]
    if first in (0x0d, 0x0a):
        del buf[:1]
        return 'ENTER'
    if first in (0x7f, 0x08):
        del buf[:1]
        return 'BACKSPACE'
    if first == 0x03:
        del buf[:1]
        return 'CTRL_C'
    if first == 0x04:
        del buf[:1]
        return 'CTRL_D'

    # Batch decode consecutive printable characters to handle rapid typing and pastes efficiently,
    # reducing redrawing and network overhead (Bolt - Performance optimization).
    if first >= 0x20 and first != 0x7f:
        limit = 0
        while limit < len(buf) and buf[limit] >= 0x20 and buf[limit] != 0x7f:
            limit += 1
        decoded = None
        for l in range(limit, 0, -1):
            try:
                decoded = buf[:l].decode('utf-8')
                del buf[:l]
                break
            except UnicodeDecodeError:
                continue
        if decoded:
            return decoded

    # Decode UTF-8 char from the beginning of the buffer
    for i in range(1, min(len(buf) + 1, 5)):
        try:
            char_str = buf[:i].decode('utf-8')
            del buf[:i]
            return char_str
        except UnicodeDecodeError:
            pass
    
    # If decoding failed, discard the first byte to avoid deadlock
    del buf[:1]
    return None


def read_line(channel, prompt: str, mask: bool = False, max_len: int = 20) -> str | None:
    """
    Read a full line of text input from the client channel with echo or masked echo.

    Args:
        channel (paramiko.Channel): The active SSH channel.
        prompt (str): Prompt string to display to the user.
        mask (bool): If True, mask characters with '*' (e.g. for passcode entry).
        max_len (int): Maximum input length allowed.

    Returns:
        str | None: The accumulated input string, or None if connection was closed or cancelled.
    """
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
    Attach an interactive terminal session to the target Docker container.
    Launches /bin/bash inside the container and pipes bidirectional I/O between the SSH channel
    and the container's exec socket. Automatically handles terminal window resize events
    and enforces session inactivity idle timeout.

    Args:
        channel (paramiko.Channel): The active SSH client channel.
        server (StellarSSHServer): The SSH server instance tracking connection settings.
        process_id (str): The unique process identifier of the application.
        app_type (str): The application environment type.
        user_id (int): The ID of the authenticated user.
    """
    try:
        import docker
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"SHELL_ATTACH | user_id={user_id} | container={container_name}")

        if container.status != 'running':
            logger.warning("Cannot attach shell to non-running container container_name=%s user_id=%d", container_name, user_id)
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
                # Bolt - Performance optimization: Check if Paramiko has data buffered internally
                # to process immediately, bypassing select.select and avoiding terminal/typing lag.
                if channel.recv_ready():
                    r_list = [channel]
                else:
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
        logger.warning("Container not found for shell attach process_id=%s app_type=%s", process_id, app_type)
        send_raw(channel, "\r\n\x1b[31m  Container not found. It may have been removed.\x1b[0m\r\n")
        time.sleep(1.5)
    except Exception as e:
        logger.error("Shell attach error container_name=%s error=%s", container_name, e, exc_info=True)
        send_raw(channel, f"\r\n\x1b[31m  Error: {str(e)[:80]}\x1b[0m\r\n")
        time.sleep(1.5)


# Container Logs Viewer (streaming is handled inline via diff checks on timeout/resize)


# ============================================================
# Container Management Actions
# ============================================================
def restart_container(process_id: str, app_type: str, user_id: int) -> str:
    """
    Restart the Docker container associated with the specified deployment.

    Args:
        process_id (str): The process identifier of the application.
        app_type (str): The application type (e.g. 'repo').
        user_id (int): The ID of the requesting user (for audit logging).

    Returns:
        str: Success or failure status message.
    """
    try:
        import docker
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info("CONTAINER_RESTART | user_id=%s | container=%s", user_id, container_name)
        start_time = time.time()
        container.restart(timeout=10)
        duration = time.time() - start_time
        logger.info("Container restart complete container_name=%s duration_sec=%.2f", container_name, duration)
        audit.info("CONTAINER_RESTART_SUCCESS | user_id=%s | container=%s | duration_sec=%.2f", user_id, container_name, duration)
        # Optimize: Update local container status cache directly to prevent race conditions and stale UI states
        # (Bolt - Performance optimization: direct cache update).
        with _cache_lock:
            _container_statuses_cache[container_name] = 'running'
        # Invalidate container cache so it updates instantly
        invalidate_container_cache()
        return f"✓ {container_name} restarted successfully"
    except docker.errors.NotFound:
        logger.warning("Container not found for restart process_id=%s app_type=%s", process_id, app_type)
        return "✗ Container not found"
    except Exception as e:
        logger.error("Container restart failed process_id=%s error=%s", process_id, e, exc_info=True)
        return f"✗ Restart failed: {str(e)[:60]}"


def stop_container(process_id: str, app_type: str, user_id: int) -> str:
    """
    Stop the Docker container associated with the specified deployment.

    Args:
        process_id (str): The process identifier of the application.
        app_type (str): The application type (e.g. 'repo').
        user_id (int): The ID of the requesting user (for audit logging).

    Returns:
        str: Success or failure status message.
    """
    try:
        import docker
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info("CONTAINER_STOP | user_id=%s | container=%s", user_id, container_name)
        start_time = time.time()
        container.stop(timeout=10)
        duration = time.time() - start_time
        logger.info("Container stop complete container_name=%s duration_sec=%.2f", container_name, duration)
        audit.info("CONTAINER_STOP_SUCCESS | user_id=%s | container=%s | duration_sec=%.2f", user_id, container_name, duration)
        # Optimize: Update local container status cache directly to prevent race conditions and stale UI states
        # (Bolt - Performance optimization: direct cache update).
        with _cache_lock:
            _container_statuses_cache[container_name] = 'exited'
        # Invalidate container cache so it updates instantly
        invalidate_container_cache()
        return f"✓ {container_name} stopped"
    except docker.errors.NotFound:
        logger.warning("Container not found for stop process_id=%s app_type=%s", process_id, app_type)
        return "✗ Container not found"
    except Exception as e:
        logger.error("Container stop failed process_id=%s error=%s", process_id, e, exc_info=True)
        return f"✗ Stop failed: {str(e)[:60]}"


def start_container(process_id: str, app_type: str, user_id: int) -> str:
    """
    Start the Docker container associated with the specified deployment.

    Args:
        process_id (str): The process identifier of the application.
        app_type (str): The application type (e.g. 'repo').
        user_id (int): The ID of the requesting user (for audit logging).

    Returns:
        str: Success or failure status message.
    """
    try:
        import docker
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info("CONTAINER_START | user_id=%s | container=%s", user_id, container_name)
        start_time = time.time()
        container.start()
        duration = time.time() - start_time
        logger.info("Container start complete container_name=%s duration_sec=%.2f", container_name, duration)
        audit.info("CONTAINER_START_SUCCESS | user_id=%s | container=%s | duration_sec=%.2f", user_id, container_name, duration)
        # Optimize: Update local container status cache directly to prevent race conditions and stale UI states
        # (Bolt - Performance optimization: direct cache update).
        with _cache_lock:
            _container_statuses_cache[container_name] = 'running'
        # Invalidate container cache so it updates instantly
        invalidate_container_cache()
        return f"✓ {container_name} started"
    except docker.errors.NotFound:
        logger.warning("Container not found for start process_id=%s app_type=%s", process_id, app_type)
        return "✗ Container not found"
    except Exception as e:
        logger.error("Container start failed process_id=%s error=%s", process_id, e, exc_info=True)
        return f"✗ Start failed: {str(e)[:60]}"


# ============================================================
# Theme Persistence
# ============================================================
def load_theme(user_id=None):
    """
    Load the saved theme index and border index preferences for the specified user.

    Args:
        user_id (int or str, optional): The user ID. Defaults to None.

    Returns:
        dict: A dictionary containing theme_idx and border_idx.
    """
    if not user_id:
        return {"theme_idx": 0, "border_idx": 0}
    
    safe_user_id = str(user_id).replace('/', '_').replace('\\', '_')
    theme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'.ssh_theme_{safe_user_id}.json')
    try:
        if os.path.exists(theme_path):
            with open(theme_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error("Failed to load theme user_id=%s error=%s", user_id, e)
    return {"theme_idx": 0, "border_idx": 0}

def save_theme(user_id, theme_idx, border_idx):
    """
    Save the specified user's theme index and border index preferences to disk.

    Args:
        user_id (int or str): The user ID.
        theme_idx (int): Selected theme index.
        border_idx (int): Selected border color index.
    """
    if not user_id:
        return
    
    import tempfile
    safe_user_id = str(user_id).replace('/', '_').replace('\\', '_')
    dir_path = os.path.dirname(os.path.abspath(__file__))
    theme_path = os.path.join(dir_path, f'.ssh_theme_{safe_user_id}.json')
    
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=f'.ssh_theme_{safe_user_id}.',
            suffix='.tmp',
            dir=dir_path
        )
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({"theme_idx": theme_idx, "border_idx": border_idx}, f)
        os.replace(temp_path, theme_path)
    except Exception as e:
        logger.error("Failed to save theme user_id=%s error=%s", user_id, e)
        try:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

# ============================================================
# Session Handler (main per-connection logic)
# ============================================================
def handle_session(channel, server: StellarSSHServer, client_addr: str):
    """
    Main session handler for one SSH connection.
    Runs: Theme Picker → Splash/Auth → Dashboard → Container Shell/Logs → Goodbye
    """
    global active_sessions
    user_info = None

    try:
        # Wait for shell request
        if not server.event.wait(timeout=10):
            logger.warning("Client shell request timeout client_addr=%s", client_addr)
            return

        channel.settimeout(SESSION_IDLE_TIMEOUT)
        real_ip = server.username if (server.username and client_addr == '127.0.0.1') else client_addr
        
        # Rate limit check
        if real_ip != '127.0.0.1' and client_addr == '127.0.0.1':
            if rate_limiter.is_ip_blocked(real_ip):
                send_raw(channel, "\r\n\x1b[31m  ✗ Your IP is blocked due to too many failed attempts.\x1b[0m\r\n")
                time.sleep(2)
                return

        send_raw(channel, '\x1b[?1049h\x1b[?25l')  # Enter alt screen, hide cursor

        # Double-buffer / cache the rendered string and use line-by-line diffing
        # to eliminate terminal flickering and reduce network payload.
        last_drawn_content = None

        def draw(content: str, clear: bool = False):
            """
            Render the content string onto the SSH channel.
            Uses double-buffering and line-by-line diffing to prevent terminal flickering.

            Args:
                content (str): The rendered TUI screen content.
                clear (bool, optional): Force a full screen refresh. Defaults to False.
            """
            nonlocal last_drawn_content
            if not clear and content == last_drawn_content:
                return
            if clear or last_drawn_content is None:
                # Overwrite starting from home, then clear trailing lines to prevent flickering
                send_raw(channel, '\x1b[H' + content + '\x1b[J')
            else:
                new_lines = content.splitlines()
                old_lines = last_drawn_content.splitlines()
                # If number of lines changed, fallback to full draw with trailing clear
                if len(new_lines) != len(old_lines):
                    send_raw(channel, '\x1b[H' + content + '\x1b[J')
                else:
                    # Count how many lines changed
                    changed_indices = [idx for idx, (new_line, old_line) in enumerate(zip(new_lines, old_lines)) if new_line != old_line]
                    if len(changed_indices) > 3:
                        # Overwrite the whole screen from home without clearing to eliminate flickering (Bolt - Performance optimization)
                        send_raw(channel, '\x1b[H' + content)
                    else:
                        # Accumulate updates to avoid sending multiple small TCP packets, eliminating flicker
                        # (Bolt - Performance optimization: batching raw socket writes).
                        buffer = []
                        # Line-by-line diff: draw only lines that have changed to save bandwidth and prevent flickering
                        for idx in changed_indices:
                            # Move cursor to start of line (idx+1), print new content, clear rest of line
                            buffer.append(f'\x1b[{idx+1};1H' + new_lines[idx] + '\x1b[K')
                        if buffer:
                            send_raw(channel, ''.join(buffer))
            last_drawn_content = content

        # ---- PHASE 1: Authentication ----
        # Use default theme for authentication screen
        active_theme = TUI.THEMES[0].copy()
        
        auth_attempts = 0
        while auth_attempts < MAX_AUTH_ATTEMPTS:
            error_msg = ""
            if auth_attempts > 0:
                remaining = MAX_AUTH_ATTEMPTS - auth_attempts
                error_msg = f"Invalid code. {remaining} attempt{'s' if remaining > 1 else ''} remaining."

            code = ""
            first_draw = True
            while len(code) < 6:
                # Clear screen on first draw of auth screen to remove anything previous
                draw(TUI.auth_screen(server.term_width, server.term_height, typed_code=code, error_msg=error_msg, theme=active_theme), clear=first_draw)
                first_draw = False
                
                # Optimize: Reduce read_key timeout to 0.1s to handle terminal resizes instantly
                key = read_key(channel, timeout=0.1)
                if not key:
                    if server.resize_event.is_set():
                        server.resize_event.clear()
                        draw(TUI.auth_screen(server.term_width, server.term_height, typed_code=code, error_msg=error_msg, theme=active_theme), clear=True)
                    continue
                    
                if key == 'BACKSPACE':
                    code = code[:-1]
                elif key in ('CTRL_C', 'CTRL_D', 'EOF'):
                    send_raw(channel, '\x1b[?1049l\x1b[?25h')
                    return
                elif isinstance(key, str) and key not in ('UP', 'DOWN', 'LEFT', 'RIGHT', 'ENTER', 'ESC', 'BACKSPACE', 'CTRL_C', 'CTRL_D', 'EOF'):
                    for char in key.upper():
                        if char in (" ", "-"):
                            continue
                        if char.isalnum() and len(code) < 6:
                            code += char
                        if len(code) == 6:
                            break

            # Verify
            draw(TUI.auth_screen(server.term_width, server.term_height, typed_code=code, theme=active_theme))
            user_info = verify_auth_code(code)
            if user_info:
                break
            else:
                auth_attempts += 1
                rate_limiter.record_auth_failure(real_ip)
                audit.info(f"AUTH_FAIL | ip={real_ip} | attempt={auth_attempts} | code_prefix={code[:2] if code else '??'}****")
                logger.warning("SSH authentication failure ip=%s attempt=%d", real_ip, auth_attempts)

        if not user_info:
            # Clear screen when transitioning to goodbye screen
            draw(TUI.goodbye_screen("User", server.term_width, server.term_height, theme=active_theme), clear=True)
            audit.info(f"AUTH_LOCKOUT | ip={real_ip} | attempts={MAX_AUTH_ATTEMPTS}")
            logger.warning("SSH authentication lockout ip=%s attempts=%d", real_ip, MAX_AUTH_ATTEMPTS)
            time.sleep(2)
            send_raw(channel, '\x1b[?1049l\x1b[?25h')
            return

        user_id = user_info['user_id']
        username = user_info.get('display_name') or user_info.get('username', 'User')
        audit.info(f"SESSION_START | ip={real_ip} | user_id={user_id} | username={username}")
        logger.info("SSH session authenticated ip=%s user_id=%s username=%s", real_ip, user_id, username)

        # Load user-scoped theme preferences or use default (Theme 0)
        saved = load_theme(user_id)
        selected_theme = saved.get("theme_idx", 0)
        selected_border = saved.get("border_idx", 0)
        
        # Ensure values are within bounds
        if not (0 <= selected_theme < len(TUI.THEMES)):
            selected_theme = 0
        if not (0 <= selected_border < len(TUI.BORDER_COLORS)):
            selected_border = 0

        base_theme = TUI.THEMES[selected_theme].copy()
        b_val = TUI.BORDER_COLORS[selected_border]["value"]
        if b_val:
            base_theme["border"] = b_val
        active_theme = base_theme

        # ---- PHASE 2: Dashboard ----
        selected_index = 0
        status_msg = f"Welcome, {username}!"
        search_query = ""
        filter_states = ["All", "Running", "Stopped"]
        filter_idx = 0
        sort_states = ["Name", "Status", "Created"]
        sort_idx = 0
        mode = "NORMAL"
        
        last_activity = time.time()
        
        repos = []
        status_map = {}
        all_repos = []

        def get_filtered_sorted_repos(all_repos, status_map):
            """
            Filter and sort the list of repositories based on active search, filters, and sort options.

            Args:
                all_repos (list): Full list of user repository dicts.
                status_map (dict): Status mapping dictionary for container statuses.

            Returns:
                list: Filtered and sorted list of repositories.
            """
            f_repos = []
            for r in all_repos:
                r_status = status_map.get(r['process_id'], 'not_found')
                
                # Text Search
                if search_query and search_query.lower() not in r['name'].lower():
                    continue
                    
                # State Filter
                current_filter = filter_states[filter_idx]
                if current_filter == "Running" and r_status != "running":
                    continue
                # Bolt - Stability/Consistency fix: align filter logic with TUI dashboard display logic, treating non-running states as stopped.
                if current_filter == "Stopped" and r_status == "running":
                    continue
                    
                f_repos.append(r)
                
            # Sort
            current_sort = sort_states[sort_idx]
            if current_sort == "Name":
                f_repos.sort(key=lambda x: x['name'].lower())
            elif current_sort == "Status":
                f_repos.sort(key=lambda x: status_map.get(x['process_id'], 'not_found'))
            elif current_sort == "Created":
                f_repos.sort(key=lambda x: x.get('created', ''), reverse=True)
                
            return f_repos

        def redraw(clear: bool = False):
            """
            Redraw the primary TUI dashboard.

            Args:
                clear (bool): True to force a full clear and redraw of the terminal screen.
            """
            nonlocal status_msg
            draw(TUI.dashboard(
                repos, selected_index, username, 
                server.term_width, server.term_height, 
                status_msg, theme=active_theme, 
                search_query=search_query, 
                filter_state=filter_states[filter_idx], 
                sort_state=sort_states[sort_idx], 
                mode=mode, status_map=status_map
            ), clear=clear)
            status_msg = ""

        needs_refresh = True
        last_refresh_time = 0
        REFRESH_INTERVAL = 3.0 # seconds
        first_dashboard_draw = True

        while True:
            # Optimize: Handle terminal resize events instantly at the start of the loop
            if server.resize_event.is_set():
                server.resize_event.clear()
                redraw(clear=True)

            # Check idle timeout
            if time.time() - last_activity > SESSION_IDLE_TIMEOUT:
                # Clear screen when entering goodbye screen
                draw(TUI.goodbye_screen(username, server.term_width, server.term_height, theme=active_theme), clear=True)
                audit.info(f"SESSION_TIMEOUT | user_id={user_id} | idle_minutes={SESSION_IDLE_TIMEOUT // 60}")
                time.sleep(2)
                break

            # Refresh data if cooldown has expired or explicitly requested
            if needs_refresh or (time.time() - last_refresh_time > REFRESH_INTERVAL):
                try:
                    all_repos = get_user_repos(user_id)
                    
                    # Batch query all container statuses from Docker daemon using cache to reduce overhead
                    container_statuses = get_all_container_statuses()
                    
                    status_map = {}
                    for r in all_repos:
                        pid = r['process_id']
                        app_type = r.get('app_type', 'repo')
                        # Check specific and fallback container names
                        name1 = f"stellar-{app_type}-{pid}"
                        name2 = f"stellar-repo-{pid}"
                        if name1 in container_statuses:
                            status_map[pid] = container_statuses[name1]
                        elif name2 in container_statuses:
                            status_map[pid] = container_statuses[name2]
                        else:
                            status_map[pid] = 'not_found'
                    
                    repos = get_filtered_sorted_repos(all_repos, status_map)
                except Exception as e:
                    logger.error("Error querying deployments error=%s", e)
                needs_refresh = False
                last_refresh_time = time.time()
                if selected_index >= len(repos):
                    selected_index = max(0, len(repos) - 1)
                redraw(clear=first_dashboard_draw)
                first_dashboard_draw = False

            # Read keypress
            key = read_key(channel, timeout=0.1)
            if not key:
                continue

            last_activity = time.time()

            if mode == "SEARCH":
                if key == 'ESC':
                    mode = "NORMAL"
                    redraw()
                elif key == 'BACKSPACE':
                    search_query = search_query[:-1]
                    repos = get_filtered_sorted_repos(all_repos, status_map)
                    selected_index = 0
                    redraw()
                elif key in ('ENTER', '\n', '\r'):
                    mode = "NORMAL"
                    redraw()
                # Allow strings of any length to fully support pasted queries and rapid keypresses
                elif isinstance(key, str) and len(key) >= 1 and key.isprintable():
                    search_query += key
                    repos = get_filtered_sorted_repos(all_repos, status_map)
                    selected_index = 0
                    redraw()
                continue

            # Normal Mode Controls
            if key == 'UP':
                if repos and selected_index > 0:
                    selected_index -= 1
                    redraw()
            elif key == 'DOWN':
                if repos and selected_index < len(repos) - 1:
                    selected_index += 1
                    redraw()
            elif key in ('q', 'Q', 'CTRL_C', 'CTRL_D', 'EOF'):
                break
            elif key == '/':
                mode = "SEARCH"
                redraw()
            elif key in ('f', 'F'):
                filter_idx = (filter_idx + 1) % len(filter_states)
                repos = get_filtered_sorted_repos(all_repos, status_map)
                selected_index = 0
                redraw()
            elif key in ('o', 'O'):
                sort_idx = (sort_idx + 1) % len(sort_states)
                repos = get_filtered_sorted_repos(all_repos, status_map)
                selected_index = 0
                redraw()
            elif key in ('t', 'T'):
                # Run the interactive theme picker inside the session
                t_sel_theme = selected_theme
                t_sel_border = selected_border
                t_is_default = False
                t_focus = "theme"
                
                # Clear screen when entering theme picker
                draw(TUI.theme_picker(t_sel_theme, t_sel_border, t_focus, server.term_width, server.term_height, t_is_default), clear=True)
                
                while True:
                    # Optimize: Reduce read_key timeout to 0.1s to handle terminal resizes instantly
                    t_key = read_key(channel, timeout=0.1)
                    if not t_key:
                        if server.resize_event.is_set():
                            server.resize_event.clear()
                            draw(TUI.theme_picker(t_sel_theme, t_sel_border, t_focus, server.term_width, server.term_height, t_is_default), clear=True)
                        continue
                    
                    t_needs_redraw = True
                    if t_key == "RIGHT" and t_focus == "theme":
                        t_focus = "border"
                    elif t_key == "LEFT" and t_focus == "border":
                        t_focus = "theme"
                    elif t_key == "UP":
                        if t_focus == "theme" and t_sel_theme > 0: t_sel_theme -= 1
                        elif t_focus == "border" and t_sel_border > 0: t_sel_border -= 1
                    elif t_key == "DOWN":
                        if t_focus == "theme" and t_sel_theme < len(TUI.THEMES) - 1: t_sel_theme += 1
                        elif t_focus == "border" and t_sel_border < len(TUI.BORDER_COLORS) - 1: t_sel_border += 1
                    elif t_key == " ":
                        t_is_default = not t_is_default
                    elif t_key == "ENTER":
                        selected_theme = t_sel_theme
                        selected_border = t_sel_border
                        if t_is_default:
                            save_theme(user_id, selected_theme, selected_border)
                        break
                    elif t_key == "ESC":
                        break
                    elif t_key in ('CTRL_C', 'CTRL_D', 'EOF'):
                        break
                    else:
                        t_needs_redraw = False
                        
                    if t_needs_redraw:
                        draw(TUI.theme_picker(t_sel_theme, t_sel_border, t_focus, server.term_width, server.term_height, t_is_default))
                
                base_theme = TUI.THEMES[selected_theme].copy()
                b_val = TUI.BORDER_COLORS[selected_border]["value"]
                if b_val: base_theme["border"] = b_val
                active_theme = base_theme
                # Clear screen when returning to dashboard
                redraw(clear=True)
            elif key == 'ENTER':
                if repos:
                    repo = repos[selected_index]
                    app_type = repo.get('app_type', 'repo')
                    live_status = get_container_status(repo['process_id'], app_type)
                    if live_status == 'running':
                        draw(TUI.connecting_screen(repo['name'], server.term_width, server.term_height, theme=active_theme), clear=True)
                        time.sleep(0.5)
                        send_raw(channel, '\x1b[?1049l\x1b[?25h') # Exit alt screen for shell
                        attach_container_shell(channel, server, repo['process_id'], app_type, user_id)
                        send_raw(channel, '\x1b[?1049h\x1b[?25l') # Re-enter alt screen
                        status_msg = f"Disconnected from {repo['name']}"
                        # Force full clear draw when returning from container shell
                        first_dashboard_draw = True
                        needs_refresh = True
                    elif live_status in ('exited', 'created'):
                        status_msg = start_container(repo['process_id'], app_type, user_id)
                        needs_refresh = True
                    else:
                        status_msg = f"Cannot connect: container is {live_status}"
                        redraw()
            elif key in ('l', 'L'):
                if repos:
                    repo = repos[selected_index]
                    app_type = repo.get('app_type', 'repo')
                    try:
                        # Re-use global docker client to avoid expensive initialization overhead
                        client = get_docker_client()
                        container = get_container(client, repo['process_id'], app_type)
                        logs_raw = container.logs(tail=50, timestamps=True).decode('utf-8').splitlines()
                        
                        # Draw the logs screen once and clear screen
                        draw(TUI.logs_screen(repo['name'], logs_raw, server.term_width, server.term_height, theme=active_theme), clear=True)
                        
                        last_log_refresh_time = time.time()
                        while True:
                            # Optimize: Reduce read_key timeout to 0.1s to handle terminal resizes and user exits instantly
                            log_key = read_key(channel, timeout=0.1)
                            if log_key in ('q', 'Q', 'ESC', 'CTRL_C'):
                                break
                            
                            # Live log refreshes: triggered on timeout or resize, and redraws only if content changes
                            # Optimize: rate-limit log fetches to avoid high CPU and Docker socket overhead
                            now = time.time()
                            is_due = (now - last_log_refresh_time >= 2.0)
                            if (log_key is None and is_due) or server.resize_event.is_set():
                                clear_screen = False
                                if server.resize_event.is_set():
                                    server.resize_event.clear()
                                    clear_screen = True
                                
                                try:
                                    new_logs = container.logs(tail=50, timestamps=True).decode('utf-8').splitlines()
                                    if new_logs != logs_raw or clear_screen:
                                        logs_raw = new_logs
                                        draw(TUI.logs_screen(repo['name'], logs_raw, server.term_width, server.term_height, theme=active_theme), clear=clear_screen)
                                    last_log_refresh_time = now
                                except Exception as e:
                                    logger.error("Failed to fetch container logs container_name=%s error=%s", repo['name'], e, exc_info=True)
                    except Exception as e:
                        status_msg = f"Could not fetch logs: {e}"
                        logger.error("Error in logs viewer loop container_name=%s error=%s", repo['name'], e, exc_info=True)
                    # Force full clear draw when returning to dashboard
                    first_dashboard_draw = True
                    redraw()
            elif key in ('r', 'R'):
                if repos:
                    repo = repos[selected_index]
                    status_msg = f"Restarting {repo['name']}..."
                    redraw()
                    status_msg = restart_container(repo['process_id'], repo.get('app_type', 'repo'), user_id)
                    needs_refresh = True
            elif key in ('s', 'S'):
                if repos:
                    repo = repos[selected_index]
                    status_msg = stop_container(repo['process_id'], repo.get('app_type', 'repo'), user_id)
                    needs_refresh = True

        # ---- PHASE 3: Goodbye ----
        draw(TUI.goodbye_screen(username, server.term_width, server.term_height, theme=active_theme), clear=True)
        audit.info(f"SESSION_END | user_id={user_id} | username={username}")
        time.sleep(1)

    except socket.timeout:
        audit.info(f"SESSION_SOCKET_TIMEOUT | ip={client_addr}")
    except Exception as e:
        logger.error("Session error client_addr=%s error=%s", client_addr, e, exc_info=True)
    finally:
        send_raw(channel, '\x1b[?1049l\x1b[?25h') # Ensure exit alt screen
        try:
            channel.close()
        except Exception:
            pass


# ============================================================
# Connection Handler
# ============================================================
def handle_connection(client_socket, client_addr):
    """
    Handle a newly accepted TCP connection, performing rate limiting and setting up Paramiko SSH transport.

    Args:
        client_socket (socket.socket): The TCP socket of the client connection.
        client_addr (tuple): The client IP address and port tuple.
    """
    global active_sessions
    session_active = False  # Initialize early to prevent UnboundLocalError in finally block (Bolt - Stability fix)
    _thread_local.session_id = f"{client_addr[0]}:{client_addr[1]}"
    ip = client_addr[0]

    # Rate limit check
    if ip != '127.0.0.1':
        if rate_limiter.is_ip_blocked(ip):
            audit.info(f"CONNECTION_BLOCKED | ip={ip} | reason=auth_failures")
            logger.warning("Connection blocked ip=%s reason=auth_failures", ip)
            client_socket.close()
            return

        if not rate_limiter.check_connection_rate(ip):
            audit.info(f"CONNECTION_BLOCKED | ip={ip} | reason=rate_limit")
            logger.warning("Connection blocked ip=%s reason=rate_limit", ip)
            client_socket.close()
            return

    with sessions_lock:
        if active_sessions >= MAX_CONCURRENT_SESSIONS:
            logger.warning("Max concurrent sessions reached concurrent_limit=%s ip=%s", MAX_CONCURRENT_SESSIONS, ip)
            client_socket.close()
            return
        active_sessions += 1

    session_active = True
    audit.info(f"CONNECTION_NEW | ip={ip} | active_sessions={active_sessions}")
    logger.info("New SSH connection established ip=%s active_sessions=%d", ip, active_sessions)

    transport = None
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
            logger.error("SSH negotiation failed ip=%s error=%s", ip, e)
            return

        # Wait for a channel
        channel = transport.accept(timeout=20)
        if channel is None:
            logger.warning("No channel opened by ip=%s", ip)
            return

        # Handle the session
        handle_session(channel, server, ip)

    except Exception as e:
        logger.error("Connection handler error ip=%s error=%s", ip, e, exc_info=True)
    finally:
        if transport:
            try:
                transport.close()
            except Exception:
                pass
        # Always decrement session count on connection teardown to prevent socket leak DoS
        if session_active:
            with sessions_lock:
                active_sessions -= 1
            logger.info("Session closed for ip=%s", ip)


# ============================================================
# Host Key Generation
# ============================================================
def ensure_host_key():
    """
    Generate Ed25519 host key if it doesn't exist.
    Saves the key to the configured HOST_KEY_PATH and sets secure file permissions.
    """
    if os.path.exists(HOST_KEY_PATH):
        key = paramiko.Ed25519Key(filename=HOST_KEY_PATH)
        logger.info("Using existing host key path=%s fingerprint=%s", HOST_KEY_PATH, key.get_fingerprint().hex())
        return

    logger.info("Generating new Ed25519 host key path=%s", HOST_KEY_PATH)
    import subprocess
    result = subprocess.run(
        ['ssh-keygen', '-t', 'ed25519', '-f', HOST_KEY_PATH, '-N', '', '-q'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.critical("Failed to generate host key error=%s", result.stderr)
        sys.exit(1)
    os.chmod(HOST_KEY_PATH, 0o600)
    key = paramiko.Ed25519Key(filename=HOST_KEY_PATH)
    logger.info("Host key generated fingerprint=%s", key.get_fingerprint().hex())


# ============================================================
# Main Server
# ============================================================
def main():
    """
    Start the SSH gateway server daemon.
    Generates host keys, registers signal handlers for graceful shutdown, pre-warms the Docker status
    cache, binds to the configured host and port, and loops to accept incoming TCP connections
    delegated to session threads.
    """
    logger.info("=== Stellar SSH Gateway starting on %s:%d ===", SSH_HOST, SSH_PORT)

    # Generate host key if needed
    ensure_host_key()

    # Pre-populate status cache and start refresher thread immediately to prevent login lag
    # (Bolt - Performance optimization: pre-warm cache).
    start_refresher_thread_if_needed()

    # Create server socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind((SSH_HOST, SSH_PORT))
    except OSError as e:
        logger.critical("FATAL: Cannot bind to %s:%d error=%s", SSH_HOST, SSH_PORT, e)
        sys.exit(1)

    server_socket.listen(10)
    server_socket.settimeout(1.0)  # For graceful shutdown

    logger.info("Listening for SSH connections on port port=%d", SSH_PORT)

    # Graceful shutdown
    shutdown_event = threading.Event()

    def signal_handler(signum, frame):
        """
        Handle shutdown signals (SIGINT, SIGTERM) by setting the shutdown event
        to stop the server gracefully.

        Args:
            signum (int): The signal number.
            frame (frame): The current stack frame.
        """
        logger.info("Shutdown signal received, shutting down SSH gateway...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while not shutdown_event.is_set():
            try:
                client_socket, client_addr = server_socket.accept()
                logger.info("New connection from ip=%s port=%d", client_addr[0], client_addr[1])

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
        logger.critical("Server error error=%s", e, exc_info=True)
    finally:
        server_socket.close()
        logger.info("SSH Gateway stopped.")


if __name__ == '__main__':
    main()
