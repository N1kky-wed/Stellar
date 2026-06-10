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

import redis
import docker
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


_docker_client = None
_docker_client_lock = threading.Lock()

def get_docker_client():
    """Get a thread-safe shared Docker client instance."""
    global _docker_client
    if _docker_client is None:
        with _docker_client_lock:
            if _docker_client is None:
                _docker_client = docker.from_env()
    return _docker_client


def get_container_status(process_id: str, app_type: str = 'repo') -> str:
    """Get live Docker container status."""
    try:
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
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
from rich.columns import Columns
from rich.console import Group

class TUI:
    """Renders beautiful terminal UI screens via Rich."""

    THEMES = [
        {
            "name": "Stellar Classic",
            "bg": "#0E0E0E",
            "border": "#444444",
            "primary": "#E38B68",
            "accent": "#6ECFFF",
            "text": "white",
            "dim": "#888888"
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
            "name": "Monochrome",
            "bg": "black",
            "border": "white",
            "primary": "white",
            "accent": "grey74",
            "text": "white",
            "dim": "grey50"
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
        return f"[bold {theme['primary']}]Stellar[/bold {theme['primary']}] [{theme['dim']}]Code[/{theme['dim']}]"

    @staticmethod
    def get_big_logo(theme: dict) -> str:
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
        """Wrap content in a themed box and render to string, filling the screen."""
        from rich.align import Align
        buf = StringIO()
        console = Console(
            file=buf,
            width=width,
            force_terminal=True,
            color_system="256",
            highlight=False,
            legacy_windows=False
        )
        
        # Wrap everything in the master "app box"
        panel = Panel(
            content,
            style=f"{theme['text']} on {theme['bg']}",
            border_style=theme['border'],
            box=box.DOUBLE,
            padding=(1, 2),
            expand=True,
            height=height
        )
        console.print(panel)
        return buf.getvalue()

    @staticmethod
    def theme_picker(selected_theme: int, selected_border: int, focus: str, width: int, height: int, is_default: bool = False) -> str:
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
            content.append(f"\n\n[bold red]![/bold red] {error_msg}")
            
        from rich.align import Align
        return TUI._render(width, height, Align(content, vertical="middle", align="center"), theme)

    @staticmethod
    def dashboard(repos: list, selected: int, username: str, width: int, height: int, status_msg: str = "", theme: dict = None, search_query: str = "", filter_state: str = "All", sort_state: str = "Name", mode: str = "NORMAL", status_map: dict = None) -> str:
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
            table.add_column("Type", style=theme['dim'])
            table.add_column("Created", style=theme['dim'])

            for i, repo in enumerate(repos):
                is_sel = (i == selected)
                marker = f"[bold {theme['accent']}]▸[/bold {theme['accent']}]" if is_sel else " "
                
                status_raw = status_map.get(repo['process_id']) if status_map else None
                if not status_raw:
                    status_raw = get_container_status(repo['process_id'], repo.get('app_type', 'repo'))
                if status_raw == 'running':
                    status_icon_sel = "[bold green]●[/bold green]"
                    status_icon_dim = "[dim green]●[/dim green]"
                elif status_raw == 'exited':
                    status_icon_sel = "[bold red]○[/bold red]"
                    status_icon_dim = "[dim red]○[/dim red]"
                else:
                    status_icon_sel = "[bold yellow]◌[/bold yellow]"
                    status_icon_dim = "[dim yellow]◌[/dim yellow]"
                
                if is_sel:
                    status_disp = f"{status_icon_sel} [{theme['text']}]{status_raw}[/{theme['text']}]"
                else:
                    status_disp = f"[{theme['dim']}]{status_icon_dim} {status_raw}[/{theme['dim']}]"
                
                name_style = f"bold {theme['text']}" if is_sel else theme['text']
                name_disp = f"[{name_style}]{repo['name']}[/{name_style}]"
                
                sub_style = theme['text'] if is_sel else theme['dim']
                sub_disp = f"[{sub_style}]{repo['subdomain']}[/{sub_style}]"
                
                type_disp = f"[{sub_style}]{repo.get('app_type', 'repo')}[/{sub_style}]"
                
                created = repo.get('created', '-')
                if len(created) > 10: created = created[:10]
                created_disp = f"[{sub_style}]{created}[/{sub_style}]"

                table.add_row(marker, name_disp, status_disp, sub_disp, type_disp, created_disp)
                # Add gap row if not the last item
                if i < len(repos) - 1:
                    table.add_row("", "", "", "", "", "")

            from rich.align import Align
            table_or_empty = Align.center(table)

        nav_footer = Table.grid(expand=True)
        nav_footer.add_column(justify="left")
        nav_footer.add_column(justify="right")

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
        if not theme: theme = TUI.THEMES[0]
        content = Text.from_markup(f"\n\n[{theme['dim']}]Connecting to[/{theme['dim']}] [bold {theme['text']}]{repo_name}[/bold {theme['text']}]\n\n", justify="center")
        from rich.align import Align
        return TUI._render(width, height, Align(content, vertical="middle", align="center"), theme)

    @staticmethod
    def logs_screen(repo_name: str, logs: list, width: int, height: int, theme: dict = None) -> str:
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
        if not theme: theme = TUI.THEMES[0]
        content = Text.from_markup(f"\n\n[{theme['dim']}]Goodbye,[/{theme['dim']}] [bold {theme['text']}]{username}[/bold {theme['text']}][{theme['dim']}]. Session terminated.[/{theme['dim']}]\n\n", justify="center")
        from rich.align import Align
        return TUI._render(width, height, Align(content, vertical="middle", align="center"), theme)


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
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
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
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
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
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"CONTAINER_RESTART | user_id={user_id} | container={container_name}")
        start_time = time.time()
        container.restart(timeout=10)
        duration = time.time() - start_time
        logger.info(f"Container restart complete for {container_name} in {duration:.2f}s")
        audit.info(f"CONTAINER_RESTART_SUCCESS | user_id={user_id} | container={container_name} | duration_sec={duration:.2f}")
        return f"✓ {container_name} restarted successfully"
    except docker.errors.NotFound:
        return "✗ Container not found"
    except Exception as e:
        logger.error(f"Container restart failed for process_id={process_id}: {e}", exc_info=True)
        return f"✗ Restart failed: {str(e)[:60]}"


def stop_container(process_id: str, app_type: str, user_id: int) -> str:
    """Stop a Docker container."""
    try:
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"CONTAINER_STOP | user_id={user_id} | container={container_name}")
        start_time = time.time()
        container.stop(timeout=10)
        duration = time.time() - start_time
        logger.info(f"Container stop complete for {container_name} in {duration:.2f}s")
        audit.info(f"CONTAINER_STOP_SUCCESS | user_id={user_id} | container={container_name} | duration_sec={duration:.2f}")
        return f"✓ {container_name} stopped"
    except docker.errors.NotFound:
        return "✗ Container not found"
    except Exception as e:
        logger.error(f"Container stop failed for process_id={process_id}: {e}", exc_info=True)
        return f"✗ Stop failed: {str(e)[:60]}"


def start_container(process_id: str, app_type: str, user_id: int) -> str:
    """Start a stopped Docker container."""
    try:
        # Re-use global docker client to avoid expensive initialization overhead
        client = get_docker_client()
        container = get_container(client, process_id, app_type)
        container_name = container.name
        audit.info(f"CONTAINER_START | user_id={user_id} | container={container_name}")
        start_time = time.time()
        container.start()
        duration = time.time() - start_time
        logger.info(f"Container start complete for {container_name} in {duration:.2f}s")
        audit.info(f"CONTAINER_START_SUCCESS | user_id={user_id} | container={container_name} | duration_sec={duration:.2f}")
        return f"✓ {container_name} started"
    except docker.errors.NotFound:
        return "✗ Container not found"
    except Exception as e:
        logger.error(f"Container start failed for process_id={process_id}: {e}", exc_info=True)
        return f"✗ Start failed: {str(e)[:60]}"


# ============================================================
# Theme Persistence
# ============================================================
def load_theme(user_id=None):
    if not user_id:
        return {"theme_idx": 0, "border_idx": 0}
    
    safe_user_id = str(user_id).replace('/', '_').replace('\\', '_')
    theme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'.ssh_theme_{safe_user_id}.json')
    try:
        if os.path.exists(theme_path):
            with open(theme_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load theme for user {user_id}: {e}")
    return {"theme_idx": 0, "border_idx": 0}

def save_theme(user_id, theme_idx, border_idx):
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
        logger.error(f"Failed to save theme for user {user_id}: {e}")
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
            logger.warning(f"Client {client_addr} didn't request shell in time")
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

        def draw(content: str):
            send_raw(channel, '\x1b[H\x1b[0J' + content)

        # ---- PHASE 0: Theme Picker ----
        saved = load_theme()
        selected_theme = saved.get("theme_idx", 0)
        selected_border = saved.get("border_idx", 0)
        is_default = False
        focus = "theme"
        theme_picked = False
        
        draw(TUI.theme_picker(selected_theme, selected_border, focus, server.term_width, server.term_height, is_default))
        
        while True:
            key = read_key(channel, timeout=5.0)
            if not key:
                if server.resize_event.is_set():
                    server.resize_event.clear()
                    draw(TUI.theme_picker(selected_theme, selected_border, focus, server.term_width, server.term_height, is_default))
                continue
            
            needs_redraw = True
            if key == "RIGHT" and focus == "theme":
                focus = "border"
            elif key == "LEFT" and focus == "border":
                focus = "theme"
            elif key == "UP":
                if focus == "theme" and selected_theme > 0: selected_theme -= 1
                elif focus == "border" and selected_border > 0: selected_border -= 1
            elif key == "DOWN":
                if focus == "theme" and selected_theme < len(TUI.THEMES) - 1: selected_theme += 1
                elif focus == "border" and selected_border < len(TUI.BORDER_COLORS) - 1: selected_border += 1
            elif key == " ": # Spacebar toggle
                is_default = not is_default
            elif key == "ENTER":
                theme_picked = True
                break
            elif key == "ESC":
                selected_theme, selected_border = 0, 0
                theme_picked = False
                break
            elif key in ('CTRL_C', 'CTRL_D', 'EOF'):
                send_raw(channel, '\x1b[?1049l\x1b[?25h')
                return
            else:
                needs_redraw = False
                
            if needs_redraw:
                draw(TUI.theme_picker(selected_theme, selected_border, focus, server.term_width, server.term_height, is_default))
        
        base_theme = TUI.THEMES[selected_theme].copy()
        b_val = TUI.BORDER_COLORS[selected_border]["value"]
        if b_val: base_theme["border"] = b_val
        active_theme = base_theme

        # ---- PHASE 1: Authentication ----
        auth_attempts = 0
        while auth_attempts < MAX_AUTH_ATTEMPTS:
            error_msg = ""
            if auth_attempts > 0:
                remaining = MAX_AUTH_ATTEMPTS - auth_attempts
                error_msg = f"Invalid code. {remaining} attempt{'s' if remaining > 1 else ''} remaining."

            code = ""
            while len(code) < 6:
                draw(TUI.auth_screen(server.term_width, server.term_height, typed_code=code, error_msg=error_msg, theme=active_theme))
                
                key = read_key(channel, timeout=5.0)
                if not key:
                    if server.resize_event.is_set():
                        server.resize_event.clear()
                        draw(TUI.auth_screen(server.term_width, server.term_height, typed_code=code, error_msg=error_msg, theme=active_theme))
                    continue
                    
                if key == 'BACKSPACE':
                    code = code[:-1]
                elif key in ('CTRL_C', 'CTRL_D', 'EOF'):
                    send_raw(channel, '\x1b[?1049l\x1b[?25h')
                    return
                elif isinstance(key, str):
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

        if not user_info:
            draw(TUI.goodbye_screen("User", server.term_width, server.term_height, theme=active_theme))
            audit.info(f"AUTH_LOCKOUT | ip={real_ip} | attempts={MAX_AUTH_ATTEMPTS}")
            time.sleep(2)
            send_raw(channel, '\x1b[?1049l\x1b[?25h')
            return

        user_id = user_info['user_id']
        username = user_info.get('display_name') or user_info.get('username', 'User')
        audit.info(f"SESSION_START | ip={real_ip} | user_id={user_id} | username={username}")

        # Load or save user-scoped theme preferences
        if is_default:
            save_theme(user_id, selected_theme, selected_border)
        elif not theme_picked:
            saved = load_theme(user_id)
            selected_theme = saved.get("theme_idx", 0)
            selected_border = saved.get("border_idx", 0)
            
        base_theme = TUI.THEMES[selected_theme].copy()
        b_val = TUI.BORDER_COLORS[selected_border]["value"]
        if b_val: base_theme["border"] = b_val
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
                if current_filter == "Stopped" and r_status != "exited":
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

        def redraw():
            nonlocal status_msg
            draw(TUI.dashboard(
                repos, selected_index, username, 
                server.term_width, server.term_height, 
                status_msg, theme=active_theme, 
                search_query=search_query, 
                filter_state=filter_states[filter_idx], 
                sort_state=sort_states[sort_idx], 
                mode=mode, status_map=status_map
            ))
            status_msg = ""

        needs_refresh = True
        last_refresh_time = 0
        REFRESH_INTERVAL = 3.0 # seconds

        while True:
            # Check idle timeout
            if time.time() - last_activity > SESSION_IDLE_TIMEOUT:
                draw(TUI.goodbye_screen(username, server.term_width, server.term_height, theme=active_theme))
                audit.info(f"SESSION_TIMEOUT | user_id={user_id} | idle_minutes={SESSION_IDLE_TIMEOUT // 60}")
                time.sleep(2)
                break

            # Refresh data if cooldown has expired or explicitly requested
            if needs_refresh or (time.time() - last_refresh_time > REFRESH_INTERVAL):
                try:
                    all_repos = get_user_repos(user_id)
                    
                    # Batch query all container statuses from Docker daemon to reduce overhead
                    try:
                        client = get_docker_client()
                        containers = client.containers.list(all=True)
                        container_statuses = {c.name: c.status for c in containers}
                    except Exception as docker_err:
                        logger.error(f"Failed to list containers: {docker_err}")
                        container_statuses = {}
                    
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
                    logger.error(f"Error querying deployments: {e}")
                needs_refresh = False
                last_refresh_time = time.time()
                if selected_index >= len(repos):
                    selected_index = max(0, len(repos) - 1)
                redraw()

            # Read keypress
            key = read_key(channel, timeout=0.1)
            if not key:
                if server.resize_event.is_set():
                    server.resize_event.clear()
                    redraw()
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
                elif isinstance(key, str) and len(key) == 1 and key.isprintable():
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
                
                draw(TUI.theme_picker(t_sel_theme, t_sel_border, t_focus, server.term_width, server.term_height, t_is_default))
                
                while True:
                    t_key = read_key(channel, timeout=5.0)
                    if not t_key:
                        if server.resize_event.is_set():
                            server.resize_event.clear()
                            draw(TUI.theme_picker(t_sel_theme, t_sel_border, t_focus, server.term_width, server.term_height, t_is_default))
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
                redraw()
            elif key == 'ENTER':
                if repos:
                    repo = repos[selected_index]
                    app_type = repo.get('app_type', 'repo')
                    live_status = get_container_status(repo['process_id'], app_type)
                    if live_status == 'running':
                        draw(TUI.connecting_screen(repo['name'], server.term_width, server.term_height, theme=active_theme))
                        time.sleep(0.5)
                        send_raw(channel, '\x1b[?1049l\x1b[?25h') # Exit alt screen for shell
                        attach_container_shell(channel, server, repo['process_id'], app_type, user_id)
                        send_raw(channel, '\x1b[?1049h\x1b[?25l') # Re-enter alt screen
                        status_msg = f"Disconnected from {repo['name']}"
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
                        
                        while True:
                            draw(TUI.logs_screen(repo['name'], logs_raw, server.term_width, server.term_height, theme=active_theme))
                            log_key = read_key(channel, timeout=0.1)
                            if log_key in ('q', 'Q', 'ESC', 'CTRL_C'):
                                break
                            if server.resize_event.is_set():
                                server.resize_event.clear()
                    except Exception as e:
                        status_msg = f"Could not fetch logs: {e}"
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
        draw(TUI.goodbye_screen(username, server.term_width, server.term_height, theme=active_theme))
        audit.info(f"SESSION_END | user_id={user_id} | username={username}")
        time.sleep(1)

    except socket.timeout:
        audit.info(f"SESSION_SOCKET_TIMEOUT | ip={client_addr}")
    except Exception as e:
        logger.error(f"Session error for {client_addr}: {e}", exc_info=True)
    finally:
        send_raw(channel, '\x1b[?1049l\x1b[?25h') # Ensure exit alt screen
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
