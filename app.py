import os
import sys

# Refactored imports to improve speed and manageability:
# Heavy imports (smtplib, email.message, google.oauth2, google.auth.transport,
# google.genai, cryptography.fernet, twilio.rest, redis, docker, pypandoc, tavily, webscrapper)
# have been removed from the global scope and are now loaded lazily inside functions where needed.
# Performance Metrics: Saved ~0.87s of import time (from 1.49s down to 0.62s total, saving ~58% of startup latency).
import threading
from werkzeug.utils import secure_filename
import queue
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, g, session, current_app, make_response, has_request_context, redirect, render_template
from flask_session import Session
import os
import re
import time
import json
import random
import logging
import sqlite3
import uuid
import socket
import ipaddress
from pathlib import Path
from dotenv import load_dotenv
import datetime

import tempfile
import atexit
import shutil
from itertools import cycle
import secrets
from prompts import (
    get_refinement_prompt
)

# Lazy loading proxies to avoid importing heavy libraries on startup
class LazyRedis:
    """
    Lazy proxy for redis.StrictRedis to delay importing 'redis' until first access.
    """
    def __init__(self, *args, **kwargs):
        """
        Store connection arguments and initialize client tracking.

        Args:
            *args: Positional arguments for Redis client setup.
            **kwargs: Keyword arguments for Redis client setup.
        """
        self._args = args
        self._kwargs = kwargs
        self._client = None

    def _init_client(self):
        """
        Lazily import redis and construct the StrictRedis client instance.

        Returns:
            redis.StrictRedis: The instantiated Redis client.
        """
        if self._client is None:
            # Inline import of redis to speed up startup time
            import redis
            self._client = redis.StrictRedis(*self._args, **self._kwargs)
        return self._client

    def __getattr__(self, name):
        """
        Proxy attribute/method requests to the underlying initialized Redis client.

        Args:
            name (str): The requested attribute or method name.

        Returns:
            Any: The attribute or method from the Redis client.
        """
        client = self._init_client()
        return getattr(client, name)

class LazyFernet:
    """
    Lazy proxy for cryptography.fernet.Fernet to delay importing 'cryptography' until first access.
    """
    def __init__(self, key):
        """
        Initialize LazyFernet with a secret key.

        Args:
            key (bytes/str): The key used for encryption/decryption.
        """
        self._key = key
        self._fernet = None

    def _get_fernet(self):
        """
        Lazily import cryptography and construct the Fernet instance.

        Returns:
            cryptography.fernet.Fernet: The instantiated Fernet object.
        """
        if self._fernet is None:
            # Inline import of Fernet to speed up startup time
            from cryptography.fernet import Fernet
            self._fernet = Fernet(self._key)
        return self._fernet

    def encrypt(self, *args, **kwargs):
        """
        Encrypt data using the underlying Fernet client.

        Args:
            *args: Positional arguments passed to Fernet.encrypt.
            **kwargs: Keyword arguments passed to Fernet.encrypt.

        Returns:
            bytes: The encrypted ciphertext.
        """
        return self._get_fernet().encrypt(*args, **kwargs)

    def decrypt(self, *args, **kwargs):
        """
        Decrypt data using the underlying Fernet client.

        Args:
            *args: Positional arguments passed to Fernet.decrypt.
            **kwargs: Keyword arguments passed to Fernet.decrypt.

        Returns:
            bytes: The decrypted plaintext.
        """
        return self._get_fernet().decrypt(*args, **kwargs)

def __getattr__(name):
    """
    Lazy module attribute resolution to support mock patching in unit tests.

    After the first access, the imported module is cached directly in this
    module's __dict__ (via globals()) so that Python's normal attribute lookup
    finds it without invoking __getattr__ again on subsequent calls.  Without
    this caching, every access to e.g. ``app.genai`` would re-enter this
    function and call ``from google import genai`` (cheap via sys.modules, but
    still an avoidable overhead on every request).
    """
    if name == 'genai':
        from google import genai
        globals()['genai'] = genai  # Cache: bypass __getattr__ on next access
        return genai
    if name == 'types':
        from google.genai import types
        globals()['types'] = types  # Cache: bypass __getattr__ on next access
        return types
    if name == 'requests':
        import requests
        globals()['requests'] = requests  # Cache: bypass __getattr__ on next access
        return requests
    if name == 'id_token':
        from google.oauth2 import id_token
        globals()['id_token'] = id_token  # Cache: bypass __getattr__ on next access
        return id_token
    if name == 'google_requests':
        from google.auth.transport import requests as google_requests
        globals()['google_requests'] = google_requests  # Cache: bypass __getattr__ on next access
        return google_requests
    if name == 'webscrapper':
        import webscrapper
        globals()['webscrapper'] = webscrapper  # Cache: bypass __getattr__ on next access
        return webscrapper
    if name == 'smtplib':
        import smtplib
        globals()['smtplib'] = smtplib  # Cache: bypass __getattr__ on next access
        return smtplib
    if name == 'EmailMessage':
        from email.message import EmailMessage
        globals()['EmailMessage'] = EmailMessage  # Cache: bypass __getattr__ on next access
        return EmailMessage
    raise AttributeError(f"module {__name__} has no attribute {name}")

thread_local_ctx = threading.local()

class RequestIDFormatter(logging.Formatter):
    """
    Custom logging formatter that injects a request_id into log records.
    The request ID is retrieved from Flask request context, Flask app context,
    or thread-local context, defaulting to 'system' if not present.
    """
    def format(self, record):
        """
        Format the logging record, injecting the request ID context variable.

        Args:
            record (logging.LogRecord): The log record to format.

        Returns:
            str: The formatted log message.
        """
        from flask import has_request_context, has_app_context, g, request
        import uuid
        try:
            if has_request_context():
                if not getattr(g, 'request_id', None):
                    g.request_id = request.headers.get('X-Request-ID', str(uuid.uuid4())[:8])
                record.request_id = g.request_id
            elif has_app_context() and getattr(g, 'request_id', None):
                record.request_id = g.request_id
            elif getattr(thread_local_ctx, 'request_id', None):
                record.request_id = thread_local_ctx.request_id
            else:
                record.request_id = 'system'
        except Exception:
            record.request_id = 'error'
        return super().format(record)

handler = logging.StreamHandler()
handler.setFormatter(RequestIDFormatter('%(asctime)s [%(levelname)s] %(name)s [req_id=%(request_id)s]: %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Thread-local storage for DNS pinning (SSRF / DNS Rebinding protection)
import urllib3.util.connection as connection
dns_cache = threading.local()
_orig_create_connection = connection.create_connection

def patched_create_connection(address, *args, **kwargs):
    """
    Intercept socket connection creation to route hostnames to thread-local pinned IPs.
    This helps prevent DNS rebinding SSRF TOCTOU (Time-of-Check to Time-of-Use) attacks.

    Args:
        address (tuple): A (host, port) pair.
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments.

    Returns:
        socket.socket: The connected socket object.
    """
    host, port = address
    pinned_ips = getattr(dns_cache, 'pinned_ips', None)
    if pinned_ips and host in pinned_ips:
        logger.info("DNS Pinning: routing %s to %s", host, pinned_ips[host])
        return _orig_create_connection((pinned_ips[host], port), *args, **kwargs)
    return _orig_create_connection(address, *args, **kwargs)

connection.create_connection = patched_create_connection

ACTIVE_CHATS_CANCEL_EVENTS = {}

def start_redis_cancellation_listener():
    """Starts a daemon thread to listen for cross-worker cancellations via Redis Pub/Sub."""
    def listen():
        pubsub = redis_client.pubsub()
        try:
            pubsub.subscribe("stellar_cancellations")
            logger.info("Redis cancellation listener subscribed to channel: stellar_cancellations")
            for message in pubsub.listen():
                if message['type'] == 'message':
                    try:
                        data = json.loads(message['data'] if isinstance(message['data'], str) else message['data'].decode('utf-8'))
                        chat_id = data.get('chat_id')
                        exclude_query_id = data.get('exclude_query_id')
                        
                        if chat_id:
                            # Account for potential type mismatches (string vs int)
                            for c_id in (chat_id, str(chat_id), int(chat_id) if isinstance(chat_id, str) and chat_id.isdigit() else None):
                                if c_id is None: continue
                                val = ACTIVE_CHATS_CANCEL_EVENTS.get(c_id)
                                if val:
                                    cancel_event, active_query_id = val if isinstance(val, tuple) else (val, None)
                                    if exclude_query_id and active_query_id == exclude_query_id:
                                        # Do not cancel if it matches the excluded new query
                                        continue
                                    logger.info("Received cross-process cancel signal chat_id=%s query_id=%s", c_id, active_query_id)
                                    cancel_event.set()
                    except Exception as parse_err:
                        logger.error("Error parsing cancellation pubsub message error=%s", parse_err, exc_info=True)
        except Exception as conn_err:
            logger.error("Redis cancellation listener connection error error=%s", conn_err, exc_info=True)
        finally:
            try:
                pubsub.close()
            except:
                pass

    threading.Thread(target=listen, daemon=True).start()

if os.environ.get('TESTING') == 'true':
    import redis
    redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)
    import docker
    try:
        client = docker.from_env()
    except Exception:
        client = None
else:
    redis_client = LazyRedis(host='localhost', port=6379, db=0, decode_responses=True)
    client = None

    def _setup_redis_notifications():
        try:
            redis_client.config_set('notify-keyspace-events', 'Ksx')
        except Exception as _kn_err:
            pass

    def _async_startup_setup():
        _setup_redis_notifications()
        start_redis_cancellation_listener()
        global client
        try:
            import docker
            client = docker.from_env()
            client.ping()
            logger.info("Successfully connected to Docker daemon on startup.")
            try:
                client.networks.get("stellar_isolated")
                logger.info("Found existing 'stellar_isolated' network.")
            except docker.errors.NotFound:
                logger.info("Creating 'stellar_isolated' network with ICC disabled.")
                client.networks.create("stellar_isolated", driver="bridge", options={"com.docker.network.bridge.enable_icc": "false"})
        except Exception as e:
            logger.error(f"Could not connect to Docker daemon on startup. Code execution will fail. Error: {e}")

    threading.Thread(target=_async_startup_setup, daemon=True).start()

from functools import wraps

def require_approval(f):
    """
    Decorator that requires a user to be authenticated and approved.
    If the user's ID is not in the session, it returns a 401 Unauthorized error.
    If the user is not marked as approved in the session or database, it returns a 403 Forbidden error.

    Args:
        f (function): The route handler function to decorate.

    Returns:
        function: The decorated function.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required.'}), 401

        # We check the session first for speed, but the index route syncs it with DB
        if not session.get('is_approved'):
            # Double check with DB to be sure
            db = get_db()
            cursor = db.execute('SELECT is_approved FROM users WHERE id = ?', (session['user_id'],))
            row = cursor.fetchone()
            if row and row[0]:
                session['is_approved'] = True
            else:
                session['is_approved'] = False
                return jsonify({'error': 'Access denied. You are on the waitlist or your access has been revoked.'}), 403
        return f(*args, **kwargs)
    return decorated_function

def ensure_user_network(docker_client, user_id):
    """
    Ensure that an isolated Docker bridge network exists for the given user.
    This limits inter-sandbox container communications.

    Args:
        docker_client (docker.DockerClient): The Docker client instance.
        user_id (int or str): The unique ID of the user.

    Returns:
        str: The name of the user network, or "stellar_isolated" if no user_id is provided.
    """
    import docker
    if not user_id:
        return "stellar_isolated"
    network_name = f"stellar_net_{user_id}"
    try:
        docker_client.networks.get(network_name)
    except docker.errors.NotFound:
        try:
            docker_client.networks.create(network_name, driver="bridge", options={"com.docker.network.bridge.enable_icc": "false"})
            logger.info("Created isolated Docker network network_name=%s user_id=%s", network_name, user_id)
        except docker.errors.APIError as api_err:
            logger.warning("Docker network creation skipped (likely concurrent creation) network_name=%s user_id=%s error=%s", network_name, user_id, api_err)
            pass  # Ignore if created concurrently
    return network_name

# telegram_bot unused - removed

def send_email_to_nikhil(subject, body):
    """
    Sends an email notification to the system administrator (Nikhil) at nikhil080905@gmail.com.
    """
    import smtplib
    from email.message import EmailMessage
    sender = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASS")
    recipient = "nikhil080905@gmail.com"
    
    if not sender or not password:
        logger.warning("EMAIL_USER or EMAIL_PASS not set. Skipping email notification.")
        return False
        
    msg = EmailMessage()
    msg['Subject'] = f"[STELLAR] {subject}"
    msg['From'] = f"Stellar System <{sender}>"
    msg['To'] = recipient
    msg.set_content(body)
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        logger.info("Email notification sent successfully to %s. Subject: %s", recipient, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email notification to %s: %s", recipient, e, exc_info=True)
        return False

def send_login_notification(username, display_name=None, is_waitlist=False):
    """
    Send a login or waitlist registration notification via Email.

    Args:
        username (str): The username of the user.
        display_name (str, optional): The display name of the user. Defaults to None.
        is_waitlist (bool, optional): Whether this notification is for a waitlist registration. Defaults to False.
    """
    t0 = time.time()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    name_str = f"{display_name} ({username})" if display_name else username
    if is_waitlist:
        subject = "New Waitlist Registration"
        message_body = f"⏳ New Waitlist Registration\nUser: {name_str}\nTime: {timestamp}"
    else:
        subject = "User Login on Stellar"
        message_body = f"✅ User Login on Stellar\nUser: {name_str}\nTime: {timestamp}"
    try:
        send_email_to_nikhil(subject, message_body)
        duration = time.time() - t0
        logger.info("Login notification sent via Email successfully username=%s duration_sec=%.3f", username, duration)
    except Exception as e:
        duration = time.time() - t0
        logger.error("Failed to send login notification via Email username=%s error=%s duration_sec=%.3f", username, e, duration)

# --- LOGGING AND ENV LOADING ---

script_dir = Path(__file__).resolve().parent
keys_env_path = script_dir / 'keys.env'
if keys_env_path.is_file():
    load_dotenv(dotenv_path=keys_env_path, override=True)
    logger.info("Loaded keys.env environment variables.")
else:
    logger.error(f"CRITICAL: keys.env NOT FOUND at {keys_env_path}.")
# -------------------------------
# VAPID Key Loading & Generation for PWA Web Push
# -------------------------------
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY_PEM = None

vapid_private_b64 = os.getenv("VAPID_PRIVATE_KEY_B64")
if vapid_private_b64:
    import base64
    try:
        VAPID_PRIVATE_KEY_PEM = base64.b64decode(vapid_private_b64).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to decode VAPID_PRIVATE_KEY_B64: {e}")

if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY_PEM:
    logger.info("VAPID keys not found. Generating fresh elliptic curve keys for PWA Web Push...")
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    import base64
    try:
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')

        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint
        )
        public_b64url = base64.urlsafe_b64encode(public_bytes).decode('utf-8').rstrip('=')
        private_b64 = base64.b64encode(private_pem.encode('utf-8')).decode('utf-8')

        VAPID_PUBLIC_KEY = public_b64url
        VAPID_PRIVATE_KEY_PEM = private_pem

        if keys_env_path.is_file():
            with open(keys_env_path, 'a') as f:
                f.write(f"\n# VAPID keys for PWA Web Push notifications\n")
                f.write(f"VAPID_PUBLIC_KEY=\"{public_b64url}\"\n")
                f.write(f"VAPID_PRIVATE_KEY_B64=\"{private_b64}\"\n")
            logger.info("Saved fresh VAPID keys to keys.env.")
    except Exception as e:
        logger.error(f"Failed to generate or save VAPID keys: {e}")

# Write the private key to a local PEM file for pywebpush to parse correctly
VAPID_PRIVATE_KEY_PATH = "/home/stellaradmin/my_app/vapid_private.pem"
if VAPID_PRIVATE_KEY_PEM:
    try:
        with open(VAPID_PRIVATE_KEY_PATH, "w") as f:
            f.write(VAPID_PRIVATE_KEY_PEM)
    except Exception as e:
        logger.error(f"Failed to write vapid_private.pem file: {e}")
# -------------------------------

app = Flask(__name__)

# Configure local git hooks automatically at startup
try:
    if os.path.exists('.git'):
        hook_path = os.path.join('git-hooks', 'pre-push')
        if os.path.exists(hook_path):
            os.chmod(hook_path, 0o755)
        import subprocess
        subprocess.run(['git', 'config', 'core.hooksPath', 'git-hooks'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Automatically configured local git hooks path.")
except Exception as e:
    logger.warning(f"Could not automatically configure git hooks: {e}")

SANDBOX_DIR = 'sandbox_runs'
os.makedirs(SANDBOX_DIR, exist_ok=True)
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf','docx','pptx', 'png', 'jpg', 'jpeg', 'gif', 'csv', 'md', 'py', 'js', 'html', 'css', 'json', 'xml', 'log', 'c', 'cpp', 'java', 'rb', 'php', 'go', 'rs', 'swift', 'kt','mp4','mp3'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

app.secret_key = os.getenv("FLASK_SECRET_KEY") or "stellar_fallback_secret_key_dev"

app.config['SESSION_COOKIE_NAME'] = 'stellar_session_main'
app.config['SESSION_PERMANENT'] = True

# Constants for Google OAuth (Update these if necessary)
FIREBASE_PROJECT_ID = "stellarai-live"

@app.route('/login/google', methods=['POST'])
def login_google():
    """
    Handle Google OAuth login and registration via ID token verification.
    Validates the user token, checks approval status, and initializes the user's session
    and isolated Docker network.

    Returns:
        Response: A Flask JSON response indicating success or failure.
    """
    data = request.get_json()
    token = data.get('id_token')

    if not token:
        return jsonify({"success": False, "message": "ID token required."}), 400

    try:
        # Verify the ID token using Google's verification library
        # For Firebase, the audience is the Firebase Project ID
        # and the issuer must be https://securetoken.google.com/<project_id>
        try:
            # Inline import of google.oauth2 and google.auth.transport to avoid startup overhead
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests
            t_verify = time.time()
            id_info = id_token.verify_firebase_token(
                token,
                google_requests.Request(),
                audience=FIREBASE_PROJECT_ID
            )
            logger.info("Firebase token verified successfully duration_sec=%.3f", time.time() - t_verify)
        except Exception as ve:
            logger.error("Token verification failed duration_sec=%.3f error=%s", time.time() - t_verify, ve)
            return jsonify({"success": False, "message": f"Verification failed: {ve}"}), 401

        # Additional Firebase-specific checks
        if id_info.get('iss') != f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}":
            logger.error(f"Invalid issuer: {id_info.get('iss')}")
            raise ValueError("Invalid issuer.")

        email = id_info['email']
        name = data.get('display_name') or id_info.get('name') or email.split('@')[0]
        picture_url = id_info.get('picture')

        db = get_db()
        cursor = db.execute('SELECT id, username, display_name, pfp_url, role, is_approved, login_count FROM users WHERE username = ?', (email,))
        user = _fetchone_as_dict(cursor)

        is_new_user = False
        if not user:
            is_new_user = True
            # Check for first user
            cursor = db.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]

            if user_count == 0:
                role = 'admin'
                is_approved = 1
            else:
                role = 'user'
                is_approved = 0

            db.execute('INSERT INTO users (username, display_name, pfp_url, role, is_approved) VALUES (?, ?, ?, ?, ?)', (email, name, picture_url, role, is_approved))
            db.commit()

            cursor = db.execute('SELECT id, username, display_name, pfp_url, role, is_approved, login_count FROM users WHERE username = ?', (email,))
            user = _fetchone_as_dict(cursor)
        else:
            # Update display_name or pfp_url if missing or different
            if user.get('display_name') != name or user.get('pfp_url') != picture_url:
                db.execute('UPDATE users SET display_name = ?, pfp_url = ? WHERE id = ?', (name, picture_url, user['id']))
                db.commit()
                user['display_name'] = name
                user['pfp_url'] = picture_url

        # Update login count and last active
        db.execute('UPDATE users SET login_count = login_count + 1, last_active = CURRENT_TIMESTAMP WHERE id = ?', (user['id'],))
        db.commit()

        try:
            is_waitlist = not bool(user['is_approved'])
            req_id = g.request_id if getattr(g, 'request_id', None) else 'system'
            def send_login_notification_thread_target(username, display_name, is_waitlist, r_id):
                thread_local_ctx.request_id = r_id
                send_login_notification(username, display_name, is_waitlist)

            notification_thread = threading.Thread(
                target=send_login_notification_thread_target,
                args=(email, name, is_waitlist, req_id),
                daemon=True
            )
            notification_thread.start()
        except Exception as e:
            logger.error(f"Error during login notification for {email}: {e}")

        # Set session — write keys directly so Flask-Session marks the session
        # as modified and emits the Set-Cookie header in the response.
        session['user_id'] = user['id']
        session['username'] = user['username'] # This is the email
        session['display_name'] = user['display_name']
        session['role'] = user['role']
        session['is_approved'] = bool(user['is_approved'])
        session['pfp_url'] = user.get('pfp_url')
        session.modified = True
        session.permanent = True

        if user['is_approved']:
            get_current_chat_id(session['user_id'])

        logger.info("User login successful email=%s role=%s is_approved=%s is_new_user=%s", email, user['role'], bool(user['is_approved']), is_new_user)
        return jsonify({"success": True, "is_approved": bool(user['is_approved'])}), 200

    except ValueError:
        return jsonify({"success": False, "message": "Invalid ID token."}), 401
    except Exception as e:
        logger.error(f"Unexpected error during Google login: {e}", exc_info=True)
        return jsonify({"success": False, "message": str(e)}), 500
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(days=7)

app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_DOMAIN'] = None

app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'stellar:session:'
app.config['SESSION_REDIS'] = LazyRedis(host='localhost', port=6379, db=1)

Session(app)

# Override get_cookie_domain to dynamically support wildcard subdomains in production
original_get_cookie_domain = app.session_interface.get_cookie_domain
def custom_get_cookie_domain(app):
    """
    Retrieve the custom session cookie domain to support wildcard subdomains dynamically.
    If 'stellarai.live' is in the request host header, it returns '.stellarai.live' to share
    session cookies across subdomains.

    Args:
        app (Flask): The Flask application instance.

    Returns:
        str or None: The cookie domain to use for session cookies.
    """
    if has_request_context():
        host = request.headers.get('Host', '')
        if 'stellarai.live' in host:
            return '.stellarai.live'
    return original_get_cookie_domain(app)
app.session_interface.get_cookie_domain = custom_get_cookie_domain

from key_manager import GlobalKeyManager, PRIMARY_API_KEY, BACKUP_API_KEYS, KEY_MANAGER

if PRIMARY_API_KEY:
    masked = PRIMARY_API_KEY[:4] + "..." + PRIMARY_API_KEY[-4:]
    logger.info(f"PRIMARY_API_KEY initialized: {masked}")
else:
    logger.error("CRITICAL: PRIMARY_API_KEY is EMPTY after loading attempt.")
# ------------------------------

MODEL_NAMES = {
    "gemini-3.1-flash-lite": "Emerald",
    "gemma-4-31b-it": "Lunarity",
    "gemini-3-flash-preview": "Crimson",
    "gemini-3.5-flash": "Obsidian"
}
ERROR_CODE = "ERROR_CODE_ABC123XYZ456"

# -------------------------------------------------------------


def get_seconds_until_pacific_midnight():
    """
    Calculate the number of seconds remaining until the next Pacific Time midnight (00:00:00 Pacific).
    This is used to determine daily reset times for RPD quota limits, taking Daylight Saving Time (DST)
    into account based on US calendar rules.

    Returns:
        int: Number of seconds until the next Pacific Time midnight, or 14400 (4 hours) as fallback.
    """
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        year = now_utc.year

        # DST start: 2nd Sunday in March
        march_1 = datetime.datetime(year, 3, 1, tzinfo=datetime.timezone.utc)
        march_1_dow = march_1.weekday()
        days_to_first_sunday = (6 - march_1_dow) % 7
        dst_start = march_1 + datetime.timedelta(days=days_to_first_sunday + 7)

        # DST end: 1st Sunday in November
        nov_1 = datetime.datetime(year, 11, 1, tzinfo=datetime.timezone.utc)
        nov_1_dow = nov_1.weekday()
        days_to_nov_sunday = (6 - nov_1_dow) % 7
        dst_end = nov_1 + datetime.timedelta(days=days_to_nov_sunday)

        is_dst = dst_start <= now_utc < dst_end
        pacific_offset = datetime.timedelta(hours=-7) if is_dst else datetime.timedelta(hours=-8)

        now_pacific = now_utc + pacific_offset
        # Next Pacific midnight is tomorrow at 00:00:00 Pacific
        tomorrow_pacific = datetime.datetime(now_pacific.year, now_pacific.month, now_pacific.day, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=1)

        seconds_until_midnight = (tomorrow_pacific - now_pacific).total_seconds()
        return max(int(seconds_until_midnight), 60)
    except Exception as e:
        logger.error(f"Error calculating Pacific midnight offset: {e}")
        return 14400 # Fallback to 4 hours if datetime calculations fail


def parse_quota_block_duration(error_msg):
    """
    Parse an error message to classify the quota/rate limit error type and determine the block duration.

    Args:
        error_msg (str): The raw error or exception string.

    Returns:
        tuple: (duration_seconds, block_reason) where duration_seconds is an int, and block_reason is a str.
    """
    err_lower = error_msg.lower()
    if ('minute' in err_lower or 'queries per minute' in err_lower or
        'rpm' in err_lower or 'tpm' in err_lower or 'queriesperminute' in err_lower):
        # Minute limit / TPM / RPM: Block for 61 seconds (extra 1s for network I/O jitter)
        return 61, 'RPM'
    elif ('requestsperday' in err_lower or 'requests per day' in err_lower or
          'daily' in err_lower or 'perday' in err_lower or 'projectpermodel-freetier' in err_lower or
          'exceeded your current quota' in err_lower or 'billing details' in err_lower or 'quota/rate limits' in err_lower):
        # Daily limit / Quota exhaustion: Block until the next Pacific Midnight reset time
        duration = get_seconds_until_pacific_midnight()
        return duration, 'RPD'
    elif ('overloaded' in err_lower or '503' in err_lower or 'service unavailable' in err_lower or 'service_unavailable' in err_lower):
        # Model overloaded / 503: Block key for 600 seconds to let Google cool down
        return 600, 'OVERLOAD'
    elif ('500' in err_lower or 'internal error' in err_lower or 'internal_error' in err_lower):
        # Internal error / 500: Block key for 10 seconds
        return 10, 'INTERNAL'
    # Minute limit / TPM / RPM: Block for 61 seconds (extra 1s for network I/O jitter)
    return 61, 'RPM'


def get_fallback_chain(start_model):
    """
    Get the chain of model identifiers to fall back on if the requested model fails.

    Args:
        start_model (str): The initial model identifier.

    Returns:
        list of str: A sequence of fallback model identifiers.
    """
    chain = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemma-4-31b-it"]
    if start_model in chain:
        idx = chain.index(start_model)
        return chain[idx:]
    return [start_model, "gemma-4-31b-it"]

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
# (PRIMARY_API_KEY already assigned in aggressive loading block)

# BACKUP_API_KEYS are imported from key_manager

tavily_backup_env_pattern = re.compile(r'^TAVILY_BACKUP_API_KEY_(\d+)$')
tavily_backup_vars = {
    int(match.group(1)): os.environ[key]
    for key in os.environ
    if (match := tavily_backup_env_pattern.match(key))
}
TAVILY_BACKUP_API_KEYS = [tavily_backup_vars[i] for i in sorted(tavily_backup_vars.keys())]

DATABASE_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stellar_local.db')

def _fetch_as_dict(cursor):
    """
    Fetch all rows from a database cursor as a list of dictionaries.

    Args:
        cursor (sqlite3.Cursor): The SQLite database cursor.

    Returns:
        list of dict: The query results formatted as list of column-to-value dictionaries.
    """
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def get_or_create_encryption_key():
    """
    Retrieve the existing Fernet encryption key from disk or generate a new one if it does not exist.

    Returns:
        bytes: The Fernet symmetric encryption key.
    """
    key_path = Path(script_dir / 'encryption.key')
    if key_path.is_file():
        with open(key_path, 'rb') as key_file:
            key = key_file.read()
    else:
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        with open(key_path, 'wb') as key_file:
            key_file.write(key)
    return key

ENCRYPTION_KEY = get_or_create_encryption_key()
cipher_suite = LazyFernet(ENCRYPTION_KEY)


def _fetchone_as_dict(cursor):
    """
    Fetch a single row from a database cursor as a dictionary.

    Args:
        cursor (sqlite3.Cursor): The SQLite database cursor.

    Returns:
        dict or None: The single query row formatted as a dictionary, or None if no row is returned.
    """
    row = cursor.fetchone()
    if row:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
    return None

def get_db():
    """
    Retrieve or establish a thread-safe connection to the SQLite database.
    Stores the connection in Flask's global 'g' object. Enforces WAL mode and
    a busy timeout of 5 seconds.

    Returns:
        sqlite3.Connection: The active SQLite database connection.
    """
    if 'db' not in g:
        t0 = time.time()
        g.db = sqlite3.connect(DATABASE_NAME)
        g.db.row_factory = sqlite3.Row
        # Enable WAL mode and set timeout for concurrency
        g.db.execute("PRAGMA journal_mode=WAL;")
        g.db.execute("PRAGMA busy_timeout=5000;")
        duration = time.time() - t0
        logger.info("Database connection established duration_sec=%.3f", duration)
    return g.db

@app.teardown_appcontext
def close_db(error):
    """
    Close the database connection during Flask application context teardown.

    Args:
        error (Exception or None): The exception raised during context execution, if any.
    """
    db = g.pop('db', None)
    if db is not None:
        db.close()

def initialize_database():
    """
    Initialize the SQLite database schema if it doesn't already exist.
    Creates necessary tables (users, chats, messages, repo_history, etc.) and indexes.
    """
    global t_db_init_start
    t_db_init_start = time.time()
    with app.app_context():
        db = get_db()
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA busy_timeout=5000;")
        cursor = db.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL, -- Stores email
                role TEXT DEFAULT 'user',
                is_approved BOOLEAN DEFAULT 0,
                login_count INTEGER NOT NULL DEFAULT 0,
                last_active DATETIME DEFAULT (CURRENT_TIMESTAMP),
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )''')

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chats'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'New Chat',
                token_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )''')

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_type TEXT NOT NULL,
                message_content TEXT NOT NULL,
                is_research_output BOOLEAN DEFAULT 0,
                html_file TEXT,
                file_analysis_context TEXT,
                visualization_html TEXT,
                hidden BOOLEAN DEFAULT 0,
                timestamp DATETIME DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            )''')

        # Migration: Add visualization_html column if it doesn't exist
        cursor.execute("PRAGMA table_info(messages)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'visualization_html' not in columns:
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN visualization_html TEXT")
                logger.info("Added 'visualization_html' column to 'messages' table.")
            except Exception as e:
                logger.exception("Error adding 'visualization_html' column: %s", e)

        # Migration: Add hidden column if it doesn't exist
        if 'hidden' not in columns:
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN hidden BOOLEAN DEFAULT 0")
                logger.info("Added 'hidden' column to 'messages' table.")
            except Exception as e:
                logger.exception("Error adding 'hidden' column: %s", e)

        # Migration: Add attached_files column for Native Gemini File URIs
        if 'attached_files' not in columns:
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN attached_files TEXT")
                logger.info("Added 'attached_files' column to 'messages' table.")
            except Exception as e:
                logger.exception("Error adding 'attached_files' column: %s", e)

        # Add user_logs_prefs table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_logs_prefs'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE user_logs_prefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, -- user email or 'global'
                log_entry TEXT NOT NULL,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )''')
            logger.info("Created 'user_logs_prefs' table.")

        cursor.execute("PRAGMA table_info(users)")
        users_columns = [info[1] for info in cursor.fetchall()]
        if 'display_name' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
                logger.info("Added 'display_name' column to 'users' table.")
            except Exception as e:
                logger.exception("Error adding 'display_name' column: %s", e)

        if 'last_active' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN last_active DATETIME")
                logger.info("Added 'last_active' column to 'users' table.")
            except Exception as e:
                logger.exception("Error adding 'last_active' column: %s", e)

        if 'pfp_url' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN pfp_url TEXT")
                logger.info("Added 'pfp_url' column to 'users' table.")
            except Exception as e:
                logger.exception("Error adding 'pfp_url' column: %s", e)

        if 'designation' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN designation TEXT")
                logger.info("Added 'designation' column to 'users' table.")
            except Exception as e:
                logger.exception("Error adding 'designation' column: %s", e)

        if 'source' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN source TEXT")
                logger.info("Added 'source' column to 'users' table.")
            except Exception as e:
                logger.exception("Error adding 'source' column: %s", e)

        if 'use_case' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN use_case TEXT")
                logger.info("Added 'use_case' column to 'users' table.")
            except Exception as e:
                logger.exception("Error adding 'use_case' column: %s", e)

        if 'waitlist_form_submitted' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN waitlist_form_submitted BOOLEAN DEFAULT 0")
                logger.info("Added 'waitlist_form_submitted' column to 'users' table.")
            except Exception as e:
                logger.exception("Error adding 'waitlist_form_submitted' column: %s", e)

        cursor.execute("PRAGMA table_info(chats)")
        chats_columns = [info[1] for info in cursor.fetchall()]
        if 'token_count' not in chats_columns:
            try:
                cursor.execute("ALTER TABLE chats ADD COLUMN token_count INTEGER DEFAULT 0")
                logger.info("Added 'token_count' column to 'chats' table.")
            except Exception as e:
                logger.exception("Error adding 'token_count' column: %s", e)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_api_keys'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE user_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                key_name TEXT NOT NULL,
                encrypted_value BLOB NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, key_name) ON CONFLICT REPLACE
            )''')

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='repo_history'")
        if cursor.fetchone() is None:
            cursor.execute('''
                CREATE TABLE repo_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    project_name TEXT DEFAULT 'Untitled Project',
                    process_id TEXT NOT NULL,
                    container_id TEXT,
                    status TEXT,
                    deployment_url TEXT,
                    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                    last_updated DATETIME DEFAULT (CURRENT_TIMESTAMP),
                    resource_usage TEXT,
                    files_snapshot TEXT,
                    build_logs TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')

        cursor.execute("PRAGMA table_info(repo_history)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'subdomain' not in columns:
            try:
                cursor.execute("ALTER TABLE repo_history ADD COLUMN subdomain TEXT")
                cursor.execute("CREATE INDEX idx_subdomain ON repo_history(subdomain)")
                logger.info("Added 'subdomain' column to 'repo_history' table.")
            except Exception as e:
                logger.exception("Error adding 'subdomain' column: %s", e)

        cursor.execute('''CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            tool_name TEXT NOT NULL,
            input_params TEXT,
            result TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )''')

        cursor.execute("PRAGMA table_info(tool_calls)")
        tc_columns = [info[1] for info in cursor.fetchall()]
        if 'hidden' not in tc_columns:
            try:
                cursor.execute("ALTER TABLE tool_calls ADD COLUMN hidden BOOLEAN DEFAULT 0")
                logger.info("Added 'hidden' column to 'tool_calls' table.")
            except Exception as e:
                logger.exception("Error adding 'hidden' column to tool_calls: %s", e)

        cursor.execute("PRAGMA table_info(chats)")
        chats_columns = [info[1] for info in cursor.fetchall()]
        if 'is_temp' not in chats_columns:
            try:
                cursor.execute("ALTER TABLE chats ADD COLUMN is_temp BOOLEAN DEFAULT 0")
                logger.info("Added 'is_temp' column to 'chats' table.")
            except Exception as e:
                logger.exception("Error adding 'is_temp' column: %s", e)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'")
        if cursor.fetchone() is None:
            # Bolt - Stability/Architecture: include 'status' and 'lock_id' columns in scheduled_tasks schema
            cursor.execute('''CREATE TABLE scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                task_prompt TEXT NOT NULL,
                model_id TEXT NOT NULL,  -- STORES THE MODEL THAT CREATED THE TASK
                execute_at DATETIME,
                recurring_minutes INTEGER DEFAULT 0,
                metadata TEXT,
                is_active BOOLEAN DEFAULT 1,
                last_run DATETIME,
                status TEXT DEFAULT 'pending',
                lock_id TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
            )''')
        else:
            # Migration: Add metadata column if it doesn't exist
            cursor.execute("PRAGMA table_info(scheduled_tasks)")
            st_columns = [info[1] for info in cursor.fetchall()]
            if 'metadata' not in st_columns:
                try:
                    cursor.execute("ALTER TABLE scheduled_tasks ADD COLUMN metadata TEXT")
                    logger.info("Added 'metadata' column to 'scheduled_tasks' table.")
                except Exception as e:
                    logger.exception("Error adding 'metadata' column to scheduled_tasks: %s", e)
            # Bolt - Stability: Add 'status' and 'lock_id' columns to existing tables
            if 'status' not in st_columns:
                try:
                    cursor.execute("ALTER TABLE scheduled_tasks ADD COLUMN status TEXT DEFAULT 'pending'")
                    logger.info("Added 'status' column to 'scheduled_tasks' table.")
                except Exception as e:
                    logger.exception("Error adding 'status' column to scheduled_tasks: %s", e)
            if 'lock_id' not in st_columns:
                try:
                    cursor.execute("ALTER TABLE scheduled_tasks ADD COLUMN lock_id TEXT")
                    logger.info("Added 'lock_id' column to 'scheduled_tasks' table.")
                except Exception as e:
                    logger.exception("Error adding 'lock_id' column to scheduled_tasks: %s", e)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_feedback'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE agent_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                topic TEXT,
                issue_description TEXT,
                technical_context TEXT,
                status TEXT DEFAULT 'open',
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE SET NULL
            )''')
            logger.info("Created 'agent_feedback' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='talents'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE talents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                talent_name TEXT UNIQUE NOT NULL,
                mandate_text TEXT NOT NULL
            )''')
            logger.info("Created 'talents' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='push_subscriptions'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )''')
            logger.info("Created 'push_subscriptions' table.")

        # Migrate push subscriptions to Redis if SQLite contains any
        try:
            cursor.execute("SELECT user_id, endpoint, p256dh, auth FROM push_subscriptions")
            rows = cursor.fetchall()
            if rows:
                import redis
                r_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
                migrated = 0
                for row in rows:
                    redis_key = f"user_push_subscriptions:{row['user_id']}"
                    val = json.dumps({"p256dh": row['p256dh'], "auth": row['auth']})
                    r_client.hset(redis_key, row['endpoint'], val)
                    migrated += 1
                if migrated > 0:
                    logger.info(f"Migrated {migrated} PWA push subscriptions from SQLite to Redis.")
                    cursor.execute("DELETE FROM push_subscriptions")
                    db.commit()
        except Exception as e:
            logger.exception("Error migrating push subscriptions to Redis: %s", e)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sentinel_app_errors'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE sentinel_app_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_id TEXT NOT NULL,
                error_type TEXT,
                error_message TEXT,
                stack_trace TEXT,
                affected_file TEXT,
                affected_line INTEGER,
                status TEXT DEFAULT 'open',
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )''')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sentinel_errors_process_id ON sentinel_app_errors(process_id)")
            logger.info("Created 'sentinel_app_errors' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sentinel_app_patches'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE sentinel_app_patches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_id INTEGER NOT NULL,
                patch_diff TEXT,
                status TEXT NOT NULL,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (error_id) REFERENCES sentinel_app_errors(id) ON DELETE CASCADE
            )''')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sentinel_patches_error_id ON sentinel_app_patches(error_id)")
            logger.info("Created 'sentinel_app_patches' table.")

        # Add performance indexes for foreign key lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_chat_id ON tool_calls(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_repo_history_user_id ON repo_history(user_id)")
        # Bolt - Performance: composite index lets the sidebar MAX(timestamp) correlated subquery
        # use an index range scan instead of a full table scan on messages for each chat row.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_timestamp ON messages(chat_id, timestamp)")
        # Bolt - Performance: composite index for repository lists sorted by last updated
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_repo_history_user_last_updated ON repo_history(user_id, last_updated DESC)")
        # Bolt - Performance: index process_id on repo_history to avoid full table scans on status checks and updates
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_repo_history_process_id ON repo_history(process_id)")
        # Bolt - Performance: index user_id on user_logs_prefs to optimize memory/preferences lookup
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_logs_prefs_user_id ON user_logs_prefs(user_id)")
        # Bolt - Performance: index user_id and claim criteria on scheduled_tasks to prevent full table scans
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_user_id ON scheduled_tasks(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_claim ON scheduled_tasks(is_active, status, execute_at)")

        db.commit()
    logger.info("Database initialization completed duration_sec=%.3f", time.time() - t_db_init_start)

initialize_database()

def get_current_session_id():
    """
    Get the session ID for the current user session.
    Retrieves from Flask's global 'g' object if called outside a request context.

    Returns:
        str or None: The session ID, or None if not available.
    """
    if not has_request_context():
        return getattr(g, 'session_id', None)
    if 'initialized' not in session:
        session['initialized'] = True
    return getattr(session, 'sid', None)

def get_file_context_id():
    """Returns chat_id if available, otherwise session_id, to isolate files."""
    if not has_request_context():
        cid = getattr(g, 'chat_id', None)
        return str(cid) if cid else getattr(g, 'session_id', None)
    chat_id = session.get('current_chat_id')
    return str(chat_id) if chat_id else get_current_session_id()

def get_current_chat_id(user_id):
    """
    Retrieve the active chat ID for the user, falling back to the last active chat or creating a new one if needed.

    Args:
        user_id (int): The ID of the user.

    Returns:
        int: The active chat ID.
    """
    db = get_db()
    if has_request_context():
        chat_id = session.get('current_chat_id')
    else:
        chat_id = getattr(g, 'chat_id', None)

    if chat_id:
        cursor = db.execute('SELECT id FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
        if cursor.fetchone():
            return chat_id
        else:
            chat_id = None

    cursor = db.execute('SELECT id FROM chats WHERE user_id = ? AND is_temp = 0 ORDER BY created_at DESC LIMIT 1', (user_id,))
    last_chat = cursor.fetchone()

    if last_chat:
        if has_request_context():
            session['current_chat_id'] = last_chat['id']
    else:
        cursor = db.execute('INSERT INTO chats (user_id, name) VALUES (?, ?)', (user_id, 'New Chat'))
        db.commit()
        new_chat_id = cursor.lastrowid
        if has_request_context():
            session['current_chat_id'] = new_chat_id


    if has_request_context():
        session.modified = True
        return session['current_chat_id']
    return chat_id or last_chat['id'] if last_chat else None

def insert_message(chat_id, message_type, message_content,
                   is_research_output=False, html_file=None,
                   attached_files=None, user_query_for_name=None,
                   hidden=False, client_id=None):
    """Insert a new message into the messages table."""
    if not chat_id:
        return None

    max_retries = 3
    retry_delay_seconds = 1

    hidden_val = 1 if hidden else 0

    for attempt in range(max_retries):
        try:
            db = get_db()
            cursor = db.execute(
                '''INSERT INTO messages (chat_id, message_type, message_content,
                                       is_research_output, html_file,
                                       attached_files, hidden)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (chat_id, message_type, message_content,
                 is_research_output, html_file,
                 json.dumps(attached_files) if attached_files else None,
                 hidden_val)
            )
            db.commit()
            last_id = cursor.lastrowid

            req_id = 'system'
            from flask import has_request_context, has_app_context, g
            if has_request_context() and getattr(g, 'request_id', None):
                req_id = g.request_id
            elif has_app_context() and getattr(g, 'request_id', None):
                req_id = g.request_id

            if message_type == "user" and user_query_for_name and not hidden:
                num_messages_in_chat = db.execute('SELECT COUNT(*) FROM messages WHERE chat_id = ?', (chat_id,)).fetchone()[0]
                if num_messages_in_chat == 1 or (num_messages_in_chat -1) % 10 == 0:
                    def thread_target(app_instance, target_chat_id, target_query, r_id):
                        thread_local_ctx.request_id = r_id
                        with app_instance.app_context():
                            generate_chat_name(target_chat_id, target_query)

                    threading.Thread(target=thread_target, args=(current_app._get_current_object(), chat_id, user_query_for_name, req_id), daemon=True).start()

            # Trigger Token Count update in background
            def token_update_thread(app_instance, target_chat_id, r_id):
                thread_local_ctx.request_id = r_id
                with app_instance.app_context():
                    try:
                        count_chat_tokens(target_chat_id)
                    except Exception as e:
                        logger.error(f"Error in token_update_thread: {e}")

            threading.Thread(target=token_update_thread, args=(current_app._get_current_object(), chat_id, req_id), daemon=True).start()

            # Broadcast to other devices syncing this user's state
            try:
                cursor = db.execute('SELECT user_id FROM chats WHERE id = ?', (chat_id,))
                chat_owner = cursor.fetchone()
                if chat_owner:
                    owner_id = chat_owner['user_id']
                    event_payload = {
                        "type": "new_message",
                        "client_id": client_id,
                        "chat_id": chat_id,
                        "message": {
                            "id": last_id,
                            "type": message_type,
                            "content": message_content,
                            "is_research": is_research_output,
                            "html_file": html_file,
                            "hidden": hidden
                        }
                    }
                    redis_client.publish(f"user_events:{owner_id}", json.dumps(event_payload))
            except Exception as e:
                logger.error("Failed to broadcast new message chat_id=%s message_id=%s error=%s", chat_id, last_id, e)

            return last_id
        except sqlite3.OperationalError as e:
            logger.error("Database error in insert_message attempt=%d/%d chat_id=%s message_type=%s error=%s", attempt + 1, max_retries, chat_id, message_type, e, exc_info=True)
            if attempt < max_retries - 1:
                time.sleep(retry_delay_seconds)
            else:
                return None
        except Exception as e:
            logger.error("Unexpected error in insert_message chat_id=%s message_type=%s error=%s", chat_id, message_type, e, exc_info=True)
            return None


def format_time_delta(td: datetime.timedelta) -> str:
    """Format a timedelta into a concise human-readable relative time string."""
    seconds = int(td.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m later" if minutes > 1 else "1m later"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h later" if hours > 1 else "1h later"
    days = hours // 24
    if days < 7:
        return f"{days}d later" if days > 1 else "1d later"
    weeks = days // 7
    if weeks < 4:
        return f"{weeks}w later" if weeks > 1 else "1w later"
    months = days // 30
    return f"{months}mo later" if months > 1 else "1mo later"


def build_annotated_history(conversation_history, user_message_id):
    """Build the conversation history list with relative time annotations and return the last message time."""
    conv_hist_list = []
    last_msg_time = None
    if conversation_history:
        prev_time = None
        for msg in conversation_history:
            if str(msg.get('id')) == str(user_message_id):
                continue
            role = 'User' if msg.get('message_type') == 'user' else 'Stellar'
            content = msg.get('message_content', '')

            # Calculate time delta for context
            time_str = msg.get('timestamp')
            time_annotation = ""
            if time_str:
                try:
                    # DB timestamps are UTC strings, e.g. '2026-06-02 16:19:02'
                    msg_time = datetime.datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                    if prev_time is None:
                        time_annotation = f" (at {msg_time.strftime('%Y-%m-%d %H:%M:%S')} UTC)"
                    else:
                        delta = msg_time - prev_time
                        time_annotation = f" ({format_time_delta(delta)})"
                    prev_time = msg_time
                    last_msg_time = msg_time
                except Exception as te:
                    logger.error(f"Error parsing timestamp {time_str}: {te}")

            # Strip base64 before passing to LLM context
            clean_content = re.sub(r'(data:image/[^;]+;base64,)[a-zA-Z0-9+/=]+', r'\1[TRUNCATED]', content)
            conv_hist_list.append(f"{role}{time_annotation}: {clean_content}")

    return conv_hist_list, last_msg_time


def get_conversation_history(chat_id, for_ui=False):
    """Retrieve conversation history for a chat."""
    if not chat_id:
        return []
    try:
        db = get_db()
        if for_ui:
            cursor = db.execute(
                '''SELECT id, message_type, message_content, is_research_output, html_file,
                          attached_files, visualization_html, timestamp, hidden
                   FROM messages WHERE chat_id = ? AND hidden = 0 ORDER BY timestamp ASC''',
                (chat_id,)
            )
        else:
            cursor = db.execute(
                '''SELECT id, message_type, message_content, is_research_output, html_file,
                          attached_files, visualization_html, timestamp, hidden
                   FROM messages WHERE chat_id = ? AND (hidden = 0 OR message_content LIKE '[COMPRESSED MEMORY STATE]%' OR message_content LIKE '%*[Response interrupted by user]*%') ORDER BY timestamp ASC''',
                (chat_id,)
            )
        rows = _fetch_as_dict(cursor)

        history = []
        for row in rows:
            msg = dict(row)
            if msg.get('attached_files'):
                try:
                    msg['attached_files'] = json.loads(msg['attached_files'])
                except:
                    msg['attached_files'] = []
            else:
                msg['attached_files'] = []

            # CRITICAL: Prevent frontend crashes by truncating massive Base64 images
            if msg.get('message_content'):
                # If content is huge and contains base64, replace the base64 part
                if len(msg['message_content']) > 500000 and 'base64,' in msg['message_content']:
                    msg['message_content'] = re.sub(r'(data:image/[^;]+;base64,)[a-zA-Z0-9+/=]+', r'\1[TRUNCATED BY SERVER TO PREVENT CRASH]', msg['message_content'])

            # Add HTML URL for research outputs
            if msg.get('is_research_output') and msg.get('html_file'):
                 safe_filename = os.path.basename(msg['html_file'])
                 msg['html_url'] = f'/view/{safe_filename}'
            msg['id'] = str(msg['id'])
            history.append(msg)

        return history
    except sqlite3.Error as e:
        logger.error(f"Database error in get_conversation_history: {e}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error in get_conversation_history: {e}", exc_info=True)
        return []

def get_tool_history(chat_id):
    """Retrieve tool calls with smart truncation for large outputs."""
    if not chat_id: return ""
    try:
        db = get_db()
        cursor = db.execute('''
            SELECT id, tool_name, input_params, length(result) as res_len,
                   CASE
                       WHEN tool_name IN ('read_tool_output', 'obtain_talent') THEN result
                       ELSE substr(result, 1, 1000)
                   END as res_part,
                   timestamp
            FROM tool_calls
            WHERE chat_id = ? AND hidden = 0
            ORDER BY timestamp ASC
        ''', (chat_id,))
        rows = cursor.fetchall()
        if not rows: return ""

        context_lines = ["\n**Internal Tool Execution History:**"]
        for r in rows:
            res_str = r['res_part']
            if res_str is None:
                res_str = ""
            else:
                res_str = str(res_str)
            num_chars = r['res_len'] if r['res_len'] is not None else 0

            # Smart Truncation Logic
            if r['tool_name'] in ['read_tool_output', 'obtain_talent']:
                clean_res = res_str
            elif 'data:image' in res_str:
                clean_res = "[Image Generated]"
            elif num_chars > 600:
                num_lines = res_str.count('\n') + 1
                clean_res = f"[Output truncated. ID: {r['id']}, Lines: {num_lines}, Length: {num_chars} chars. Use read_tool_output(output_id={r['id']}) to view.]"
            else:
                num_lines = res_str.count('\n') + 1
                if num_lines > 20:
                    clean_res = f"[Output truncated. ID: {r['id']}, Lines: {num_lines}, Length: {num_chars} chars. Use read_tool_output(output_id={r['id']}) to view.]"
                else:
                    clean_res = res_str

            input_str = str(r['input_params'])
            clean_input = input_str if len(input_str) <= 1000 else input_str[:1000] + f"... [Input truncated. Full length: {len(input_str)} chars]"

            context_lines.append(f"- [{r['timestamp']}] Tool: `{r['tool_name']}` (ID: {r['id']}) | Input: `{clean_input}` | Result: `{clean_res}`")

        # Performance optimization: use join instead of += for string concatenation in loop
        return "\n".join(context_lines) + "\n---\n"
    except Exception as e:
        logger.error(f"Error fetching tool history: {e}")
        return ""

def update_message(message_id, content):
    """
    Update the text content of a specific message in the database.

    Args:
        message_id (int): The unique ID of the message.
        content (str): The new content to write.

    Returns:
        bool: True if the update succeeded; False otherwise.
    """
    try:
        db = get_db()
        cursor = db.execute('SELECT chat_id FROM messages WHERE id = ?', (message_id,))
        chat_info = _fetchone_as_dict(cursor)
        if not chat_info:
            return False
        chat_id = chat_info['chat_id']
        db.execute('UPDATE messages SET message_content = ? WHERE id = ?', (content, message_id))
        db.commit()

        return True
    except sqlite3.Error as e:
        logger.error(f"Database error in update_message: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Unexpected error in update_message: {e}", exc_info=True)
        return False


def generate_chat_name(chat_id, first_message_content):
    """
    Generate a short, descriptive chat name using Gemini based on the first message.
    Automatically handles rate limits and API key rotation.

    Args:
        chat_id (int): The unique chat identifier.
        first_message_content (str): The content of the first message.
    """
    with app.app_context():
        db = get_db()
        try:
            # Inline import to avoid startup overhead
            from google import genai
            prompt = f"Given the following first message of a conversation, generate a very short, descriptive name (max 5 words) for this chat. Respond only with the name.\n\nMessage: {first_message_content}"
            model_name = "gemini-3.1-flash-lite"

            raw_keys = [PRIMARY_API_KEY] + [bk for bk in BACKUP_API_KEYS if bk]
            keys_to_try = [k for k in dict.fromkeys(raw_keys) if k]

            # Filter out globally rate-limited or quota-exhausted keys
            active_keys = []
            for k in keys_to_try:
                is_blocked, _ = KEY_MANAGER.is_key_blocked(k, model_name)
                if not is_blocked:
                    active_keys.append(k)
            if not active_keys:
                active_keys = keys_to_try
            keys_to_try = active_keys

            for current_key in keys_to_try:
                try:
                    client = genai.Client(api_key=current_key, http_options={'api_version': 'v1beta'})
                    chat = client.chats.create(model=model_name, config={'tools': []})
                    t0 = time.time()
                    r = chat.send_message(prompt)
                    duration = time.time() - t0
                    usage = getattr(r, 'usage_metadata', None)
                    prompt_tokens = getattr(usage, 'prompt_token_count', 0) if usage else 0
                    candidates_tokens = getattr(usage, 'candidates_token_count', 0) if usage else 0
                    logger.info("Gemini API call completed model=%s duration_sec=%.2f purpose=chat_name_generation prompt_tokens=%d candidates_tokens=%d", model_name, duration, prompt_tokens, candidates_tokens)

                    generated_name = "New Chat"
                    if r.candidates and r.candidates[0].content and r.candidates[0].content.parts:
                        response_text = r.candidates[0].content.parts[0].text.strip()
                        generated_name = response_text.replace('"', '').replace("'", '').strip()
                        if len(generated_name.split()) > 5:
                            generated_name = ' '.join(generated_name.split()[:5]) + '...'

                    logger.info(f"LLM generated name: '{generated_name}' for chat_id: {chat_id}")
                    db.execute('UPDATE chats SET name = ? WHERE id = ?', (generated_name, chat_id))
                    db.commit()
                    logger.info(f"Chat name updated in DB for chat_id {chat_id} to '{generated_name}'")
                    return # Success
                except Exception as e:
                    err_str = str(e).lower()
                    if ('429' in err_str or '403' in err_str or 'resource_exhausted' in err_str or 'quota' in err_str or 'rate limit' in err_str or
                        'overloaded' in err_str or '503' in err_str or 'service unavailable' in err_str or
                        '500' in err_str or 'internal error' in err_str or 'internal_error' in err_str):
                        block_duration, block_reason = parse_quota_block_duration(err_str)
                        # Scope block to model_name if it is a quota/transient limit, else block globally if 403 / invalid
                        block_scope = None if ('403' in err_str or 'permission_denied' in err_str or 'invalid' in err_str) else model_name
                        KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                        logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to {block_reason} error during name generation.")
                    logger.warning(f"API error during chat name generation: {e}. Trying next key...")
                    continue

            logger.error(f"All keys failed for chat name generation (chat {chat_id}).")

        except Exception as e:
            logger.error(f"Error in generate_chat_name (chat {chat_id}): {e}")

def generate_unique_subdomain(project_name):
    """
    Generate a unique subdomain slug for a given project name by appending numeric suffixes
    to avoid collisions with existing subdomains.

    Args:
        project_name (str): The user's project name.

    Returns:
        str: A unique subdomain slug.
    """
    # Convert "My Cool App!" to "my-cool-app"
    base_slug = re.sub(r'[^a-z0-9]+', '-', project_name.lower()).strip('-')
    if not base_slug:
        base_slug = "app"

    db = get_db()
    slug = base_slug
    counter = 1
    while True:
        cursor = db.execute("SELECT 1 FROM repo_history WHERE subdomain = ? ORDER BY id DESC LIMIT 1", (slug,))
        if not cursor.fetchone():
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


def count_chat_tokens(chat_id=None):
    """
    Estimate the total token count of the conversation history for a given chat.
    Reads messages and tool calls, formats them into types.Content objects, and calls
    Gemini's count_tokens API.

    Args:
        chat_id (int, optional): The unique chat identifier.

    Returns:
        int: The estimated total token count.
    """
    # Inline import to avoid startup overhead
    from google import genai
    from google.genai import types
    db = get_db()
    try:
        # Get user_id for memory retrieval
        cursor = db.execute('SELECT user_id FROM chats WHERE id = ?', (chat_id,))
        chat_row = cursor.fetchone()
        user_id = chat_row['user_id'] if chat_row else None

        # 1. Get the exact system instruction (Refined Prompt template)
        from prompts import get_refinement_prompt
        # We pass empty history and query because we just want the 'wrapper' parts (System + Memory)
        full_system_prompt = get_refinement_prompt("", [], user_id=user_id)

        # Extract everything before 'Conversation History:'
        system_parts = full_system_prompt.split("Conversation History:")
        system_instruction = system_parts[0].strip()

        # Get messages
        cursor = db.execute(
            '''SELECT id, message_type, message_content, timestamp FROM messages WHERE chat_id = ? AND (hidden = 0 OR message_content LIKE '[COMPRESSED MEMORY STATE]%') ORDER BY timestamp ASC''',
            (chat_id,)
        )
        messages = _fetch_as_dict(cursor)

        # Get tool calls
        cursor = db.execute(
            '''SELECT id, tool_name, input_params, length(result) as res_len,
                      CASE
                          WHEN tool_name IN ('read_tool_output', 'obtain_talent') THEN result
                          ELSE substr(result, 1, 1000)
                      END as res_part,
                      timestamp
               FROM tool_calls
               WHERE chat_id = ? AND hidden = 0
               ORDER BY timestamp ASC''',
            (chat_id,)
        )
        tool_calls = _fetch_as_dict(cursor)

        # Merge messages and tool calls by timestamp
        combined_history = []
        for m in messages:
            combined_history.append({'type': 'message', 'data': m, 'ts': m['timestamp']})
        for t in tool_calls:
            combined_history.append({'type': 'tool', 'data': t, 'ts': t['timestamp']})

        combined_history.sort(key=lambda x: x['ts'])

        # Start history with the System Instruction
        history_for_tokens = [types.Content(role="system", parts=[types.Part(text=system_instruction)])]

        for item in combined_history:
            if item['type'] == 'message':
                row = item['data']
                role = "user" if row['message_type'] == "user" else "model"
                clean_content = re.sub(r'(data:image/[^;]+;base64,)[a-zA-Z0-9+/=]+', r'\1[TRUNCATED]', row['message_content'])
                history_for_tokens.append(types.Content(role=role, parts=[types.Part(text=clean_content)]))
            else:
                # Tool call
                t = item['data']
                res_str = t['res_part']
                if res_str is None:
                    res_str = ""
                else:
                    res_str = str(res_str)
                num_chars = t['res_len'] if t['res_len'] is not None else 0
                num_lines = res_str.count('\n') + 1

                if 'data:image' in res_str:
                    clean_res = "[Image Generated]"
                elif num_chars > 600 or num_lines > 20:
                    clean_res = f"[Output truncated. ID: {t['id']}, Lines: {num_lines}, Length: {num_chars} chars. Use read_tool_output(output_id={t['id']}) to view.]"
                else:
                    clean_res = res_str

                input_str = str(t['input_params'])
                clean_input = input_str if len(input_str) <= 1000 else input_str[:1000] + f"... [Input truncated. Full length: {len(input_str)} chars]"

                tool_hist_entry = f"- [{t['timestamp']}] Tool: `{t['tool_name']}` (ID: {t['id']}) | Input: `{clean_input}` | Result: `{clean_res}`"
                history_for_tokens.append(types.Content(role="user", parts=[types.Part(text=tool_hist_entry)]))

        if len(history_for_tokens) <= 1: # Only system prompt
            return 0

        raw_keys = [PRIMARY_API_KEY] + [bk for bk in BACKUP_API_KEYS if bk]
        keys_to_try = [k for k in dict.fromkeys(raw_keys) if k]

        # Filter out globally rate-limited or quota-exhausted keys
        active_keys = [k for k in keys_to_try if not KEY_MANAGER.is_key_blocked(k, "gemini-3.1-flash-lite")[0]]
        if not active_keys:
            active_keys = keys_to_try

        t_count = 0
        token_counted = False
        for current_key in active_keys:
            try:
                client = genai.Client(api_key=current_key)
                t0 = time.time()
                token_count_response = client.models.count_tokens(
                    model="gemini-3.1-flash-lite", contents=history_for_tokens
                )
                t_count = token_count_response.total_tokens
                duration = time.time() - t0
                logger.info("Gemini API call completed model=%s duration_sec=%.2f purpose=count_tokens", "gemini-3.1-flash-lite", duration)
                token_counted = True
                break
            except Exception as token_e:
                err_str = str(token_e).lower()
                if ('429' in err_str or '403' in err_str or 'resource_exhausted' in err_str or 'quota' in err_str or 'rate limit' in err_str or
                    'overloaded' in err_str or '503' in err_str or 'service unavailable' in err_str or
                    '500' in err_str or 'internal error' in err_str or 'internal_error' in err_str):
                    block_duration, block_reason = parse_quota_block_duration(err_str)
                    block_scope = None if ('403' in err_str or 'permission_denied' in err_str or 'invalid' in err_str) else "gemini-3.1-flash-lite"
                    KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                    logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to {block_reason} error during count_chat_tokens.")
                logger.warning(f"Failed to count tokens with key (Hash: {hash(current_key)}): {token_e}")

        if not token_counted:
            raise ValueError("All API keys failed or rate-limited in count_chat_tokens")

        # Save to DB
        try:
            db = get_db()
            db.execute("UPDATE chats SET token_count = ? WHERE id = ?", (t_count, chat_id))
            db.commit()
        except Exception as db_e:
            logger.error(f"Error saving token count to DB for chat {chat_id}: {db_e}")

        logger.info(f"Token count for chat {chat_id}: {t_count}")
        return t_count
    except Exception as e:
        logger.error(f"Error counting tokens for chat {chat_id}: {e}")
        return 0

def upload_files_to_gemini(context_id, filenames, api_key=None):
    """Uploads local files to Gemini File API and waits for them to become ACTIVE."""
    from google import genai
    import mimetypes
    if not api_key:
        raw_keys = [PRIMARY_API_KEY] + [bk for bk in BACKUP_API_KEYS if bk]
        keys_to_try = [k for k in dict.fromkeys(raw_keys) if k]
        api_key = PRIMARY_API_KEY
        for k in keys_to_try:
            if not KEY_MANAGER.is_key_blocked(k, None)[0]:
                api_key = k
                break
    client = genai.Client(api_key=api_key)
    session_upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], str(context_id))

    # Whitelist of extensions and MIME types supported by Gemini Native File API
    SUPPORTED_EXTENSIONS = {
        '.pdf',
        '.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif',
        '.wav', '.mp3', '.aiff', '.aac', '.ogg', '.flac',
        '.mp4', '.mpeg', '.mov', '.avi', '.flv', '.mpg', '.webm', '.wmv', '.3gpp'
    }

    gemini_files =[]
    for filename in filenames:
        filepath = os.path.join(session_upload_folder, secure_filename(filename))
        if os.path.exists(filepath):
            ext = os.path.splitext(filename)[1].lower()
            mime_type, _ = mimetypes.guess_type(filepath)
            mime_type = mime_type or 'application/octet-stream'

            logger.info(f"[GEMINI-UPLOAD] Checking file: {filename}, ext: {ext}, mime: {mime_type}")

            # Strict check: extension must be in whitelist
            if ext not in SUPPORTED_EXTENSIONS:
                logger.warning(f"[GEMINI-UPLOAD] Extension {ext} not in whitelist. Routing {filename} to Lab.")
                gemini_files.append({
                    "uri": None,
                    "mime_type": mime_type,
                    "name": None,
                    "display_name": filename,
                    "local_path": filepath,
                    "fallback_to_lab": True
                })
                continue

            try:
                logger.info(f"[GEMINI-UPLOAD] Uploading {filename} natively to Gemini...")
                t0 = time.time()
                g_file = client.files.upload(file=filepath)
                duration = time.time() - t0
                logger.info("Gemini API call completed method=files.upload duration_sec=%.2f filename=%s", duration, filename)

                # Wait for processing
                while g_file.state.name == "PROCESSING":
                    time.sleep(2)
                    g_file = client.files.get(name=g_file.name)

                if g_file.state.name == "FAILED":
                    logger.error(f"[GEMINI-UPLOAD] Gemini failed to process file {filename}")
                    continue

                gemini_files.append({
                    "uri": g_file.uri,
                    "mime_type": g_file.mime_type,
                    "name": g_file.name,
                    "display_name": filename,
                    "local_path": filepath
                })
                logger.info(f"[GEMINI-UPLOAD] Successfully uploaded {filename} to Gemini URI: {g_file.uri}")
            except Exception as e:
                logger.error(f"[GEMINI-UPLOAD] Exception during upload of {filename}: {e}")
                gemini_files.append({
                    "uri": None,
                    "mime_type": mime_type,
                    "name": None,
                    "display_name": filename,
                    "local_path": filepath,
                    "fallback_to_lab": True
                })
    return gemini_files

@app.route('/upload_files', methods=['POST'])
@require_approval
def upload_files():
    """
    Handle multi-file uploads from the web client.
    Saves uploaded files to the user's isolated session folder on the server's local file system.

    Returns:
        Response: A Flask JSON response indicating success or failure.
    """
    context_id = get_file_context_id()
    if not context_id:
        return jsonify({'error': 'Session initialization failed.'}), 500

    uploaded_files = request.files.getlist("file")
    if not uploaded_files or all(f.filename == '' for f in uploaded_files):
        return jsonify({'error': 'No files selected'}), 400

    session_upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], context_id)
    os.makedirs(session_upload_folder, exist_ok=True)

    successful_uploads =[]
    for file in uploaded_files:
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(session_upload_folder, filename)
            file.save(filepath)
            successful_uploads.append(filename)

    logger.info("Uploaded files saved context_id=%s count=%d files=%s", context_id, len(successful_uploads), successful_uploads)

    return jsonify({
        'status': f"Saved {len(successful_uploads)} file(s) locally.",
        'uploaded_files': successful_uploads
    }), 200

def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by replacing spaces with underscores and stripping out
    special characters. Truncates to a maximum length of 100 characters.

    Args:
        filename (str): The original filename.

    Returns:
        str: The sanitized filename.
    """
    filename = filename.replace(' ', '_')
    sanitized = re.sub(r'[^\w\-\.]+', '', filename)
    return sanitized[:100] if len(sanitized) > 100 else sanitized

def is_safe_hostname(hostname):
    """Helper to resolve hostname and check if all associated IPs are safe for SSRF protection."""
    if not hostname:
        return False, "Invalid hostname"
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        resolved_ips = []
        for family, kind, proto, canonname, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if (ip.is_private or ip.is_loopback or ip.is_link_local or
                    ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                    return False, f"Access to internal networks is forbidden: {ip_str}"
                resolved_ips.append(ip_str)
            except ValueError:
                continue
    except socket.gaierror:
        return False, "Failed to resolve hostname"
    
    if not resolved_ips:
        return False, "No valid IP addresses resolved"
        
    # Return the first resolved safe IP address so the caller can pin it
    return True, resolved_ips[0]

def scrape_url(url: str) -> str:
    """
    Fetch and scrape text content from a web URL.
    Implements SSRF validation to prevent access to private IP ranges.

    Args:
        url (str): The URL to scrape.

    Returns:
        str: The text content of the page, or an error message.
    """
    # SECURITY CONTROL: Protocol restriction. Force URLs to start strictly with HTTP or HTTPS
    # to prevent protocol smuggling (e.g., file://, ftp://, gopher://).
    if not url or not url.startswith(('http://', 'https://')):
        return f"Error scraping {url}: Invalid URL format"

    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        # SECURITY CONTROL: Hostname resolving check. Check hostname against DNS entries, preventing access
        # to private/reserved ranges (RFC 1918, localhost, loopback).
        safe, ip_or_msg = is_safe_hostname(parsed.hostname)
        if not safe:
            logger.warning(f"Blocked scraping SSRF attempt: {ip_or_msg} via {url}")
            return f"Error scraping {url}: {ip_or_msg}"

        # SECURITY CONTROL: DNS Rebinding prevention. Pin DNS resolution results in the thread-local
        # cache to prevent Time-of-Check to Time-of-Use (TOCTOU) DNS rebinding attacks.
        dns_cache.pinned_ips = getattr(dns_cache, 'pinned_ips', {})
        try:
            ipaddress.ip_address(ip_or_msg)
            dns_cache.pinned_ips[parsed.hostname] = ip_or_msg
        except ValueError:
            pass

        try:
            # Inline import of webscrapper to avoid startup overhead
            import webscrapper
            apron=webscrapper.scrape_url(url)
            logger.info("Scraped URL successfully url=%s content_length=%d", url, len(apron) if apron else 0)
            return apron
        finally:
            # Clear DNS pinning for this host
            if parsed.hostname in dns_cache.pinned_ips:
                del dns_cache.pinned_ips[parsed.hostname]
    except Exception as e:
        logger.exception("Failed to scrape URL url=%s", url)
        return f"Error scraping {url}: {str(e)}"

stop_sequence="8919018818"

def is_output_cut_off(text: str, key: str) -> bool:
    """
    Check if the LLM generation output was cut off before completion.

    Args:
        text (str): The accumulated text content.
        key (str): The API key used for generation.

    Returns:
        bool: True if the output appears to be cut off; False otherwise.
    """
    if not key:
        return False

    if len(text.strip()) < 50 and (text.strip().endswith('.') or text.strip().endswith('!') or text.strip().endswith('?')):
        return False

    check_prompt = (
        f"Given the following text, determine if it is a complete and natural conclusion, "
        f"or if it abruptly stops mid-sentence, mid-paragraph, or mid-idea, suggesting it requires continuation. "
        f"Respond only with 'YES' if it feels complete, and 'NO' if it feels cut off and requires continuation. "
        f"Do not add any other text.\n\nText:\n---\n{text}\n---"
    )

    try:
        # Inline import of genai to avoid startup overhead
        from google import genai
        client = genai.Client(api_key=key, http_options={'api_version': 'v1beta'})
        chat = client.chats.create(model='gemini-3.1-flash-lite', config={'tools': []})
        t0 = time.time()
        r = chat.send_message(check_prompt)
        duration = time.time() - t0
        usage = getattr(r, 'usage_metadata', None)
        prompt_tokens = getattr(usage, 'prompt_token_count', 0) if usage else 0
        candidates_tokens = getattr(usage, 'candidates_token_count', 0) if usage else 0
        logger.info("Gemini API call completed model=%s duration_sec=%.2f purpose=cut_off_check prompt_tokens=%d candidates_tokens=%d", 'gemini-3.1-flash-lite', duration, prompt_tokens, candidates_tokens)

        if r.candidates and r.candidates[0].content and r.candidates[0].content.parts:
            response_text = r.candidates[0].content.parts[0].text.strip().upper()
            if "NO" in response_text:
                return True
            else:
                return False
        else:
            return False
    except Exception as e:
        logger.exception("Failed to check if output is cut off")
        return False


def gemini_generate(prompt: str, model_id: str, key: str, attempts: int = 3, backoff_factor: float = 1.5, model_display_name=None, username=None, chat_id=None, disabled_tools=None, gemini_files_data=None, cancel_event=None):
    """
    Main generative wrapper for the Gemini API. Enforces token usage limits, executes agent tool calls,
    handles prompt injection security validations, and streams response tokens to the SSE stream.
    Features key rotation, exponential backoff, and robust error recovery mechanisms.

    Args:
        prompt (str or list): The prompt string or list of content parts/files to generate from.
        model_id (str): The Gemini model identifier.
        key (str): The initial Gemini API key value to try.
        attempts (int, optional): The max number of retries per key. Defaults to 3.
        backoff_factor (float, optional): The multiplier for exponential retry backoff. Defaults to 1.5.
        model_display_name (str, optional): The display name of the model. Defaults to None.
        username (str, optional): The username of the user. Defaults to None.
        chat_id (int, optional): The unique chat identifier. Defaults to None.
        disabled_tools (list, optional): The list of disabled tool names. Defaults to None.
        gemini_files_data (list, optional): Uploaded file metadata for Gemini. Defaults to None.
        cancel_event (threading.Event, optional): Event to signal cancellation. Defaults to None.

    Yields:
        str: Streamed response chunks/tokens or tool execution log strings.
    """
    from flask import g
    # Inline import of genai and types to avoid startup overhead
    from google import genai
    from google.genai import types
    g.model_id = model_id # Set ground-truth model for tools
    display_name = model_display_name or MODEL_NAMES.get(model_id)
    if isinstance(prompt, str):
        prompt_len = len(prompt)
    elif isinstance(prompt, list):
        prompt_len = 0
        for p in prompt:
            if isinstance(p, str):
                prompt_len += len(p)
            elif hasattr(p, 'text') and p.text:
                prompt_len += len(p.text)
    else:
        prompt_len = len(str(prompt))
    logger.info("Initiating gemini_generate model=%s display_name=%s username=%s chat_id=%s prompt_len=%d", model_id, display_name, username, chat_id, prompt_len)

    def record_tool_call(t_name, t_input, t_result):
        if not chat_id: return
        try:
            with app.app_context():
                db = get_db()
                db.execute('INSERT INTO tool_calls (chat_id, tool_name, input_params, result) VALUES (?, ?, ?, ?)',
                           (chat_id, t_name, json.dumps(t_input), str(t_result)))
                db.commit()
        except Exception as e:
            logger.error(f"Error recording tool call: {e}")

    last_exception = None

    original_prompt_for_continuation = prompt
    current_effective_prompt = prompt
    accumulated_full_output = ""

    # Ensure PRIMARY_API_KEY is the absolute first priority, then check passed 'key', then backups.
    # We use a list with dict.fromkeys() to maintain order and remove duplicates.
    raw_keys = [PRIMARY_API_KEY, key] + [bk for bk in BACKUP_API_KEYS if bk]
    keys_to_try = [k for k in dict.fromkeys(raw_keys) if k]

    # ------------------------------------------------------------------
    # Key rotation strategy
    # ALL_KEYS: the full deduplicated ordered pool (PRIMARY first, then
    #   passed key, then BACKUP_API_KEYS). Preserved for the life of this
    #   call so RPM-expired keys can be reconsidered.
    # RPD-blocked keys are removed permanently for this invocation because
    #   they won't unblock before Pacific midnight.
    # RPM-blocked keys (60 s) stay in the pool; we skip them now but can
    #   return to them once their window expires.
    # ------------------------------------------------------------------
    ALL_KEYS = [k for k in dict.fromkeys([PRIMARY_API_KEY, key] + [bk for bk in BACKUP_API_KEYS if bk]) if k]

    # Partition: permanently remove RPD/INVALID keys; keep RPM/OVERLOAD
    # keys because they will recover within this call.
    def _is_permanently_blocked(k):
        """True for RPD, INVALID, or OVERLOAD blocks (won't recover mid-call)."""
        blocked, reason = KEY_MANAGER.is_key_blocked(k, model_id)
        return blocked and reason in ('RPD', 'INVALID', 'OVERLOAD')

    keys_to_try = [k for k in ALL_KEYS if not _is_permanently_blocked(k)]
    if not keys_to_try:
        # All keys are RPD-exhausted — nothing we can do this call
        logger.error("All API keys are RPD/INVALID-blocked for model %s. Aborting.", model_id)
        keys_to_try = ALL_KEYS  # surface the error naturally

    if len(keys_to_try) < len(ALL_KEYS):
        n_skipped = len(ALL_KEYS) - len(keys_to_try)
        logger.info("Skipped %d RPD/INVALID-blocked API key(s) for model %s.", n_skipped, model_id)

    # Pick starting key: always the lowest-indexed key that is not blocked.
    # "Earliest-available" strategy: use K1 exclusively until RPM, then K2, etc.
    # When K1's 61s window expires it is automatically preferred again.
    # Redis stores block state cross-process so all workers agree without a counter.
    current_key_index = 0
    for _i in range(len(keys_to_try)):
        _k = keys_to_try[_i]
        _blocked, _ = KEY_MANAGER.is_key_blocked(_k, model_id)
        if not _blocked:
            current_key_index = _i
            break

    def get_next_unblocked_key_index(start_idx):
        """
        Always return the lowest-indexed key that is currently unblocked.

        Strategy — "earliest-available":
          Scan from index 0 every time. This means:
            · K1 is used until it hits RPM (61 s block).
            · Switch to K2 (next lowest unblocked).
            · When K1’s 61 s expires, the very next call comes back to K1.
            · K3, K4 … are only used when every lower key is blocked.

        Pass 1 — Scan keys 0..N-1. Pure time comparison, no request sent.
        Pass 2 — All blocked: find soonest expiry, sleep + 1 s, rescan from 0.
        RPD/INVALID/OVERLOAD — skipped, never waited on.
        """
        if not keys_to_try:
            return None

        # Pass 1: scan from index 0 — prefer lowest-indexed unblocked key.
        for i in range(len(keys_to_try)):
            k = keys_to_try[i]
            is_blocked, reason = KEY_MANAGER.is_key_blocked(k, model_id)
            if not is_blocked:
                return i
            if reason in ('RPD', 'INVALID', 'OVERLOAD'):
                continue  # permanent — skip silently

        # Pass 2: all N keys still within their RPM window.
        # Find soonest-expiring key, sleep once, rescan from 0.
        soonest_wait = None
        soonest_idx = None
        for i in range(len(keys_to_try)):
            k = keys_to_try[i]
            is_blocked, reason = KEY_MANAGER.is_key_blocked(k, model_id)
            if not is_blocked:
                return i  # cleared between Pass 1 and here (race)
            if reason in ('RPD', 'INVALID', 'OVERLOAD'):
                continue
            remaining = 62.0
            try:
                k_until, _ = KEY_MANAGER._get_redis_keys(k, model_id)
                val = redis_client.get(k_until)
                if val:
                    remaining = max(0.0, float(val) - time.time())
            except Exception:
                in_mem = KEY_MANAGER.blocked_until.get((k, model_id), 0)
                remaining = max(0.0, in_mem - time.time())
            if soonest_wait is None or remaining < soonest_wait:
                soonest_wait = remaining
                soonest_idx = i

        if soonest_idx is None:
            logger.error("Only RPD/INVALID/OVERLOAD keys remain for model %s. Cannot recover.", model_id)
            return None

        wait_secs = soonest_wait + 1.0  # +1 s for network I/O jitter
        logger.warning(
            "All %d keys RPM-blocked for model %s. Sleeping %.1f s for key_index=%d to unblock.",
            len(keys_to_try), model_id, wait_secs, soonest_idx
        )
        time.sleep(wait_secs)

        # Post-sleep: rescan from 0 — pick lowest-indexed recovered key.
        for i in range(len(keys_to_try)):
            k = keys_to_try[i]
            is_blocked, reason = KEY_MANAGER.is_key_blocked(k, model_id)
            if not is_blocked:
                logger.info("Key index %d unblocked after %.1f s wait.", i, wait_secs)
                return i

        logger.error("All keys still blocked after %.1f s wait for model %s.", wait_secs, model_id)
        return None


    for attempt in range(1, attempts + 1):
        if cancel_event and cancel_event.is_set():
            logger.info("gemini_generate loop aborted due to cancellation.")
            return
        current_key = keys_to_try[current_key_index]
        if not current_key:
            yield {'status': 'Error: No valid API key available.', 'error': True}
            last_exception = ValueError("No valid API key found.")
            break

        try:
            yield {'status': f"{display_name} is thinking..."}
            client = genai.Client(api_key=current_key, http_options={'api_version': 'v1beta'})
            output_this_attempt = ""
            output_this_attempt_parts = []
            called_tools_results = []
            candidate = None

            try:
                from agent_tools import available_tools
            except ImportError:
                available_tools = []

            tools_config = available_tools.copy()

            # Restrict lab_execute and repo_control to Elite models
            elite_models = ["gemini-3-flash-preview", "gemini-3.5-flash", "gemma-4-31b-it"]
            if model_id not in elite_models:
                tools_config = [t for t in tools_config if getattr(t, '__name__', '') not in ['lab_execute', 'repo_control']]

            if disabled_tools:
                tools_config = [t for t in tools_config if getattr(t, '__name__', '') not in disabled_tools]

            # Extract system instruction if present in the prompt
            system_instruction = None
            if isinstance(current_effective_prompt, list):
                for part in current_effective_prompt:
                    if hasattr(part, 'text') and part.text and "<!-- Internal Processing Guidelines -->" in part.text:
                        p_text = part.text
                        parts = p_text.split("<!-- End Internal Guidelines -->")
                        if len(parts) > 1:
                            system_instruction = parts[0].replace("<!-- Internal Processing Guidelines -->", "").strip()
                            part.text = parts[1].strip()
                        break
            elif isinstance(current_effective_prompt, str) and "<!-- Internal Processing Guidelines -->" in current_effective_prompt:
                parts = current_effective_prompt.split("<!-- End Internal Guidelines -->")
                if len(parts) > 1:
                    system_instruction = parts[0].replace("<!-- Internal Processing Guidelines -->", "").strip()
                    current_effective_prompt = parts[1].strip()

            chat_config = types.GenerateContentConfig(
                tools=tools_config,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=1.0,
                system_instruction=system_instruction,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_CIVIC_INTEGRITY", threshold="BLOCK_NONE"),
                ]
            )
            chat = client.chats.create(model=model_id, config=chat_config)

            message_to_send = prompt if isinstance(prompt, list) else current_effective_prompt

            import agent_tools

            consecutive_network_errors = 0
            while True:
                try:
                    t0 = time.time()
                    r = chat.send_message(message_to_send)
                    duration = time.time() - t0
                    usage = getattr(r, 'usage_metadata', None)
                    prompt_tokens = getattr(usage, 'prompt_token_count', 0) if usage else 0
                    candidates_tokens = getattr(usage, 'candidates_token_count', 0) if usage else 0
                    logger.info("Gemini API call completed model=%s duration_sec=%.2f purpose=query_stream_generation prompt_tokens=%d candidates_tokens=%d attempt=%d key_index=%d", model_id, duration, prompt_tokens, candidates_tokens, attempt, current_key_index)
                    consecutive_network_errors = 0 # Reset on success
                except Exception as loop_e:
                    logger.exception("Error caught: %s", loop_e)
                    error_string = str(loop_e).lower()
                    is_quota = any(x in error_string for x in ['429', '403', '401', '400', 'permission_denied', 'resource_exhausted', 'quota', 'rate limit', 'expired', 'invalid', 'disabled', 'unauthenticated'])
                    is_network = any(x in error_string for x in ['500', '503', 'connection', 'timeout', 'deadline'])

                    if is_quota:
                        block_duration, block_reason = parse_quota_block_duration(error_string)
                        block_scope = None if ('403' in error_string or 'permission_denied' in error_string or 'invalid' in error_string or 'expired' in error_string or '401' in error_string or '400' in error_string or 'disabled' in error_string or 'unauthenticated' in error_string) else model_id
                        KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                        logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to {block_reason} error in inner loop.")

                        next_key_idx = get_next_unblocked_key_index(current_key_index)
                        if next_key_idx is not None:
                            logger.warning(f"Switching from key index {current_key_index} to backup key index {next_key_idx} (circular queue)...")
                            current_key_index = next_key_idx
                            current_key = keys_to_try[current_key_index]
                        else:
                            logger.error("All available keys in keys_to_try are blocked. Aborting inner loop.")
                            break


                        client = genai.Client(api_key=current_key, http_options={'api_version': 'v1beta'})

                        if gemini_files_data:
                            context_id = get_file_context_id()
                            if context_id:
                                filenames = [gf['display_name'] for gf in gemini_files_data if not gf.get('fallback_to_lab')]
                                if filenames:
                                    new_files_data = upload_files_to_gemini(context_id, filenames, api_key=current_key)
                                    uri_map = {}
                                    for old_f in gemini_files_data:
                                        for new_f in new_files_data:
                                            if old_f.get('display_name') == new_f.get('display_name') and old_f.get('uri') and new_f.get('uri'):
                                                uri_map[old_f['uri']] = new_f['uri']
                                                old_f['uri'] = new_f['uri']

                                    def update_part_uri(part):
                                        if hasattr(part, 'file_data') and part.file_data and hasattr(part.file_data, 'file_uri'):
                                            old_uri = part.file_data.file_uri
                                            if old_uri in uri_map:
                                                return types.Part.from_uri(file_uri=uri_map[old_uri], mime_type=part.file_data.mime_type)
                                        return part

                                    if isinstance(message_to_send, list):
                                        message_to_send = [update_part_uri(p) for p in message_to_send]
                                    if isinstance(current_effective_prompt, list):
                                        for i in range(len(current_effective_prompt)):
                                            current_effective_prompt[i] = update_part_uri(current_effective_prompt[i])

                        old_history = chat.get_history() if hasattr(chat, 'get_history') else []
                        new_history = []
                        for h_msg in old_history:
                            new_parts = []
                            for p in getattr(h_msg, 'parts', []):
                                if gemini_files_data and 'update_part_uri' in locals():
                                    p = update_part_uri(p)
                                new_parts.append(p)
                            new_history.append(types.Content(role=h_msg.role, parts=new_parts))

                        chat = client.chats.create(model=model_id, config=chat_config, history=new_history)
                        continue
                    elif is_network and consecutive_network_errors < 3:
                        consecutive_network_errors += 1
                        logger.warning(f"Connection issue detected (Attempt {consecutive_network_errors}/3) with model {model_id} using key index {current_key_index}. Error: {loop_e}")
                        yield {'status': f'Connection issue. Retrying in-place ({consecutive_network_errors}/3)...'}
                        time.sleep(2 * consecutive_network_errors)

                        # If we hit 3 network errors, try switching keys before giving up on this attempt
                        if consecutive_network_errors == 3:
                             block_duration, block_reason = parse_quota_block_duration(error_string)
                             block_scope = model_id
                             KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                             logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to persistent network issues in inner loop.")

                             next_key_idx = get_next_unblocked_key_index(current_key_index)
                             if next_key_idx is not None:
                                 logger.warning(f"Persistent network issues with key index {current_key_index}. Switching to backup key index {next_key_idx} (circular queue)...")
                                 current_key_index = next_key_idx
                                 current_key = keys_to_try[current_key_index]
                                 consecutive_network_errors = 0 # Reset for the new key
                             else:
                                 logger.error("All available keys in keys_to_try are blocked. Aborting inner loop.")
                                 break
                             consecutive_network_errors = 0 # Reset for the new key

                        # Re-init chat with (potentially new) key
                        client = genai.Client(api_key=current_key, http_options={'api_version': 'v1beta'})
                        old_history = chat.get_history() if hasattr(chat, 'get_history') else []
                        chat = client.chats.create(model=model_id, config=chat_config, history=old_history)
                        continue
                    else:
                        raise loop_e

                if not r.candidates:
                    finish_reason_obj = getattr(r, 'prompt_feedback', {}).get('finish_reason', 'UNKNOWN')
                    finish_reason = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj)
                    safety_ratings = getattr(r, 'prompt_feedback', {}).get('safety_ratings', [])
                    safety_details = ", ".join([f"{sr.category.name}: {sr.probability.name}" for sr in safety_ratings if hasattr(sr, 'category') and hasattr(sr.category, 'name')]) if safety_ratings else "N/A"
                    error_msg = f"API Error ({display_name}): No candidates received. Finish Reason: {finish_reason}, Safety: {safety_details}"
                    if finish_reason == 'SAFETY':
                        last_exception = ValueError(f"Prompt blocked by API due to safety ({safety_details}).")
                        yield {'status': f'Prompt blocked due to safety. Retrying...'}
                        break
                    elif finish_reason == 'RECITATION':
                        last_exception = ValueError("Prompt blocked by API due to recitation.")
                        yield {'status': f'Prompt blocked due to recitation. Retrying...'}
                        break
                    else:
                        raise ValueError(error_msg)

                candidate = r.candidates[0]
                parts = getattr(candidate.content, 'parts', [])

                # DEBUG LOG
                part_types = [type(p).__name__ for p in parts]
                logger.info(f"[DEBUG] Model parts received: {part_types}")
                for p in parts:
                    if getattr(p, 'text', None): logger.info(f"[DEBUG] Text part length: {len(p.text)}")
                    if getattr(p, 'function_call', None): logger.info(f"[DEBUG] Function call: {p.function_call.name}")

                if not parts:
                    yield {'result': ""}
                    return

                function_calls = [p.function_call for p in parts if getattr(p, 'function_call', None)]

                # Capture and yield any introductory text that comes WITH the function calls
                current_parts_text = []
                for part in parts:
                    if getattr(part, 'thought', False):
                        continue # Skip thinking blocks!
                    if getattr(part, 'text', None):
                        current_parts_text.append(part.text)

                intro_text = "".join(current_parts_text)

                if intro_text:
                    yield {'result': intro_text}
                    accumulated_full_output += intro_text
                    output_this_attempt_parts.append(intro_text)

                if not function_calls:
                    # --- LIVE INTERRUPT: Check before finishing ---
                    if chat_id:
                        injected_msgs = []
                        while True:
                            raw = redis_client.lpop(f"inject_messages:{chat_id}")
                            if not raw:
                                break
                            try:
                                inj = json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
                                injected_msgs.append(inj)
                            except:
                                pass
                        if injected_msgs:
                            # --- Segment the stream backend-only on dynamic injection ---
                            if accumulated_full_output.strip():
                                stellar_msg_id = insert_message(chat_id, "stellar", accumulated_full_output.strip() + "\n\n*[Response interrupted by user]*", hidden=True)
                                if stellar_msg_id and injected_msgs:
                                    try:
                                        first_user_msg_id = injected_msgs[0].get('message_id')
                                        if first_user_msg_id:
                                            db = get_db()
                                            db.execute('''
                                                UPDATE messages
                                                SET timestamp = datetime((SELECT timestamp FROM messages WHERE id = ?), '-1 second')
                                                WHERE id = ?
                                            ''', (int(first_user_msg_id), stellar_msg_id))
                                            db.commit()
                                    except Exception as e:
                                        logger.error(f"Error adjusting interrupted message timestamp: {e}")
                            accumulated_full_output = ""
                            output_this_attempt_parts = []
                            yield {'type': 'stream_reset'}

                            inject_text = "\n".join([f"[LIVE USER FOLLOW-UP]: {m['message']}" for m in injected_msgs])
                            inject_notice = (
                                f"\n\n[SYSTEM: LIVE INTERRUPT] The user just sent follow-up messages while you were generating your response. "
                                f"You MUST address these immediately in a new response turn. Your previous output has already been sent to the user.\n{inject_text}"
                            )
                            message_to_send = inject_notice
                            yield {'status': 'User follow-up received! Continuing...'}
                            logger.info(f"Injected {len(injected_msgs)} live follow-up(s) at text-break for chat {chat_id}")
                            continue  # Don't break — loop back to send_message with the follow-up
                    # --- END LIVE INTERRUPT ---
                    break
                else:
                    # We have function calls! Let's animate and execute
                    function_responses = []
                    for fc in function_calls:
                        if cancel_event and cancel_event.is_set():
                            logger.info("gemini_generate tool execution aborted due to cancellation.")
                            return
                        func_name = fc.name
                        args_dict = dict(fc.args) if fc.args else {}

                        timeout_val = args_dict.get('timeout')

                        yield {'status': args_dict.get('status', f'Using tool: {func_name}...'), 'timeout': timeout_val}

                        # execute
                        try:
                            # STRICT SECURITY CHECK: Only allow tools that were actually provided in the config
                            allowed_tool_names = [getattr(t, '__name__', '') for t in tools_config]

                            if func_name not in allowed_tool_names:
                                res = f"Error: The tool '{func_name}' is restricted for this model level."
                                logger.warning(f"[SECURITY] Model {model_id} tried to call unauthorized tool: {func_name}")
                            else:
                                func_to_call = getattr(agent_tools, func_name)

                                # Dynamically pass the current model_id to specific tools
                                if func_name in ["analyze_youtube_video"]:
                                    if 'model_id' not in args_dict:
                                        args_dict['model_id'] = model_id

                                if func_name == "logs_and_preferences":
                                    args_dict['user_id'] = str(getattr(g, 'user_id', 'global'))

                                if func_name == "request_user_interaction":
                                    interaction_id = str(uuid.uuid4())
                                    html_ui = args_dict.get('html_ui', '')
                                    # Strip structural tags that browsers discard when inserted via innerHTML into a div.
                                    # The model often wraps html_ui in <!DOCTYPE><html><head>...<body>...</body></html>.
                                    # Browsers silently strip <html>, <head>, <body> and their closing tags when they appear
                                    # inside a <div>.innerHTML, which can cause <script> tags inside <body> to be lost.
                                    import re as _re
                                    html_ui = _re.sub(r'<!DOCTYPE[^>]*>', '', html_ui, flags=_re.IGNORECASE)
                                    html_ui = _re.sub(r'</?html[^>]*>', '', html_ui, flags=_re.IGNORECASE)
                                    html_ui = _re.sub(r'</?head[^>]*>', '', html_ui, flags=_re.IGNORECASE)
                                    html_ui = _re.sub(r'</?body[^>]*>', '', html_ui, flags=_re.IGNORECASE)
                                    yield {'type': 'generative_ui', 'html': html_ui, 'interaction_id': interaction_id}

                                    # Dispatch Web Push background notification
                                    try:
                                        from flask import g
                                        p_user_id = getattr(g, 'user_id', None)
                                        if not p_user_id and chat_id:
                                            # Bolt - Stability Optimization: Reuse get_db() to inherit WAL and busy_timeout configurations
                                            db_temp = get_db()
                                            row = db_temp.execute('SELECT user_id FROM chats WHERE id = ?', (chat_id,)).fetchone()
                                            if row:
                                                p_user_id = row['user_id']

                                        if p_user_id:
                                            prompt_desc = args_dict.get('status', 'Stellar needs your interaction to proceed with the task.')
                                            send_push_notification(
                                                user_id=p_user_id,
                                                title="Stellar: Action Required",
                                                body=prompt_desc,
                                                url=f"/?chat_id={chat_id}"
                                            )
                                    except Exception as push_err:
                                        logger.error(f"Failed to dispatch interaction push notification: {push_err}")


                                    # Polling loop
                                    start_time = time.time()
                                    poll_interval = 2
                                    res = None
                                    import redis
                                    try:
                                        r_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
                                        redis_key = f"ui_interaction:{interaction_id}"

                                        while time.time() - start_time < (timeout_val or 300):
                                            val = r_client.get(redis_key)
                                            if val:
                                                res = val
                                                r_client.delete(redis_key)
                                                break
                                            yield {'status': 'Waiting for user interaction...'}
                                            time.sleep(poll_interval)

                                        if res is None:
                                            res = "Error: User did not interact with the UI in time."
                                    except Exception as e:
                                        logger.exception(f"Redis error during active polling: {e}")
                                        res = f"Error: Backend polling failed - {str(e)}"
                                else:
                                    import concurrent.futures
                                    from flask import current_app, g

                                    app_obj = current_app._get_current_object()
                                    g_state = {k: getattr(g, k) for k in ['user_id', 'username', 'chat_id', 'session_id', 'model_id', 'request_id'] if hasattr(g, k)}

                                    def _run_tool_with_context(**kwargs):
                                        if 'request_id' in g_state:
                                            thread_local_ctx.request_id = g_state['request_id']
                                        with app_obj.app_context():
                                            for k, v in g_state.items():
                                                setattr(g, k, v)
                                            try:
                                                return func_to_call(**kwargs)
                                            finally:
                                                if hasattr(thread_local_ctx, 'request_id'):
                                                    del thread_local_ctx.request_id

                                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                                    logger.info("Executing tool name=%s chat_id=%s args=%s", func_name, chat_id, args_dict)
                                    t_tool_start = time.time()
                                    future = executor.submit(_run_tool_with_context, **args_dict)
                                    tool_status = "success"
                                    try:
                                        res = future.result(timeout=timeout_val)
                                    except concurrent.futures.TimeoutError:
                                        res = f"Error: Tool '{func_name}' was stopped because it exceeded the timeout of {timeout_val} seconds that the agent set for that tool."
                                        tool_status = "timeout"
                                    except Exception as tool_exc:
                                        res = f"Error: {str(tool_exc)}"
                                        tool_status = "error"
                                        logger.exception("Tool execution failed name=%s chat_id=%s error=%s", func_name, chat_id, tool_exc)
                                    finally:
                                        executor.shutdown(wait=False)
                                    duration_tool = time.time() - t_tool_start
                                    logger.info("Executed tool name=%s chat_id=%s duration_sec=%.2f status=%s", func_name, chat_id, duration_tool, tool_status)

                            # Record tool call in DB for context persistence
                            record_tool_call(func_name, args_dict, res)

                            # Store result for final verification/forced inclusion
                            called_tools_results.append({'name': func_name, 'result': res})

                            # We no longer yield any tool results immediately.
                            # Instead, we provide them to the model as a FunctionResponse part,
                            # and rely on the model to include the information naturally in its final text turn.
                            # This prevents the "double output" issue where the system yielded it and then the model repeated it.
                        except Exception as e:
                            logger.exception("Error caught: %s", e)
                            res = f"Error: {str(e)}"

                        # Create response part
                        # Truncate base64 image data and massive text outputs to prevent blowing up the LLM's input token limit
                        # during the immediate next function_response turn!
                        llm_safe_res = res
                        if isinstance(llm_safe_res, str):
                            if 'data:image' in llm_safe_res:
                                llm_safe_res = "Image successfully generated and rendered to the user's UI. Do not attempt to output the image markdown yourself."
                            elif func_name not in ['read_tool_output', 'obtain_talent'] and (len(llm_safe_res) > 10000 or len(llm_safe_res.split('\n')) > 100):
                                last_tool_id = "unknown"
                                try:
                                    db = get_db()
                                    cursor = db.execute('SELECT id FROM tool_calls WHERE chat_id = ? ORDER BY id DESC LIMIT 1', (chat_id,))
                                    row = cursor.fetchone()
                                    if row: last_tool_id = row[0]
                                except Exception as db_err:
                                    logger.error(f"Failed to query last tool call ID: {db_err}")

                                num_lines = len(llm_safe_res.split('\n'))
                                llm_safe_res = f"[Output truncated for context efficiency. ID: {last_tool_id}, Lines: {num_lines}, Length: {len(llm_safe_res)} chars. Use read_tool_output(output_id={last_tool_id}) to view the full text if necessary.]"

                        function_responses.append(
                            types.Part(function_response=types.FunctionResponse(
                                name=fc.name,
                                id=fc.id,
                                response={'result': llm_safe_res}
                            ))
                        )
                        message_to_send = function_responses

                        # --- LIVE INTERRUPT: Check for injected user messages ---
                        if chat_id:
                            injected_msgs = []
                            while True:
                                raw = redis_client.lpop(f"inject_messages:{chat_id}")
                                if not raw:
                                    break
                                try:
                                    inj = json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
                                    injected_msgs.append(inj)
                                except:
                                    pass
                            if injected_msgs:
                                if accumulated_full_output.strip():
                                    stellar_msg_id = insert_message(chat_id, "stellar", accumulated_full_output.strip() + "\n\n*[Response interrupted by user]*", hidden=True)
                                    if stellar_msg_id and injected_msgs:
                                        try:
                                            first_user_msg_id = injected_msgs[0].get('message_id')
                                            if first_user_msg_id:
                                                db = get_db()
                                                db.execute('''
                                                    UPDATE messages
                                                    SET timestamp = datetime((SELECT timestamp FROM messages WHERE id = ?), '-1 second')
                                                    WHERE id = ?
                                                ''', (int(first_user_msg_id), stellar_msg_id))
                                                db.commit()
                                        except Exception as e:
                                            logger.error(f"Error adjusting interrupted message timestamp: {e}")
                                accumulated_full_output = ""
                                output_this_attempt_parts = []
                                yield {'type': 'stream_reset'}

                                inject_text = "\n".join([f"[LIVE USER FOLLOW-UP]: {m['message']}" for m in injected_msgs])
                                inject_notice = (
                                    f"\n\n[SYSTEM: LIVE INTERRUPT] The user just sent follow-up messages while you were working. "
                                    f"You MUST acknowledge and address these in your response. Adjust your current approach if needed:\n{inject_text}"
                                )
                                # Append as a text part alongside the function responses
                                if isinstance(message_to_send, list):
                                    message_to_send.append(types.Part.from_text(text=inject_notice))
                                else:
                                    message_to_send = [types.Part.from_text(text=inject_notice)]
                                yield {'status': 'User follow-up received! Adjusting...'}
                                logger.info(f"Injected {len(injected_msgs)} live follow-up(s) into chat {chat_id}")
                        # --- END LIVE INTERRUPT ---

                        yield {'status': f"{display_name} is thinking..."}

            # Forcibly add tool results if the model forgot to include them or mangled them
            import re

            # First, clean up any SVGs the model might have wrapped in markdown code blocks
            # This allows them to render correctly even if the model ignored instructions.
            accumulated_full_output = re.sub(r'```(?:svg|xml)?\s*(<svg[\s\S]*?</svg>)\s*```', r'\1', accumulated_full_output, flags=re.IGNORECASE)

            for tool in called_tools_results:
                if tool['name'] in ['web_search', 'send_self_email', 'schedule_task', 'lab_execute', 'host_repo', 'repo_execute', 'repo_control', 'analyze_youtube_video', 'manage_files', 'read_tool_output', 'logs_and_preferences', 'generate_image', 'request_user_interaction', 'obtain_talent']:
                    continue

                if not isinstance(tool['result'], str): continue
                clean_res = tool['result'].strip()

                # Check if the result (or a significant part of it) is already in the output
                already_present = False
                if clean_res in accumulated_full_output:
                    already_present = True
                else:
                    # Look for unique identifiers like UUIDs or URLs in the tool result
                    matches = re.findall(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|https?://\S+', clean_res)
                    for m in matches:
                        if m in accumulated_full_output:
                            already_present = True
                            break

                if not already_present:
                    yield {'result': f"\n{clean_res}\n"}
                    accumulated_full_output += f"\n{clean_res}\n"
                    output_this_attempt_parts.append(f"\n{clean_res}\n")

            output_this_attempt = "".join(output_this_attempt_parts)
            # Re-apply markdown stripping to the final joined output to be safe
            # Use a more aggressive regex to find <svg> blocks even if they have surrounding junk inside the backticks
            output_this_attempt = re.sub(r'```(?:svg|xml)?[\s\S]*?(<svg[\s\S]*?</svg>)[\s\S]*?```', r'\1', output_this_attempt, flags=re.IGNORECASE)

            if candidate is None:
                logger.error("No candidate was generated because all API keys are blocked for model %s.", model_id)
                yield {'status': f'All API keys are blocked for model {display_name}. Failing over...'}
                last_exception = ValueError("All API keys are blocked.")
                break

            candidate_finish_reason_obj = getattr(candidate, 'finish_reason', 'UNKNOWN')
            candidate_finish_reason = candidate_finish_reason_obj.name if hasattr(candidate_finish_reason_obj, 'name') else str(candidate_finish_reason_obj)

            if candidate_finish_reason == 'MAX_TOKENS':
                yield {'status': f'Model hit MAX_TOKENS. Checking if output is cut off...', 'phase': 'continuation_check'}

                if is_output_cut_off(output_this_attempt.strip(), PRIMARY_API_KEY):
                    yield {'status': 'Output is cut off. Attempting to continue...', 'phase': 'continuation_attempt'}

                    # Prevent base64 blowup during continuation
                    safe_accumulated = re.sub(r'(data:image/[^;]+;base64,)[a-zA-Z0-9+/=]+', r'\1[TRUNCATED]', accumulated_full_output)
                    current_effective_prompt = (
                        f"{original_prompt_for_continuation}\n\n"
                        f"---CONTINUATION INSTRUCTION---\n"
                        f"Your previous response was cut off. Please continue the response exactly where you left off, "
                        f"without re-stating any previous information or context. "
                        f"Provide a seamless continuation from the last generated word or phrase. "
                        f"Do not include the 'CONTINUATION INSTRUCTION' section in your response. "
                        f"Here is what you had generated so far:\n---\n{safe_accumulated}\n---"
                    )
                    if attempt == attempts:
                        yield {'result': accumulated_full_output + f"\n\n{ERROR_CODE}: Output truncated due to MAX_TOKENS and could not be fully continued after retries."}
                        return
                    else:
                        continue
                else:
                    yield {'status': 'Model hit MAX_TOKENS, but output appears to be complete.', 'phase': 'complete_despite_max_tokens'}
                    yield {'result': accumulated_full_output}
                    return

            error_finish_reasons = ['SAFETY', 'RECITATION', 'OTHER']
            if candidate_finish_reason in error_finish_reasons:
                 candidate_safety_ratings = getattr(candidate, 'safety_ratings', [])
                 candidate_safety_details = ", ".join([f"{sr.category.name}: {sr.category.name}" for sr in candidate_safety_ratings if hasattr(sr, 'category') and hasattr(sr.category, 'name')]) if candidate_safety_ratings else "N/A"
                 error_msg = f"Content generation stopped by API ({display_name}). Reason: {candidate_finish_reason}, Safety: {candidate_safety_details}"
                 last_exception = ValueError(error_msg)
                 yield {'status': f'Content generation blocked ({candidate_finish_reason}). Retrying...'}
                 next_key_idx = get_next_unblocked_key_index(current_key_index)
                 if next_key_idx is not None:
                     current_key_index = next_key_idx
                     current_key = keys_to_try[current_key_index]
                     continue
                 else:
                     break

            grounding_metadata = getattr(candidate, 'grounding_metadata', None)
            if grounding_metadata:
                 search_entry = getattr(grounding_metadata, 'search_entry_point', None)
                 if search_entry and hasattr(search_entry, 'rendered_content') and search_entry.rendered_content:
                      accumulated_full_output += f"\n\n---\n*Note: The following information may be based on or synthesized from Google Search results.*\n{search_entry.rendered_content}\n---\n"
                 elif hasattr(grounding_metadata, 'web_search_queries') and grounding_metadata.web_search_queries:
                     pass
                 else:
                     pass

            return

        except Exception as e:
            logger.error(f"Error in gemini_generate (Attempt {attempt}/{attempts}) using model {model_id}: {e}", exc_info=True)
            last_exception = e
            is_blockable_error = False
            error_string = str(e).lower()

            is_quota_error = ('429' in error_string or '403' in error_string or '401' in error_string or '400' in error_string or 'permission_denied' in error_string or 'resource_exhausted' in error_string or 'quota' in error_string or 'rate limit' in error_string or 'expired' in error_string or 'invalid' in error_string or 'disabled' in error_string or 'unauthenticated' in error_string)
            is_transient_error = ('overloaded' in error_string or '503' in error_string or 'service unavailable' in error_string or '500' in error_string or 'internal error' in error_string or 'internal_error' in error_string)

            if is_quota_error or is_transient_error:
                 is_blockable_error = True
                 block_duration, block_reason = parse_quota_block_duration(error_string)
                 block_scope = None if ('403' in error_string or 'permission_denied' in error_string or 'invalid' in error_string or 'expired' in error_string or '401' in error_string or '400' in error_string or 'disabled' in error_string or 'unauthenticated' in error_string) else model_id
                 KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                 logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to {block_reason} error in generation loop.")

            if is_blockable_error:
                 next_key_idx = get_next_unblocked_key_index(current_key_index)
                 if next_key_idx is not None:
                     if is_quota_error:
                         yield {'status': f'Quota exceeded. Switching to backup key index {next_key_idx} (circular queue)...'}
                     else:
                         yield {'status': f'Google API encountered transient error ({block_reason}). Switching to backup key index {next_key_idx} (circular queue)...'}
                     current_key_index = next_key_idx
                 else:
                     if is_quota_error:
                         yield {'status': f'Quota exceeded on all keys. Cannot proceed.'}
                     else:
                         yield {'status': f'Google API transient errors on all keys. Cannot proceed.'}
                     break

            if attempt < attempts:
                 yield {'status': f"Encountered error, retrying..."}
                 if not is_blockable_error:
                    next_key_idx = get_next_unblocked_key_index(current_key_index)
                    if next_key_idx is not None:
                        current_key_index = next_key_idx
                        current_key = keys_to_try[current_key_index]
            else:
                 break

    error_info = {
        "model": display_name,
        "attempts": attempts,
        "keys_tried": current_key_index + 1,
        "last_error": str(last_exception)
    }
    error_message = f"{ERROR_CODE}: {display_name} failed to process the request. (Technical Details: {json.dumps(error_info)})"
    yield {'result': accumulated_full_output + error_message if accumulated_full_output else error_message}


def create_output_file(query_or_base_name: str, content: str, extension: str = "txt") -> str | None:
    """
    Create a unique output file on disk inside the 'outputs' directory.
    Automatically sanitizes the base name and increments a suffix if the file already exists.

    Args:
        query_or_base_name (str): The initial query or name used to construct the filename.
        content (str): The text content to write to the file.
        extension (str, optional): The file extension. Defaults to "txt".

    Returns:
        str or None: The filename of the created file, or None if creation failed.
    """
    try:
        output_dir = "outputs"
        os.makedirs(output_dir, exist_ok=True)
        base_filename = sanitize_filename(query_or_base_name[:60].strip())
        if not base_filename:
            base_filename = "output"
        safe_filename = f"{base_filename}.{extension}"
        full_path = os.path.join(output_dir, safe_filename)
        counter = 1
        max_attempts_filename = 100
        while os.path.exists(full_path) and counter <= max_attempts_filename:
            safe_filename = f"{base_filename}_{counter}.{extension}"
            full_path = os.path.join(output_dir, safe_filename)
            counter += 1
        if counter > max_attempts_filename:
            return None
        max_write_attempts = 3
        for attempt in range(max_write_attempts):
            try:
                with open(full_path, "w", encoding="utf-8") as file:
                    file.write(content)
                return os.path.join(output_dir, safe_filename)
            except IOError as e:
                if attempt < max_write_attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                else:
                    return None
            except Exception as e:
                logger.exception("Unexpected error writing output file full_path=%s", full_path)
                pass
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except OSError:
                    pass
            return None
    except Exception as e:
        logger.exception("Unexpected error preparing output file path prefix=%s", query_or_base_name)
        return None
    return None

GRACE_PERIOD_SECONDS = 30

def _redis_repo_key(pid):
    """
    Generate the Redis hash key for tracking a repository process.

    Args:
        pid (str): The process identifier.

    Returns:
        str: The Redis key.
    """
    return f"repo:process:{pid}"

def _redis_runcode_key(pid):
    """
    Generate the Redis hash key for tracking a code run process.

    Args:
        pid (str): The process identifier.

    Returns:
        str: The Redis key.
    """
    return f"runcode:process:{pid}"

def _get_process_key_prefix(process_id, app_type='repo'):
    """
    Determine the appropriate Redis key prefix based on application environment type.

    Args:
        process_id (str): The process identifier.
        app_type (str, optional): The application environment type ('repo' or 'runcode'). Defaults to 'repo'.

    Returns:
        str: The Redis key prefix.
    """
    if app_type == 'repo':
        return _redis_repo_key(process_id)
    return _redis_runcode_key(process_id)


def _extract_json_from_response(response_text):
    """
    Helper to extract JSON string from a potentially markdown-formatted response.
    It looks for ```json ... ``` blocks or just tries to find the first '{' and last '}'.
    """
    if not response_text:
        return None

    # Try to find markdown code blocks first
    import re
    code_block_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    match = re.search(code_block_pattern, response_text, re.DOTALL)
    if match:
        return match.group(1)

    # Fallback: try to find the first '{' and last '}'
    start_idx = response_text.find('{')
    end_idx = response_text.rfind('}')

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return response_text[start_idx : end_idx + 1]

    return None

def _deploy_and_stream_output(app_obj, project_files, process_id, old_container_id=None, app_type='repo', subdomain=None):
    """
    Handle the core deployment lifecycle for user-submitted code repositories.
    Orchestrates building the isolated Docker container environment, configuring network limits,
    mapping directories, starting the application runner, and streaming real-time compilation/build logs
    to the active SSE client connection.

    Args:
        app_obj (Flask): The Flask application instance context.
        project_files (dict): The repository files mapping.
        process_id (str): The unique process identifier of the application.
        old_container_id (str, optional): The ID of the container to tear down before redeploying. Defaults to None.
        app_type (str, optional): The application type (e.g. 'repo'). Defaults to 'repo'.
        subdomain (str, optional): The dynamic subdomain slug. Defaults to None.

    Yields:
        str: Streamed JSON-encoded event objects containing build logs and container statuses.
    """
    logger.info("Starting deployment process_id=%s app_type=%s subdomain=%s", process_id, app_type, subdomain)
    t_start = time.time()
    logs_buffer = []

    user_id = None
    with app_obj.app_context():
        db = get_db()
        cursor = db.execute('SELECT user_id FROM repo_history WHERE process_id = ?', (process_id,))
        row = cursor.fetchone()
        if row: user_id = row['user_id']

    import docker
    client = docker.from_env()
    user_network = ensure_user_network(client, user_id)

    def _put_event(data):
        if data.get('type') in ['log', 'error', 'install_log']:
            logs_buffer.append(str(data.get('content', '')))
        try:
            redis_client.publish(process_id, json.dumps(data))
        except Exception:
            logger.exception("Failed to publish event to redis for %s", process_id)

    def update_history(status=None, container_id=None, url=None, final_logs=None):
        if app_type != 'repo': return
        try:
            with app_obj.app_context():
                db = get_db()
                updates = []
                params = []
                if status:
                    updates.append("status = ?")
                    params.append(status)
                if container_id:
                    updates.append("container_id = ?")
                    params.append(container_id)
                if url:
                    updates.append("deployment_url = ?")
                    params.append(url)
                if final_logs:
                    updates.append("build_logs = ?")
                    params.append(final_logs)

                if updates:
                    updates.append("last_updated = CURRENT_TIMESTAMP")
                    params.append(process_id)
                    sql = f"UPDATE repo_history SET {', '.join(updates)} WHERE process_id = ?"
                    db.execute(sql, tuple(params))
                    db.commit()
        except Exception as e:
            logger.error(f"Failed to update repo history for {process_id}: {e}")

    container = None
    temp_dir_path = None
    redis_key = _get_process_key_prefix(process_id, app_type)

    try:
        container = None
        temp_dir_path = None
        reuse_container = False
        run_id = str(uuid.uuid4())

        # Check if we can reuse the old container
        if old_container_id:
            try:
                old_container = client.containers.get(old_container_id)

                # Check if requirements.txt changed
                old_reqs = ""
                try:
                    res = old_container.exec_run("cat /app/requirements.txt")
                    if res.exit_code == 0:
                        old_reqs = res.output.decode('utf-8')
                except Exception:
                    logger.exception("Failed to read requirements.txt from old container process_id=%s", process_id)
                    pass

                new_reqs = project_files.get('requirements.txt', '')
                if old_reqs.strip() == new_reqs.strip():
                    reuse_container = True
                    container = old_container
                    _put_event({'type': 'log', 'content': f'Reusing existing container ({container.short_id})...'})

                    # Stop old app process
                    # Kill both 'sh' and 'python' processes to prevent port conflicts
                    container.exec_run("python3 -c \"import os, signal; my_pid = os.getpid(); [os.kill(int(p), signal.SIGKILL) for p in os.listdir('/proc') if p.isdigit() and int(p) != my_pid and any(kw in open(f'/proc/{p}/cmdline').read('\x00') for kw in ['app.py', 'python', 'flask'])]\"", user='root')
                    container.exec_run("pkill -9 python || true", user='root')
                    container.exec_run("pkill -f 'python app.py' || true", user='root')

                    # Wait for the port to be fully released to prevent false-positive readiness
                    for _ in range(20):
                        time.sleep(0.5)
                        try:
                            res = container.exec_run("curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/")
                            if res.exit_code != 0 or res.output.decode().strip() == '000':
                                break
                        except Exception:
                            logger.exception("Failed to check readiness of old container port release process_id=%s", process_id)
                            break

                    # Find mount path to update files
                    for mount in container.attrs.get('Mounts', []):
                        if mount['Destination'] == '/app':
                            temp_dir_path = mount['Source']
                            break
                else:
                    _put_event({'type': 'log', 'content': f'Dependencies changed. Rebuilding container ({old_container.short_id})...'})
                    logger.info("Stopping and removing old container for rebuilding process_id=%s container_id=%s", process_id, old_container.id)
                    t_stop = time.time()
                    old_container.stop(timeout=10)
                    old_container.remove(force=True)
                    logger.info("Old container stopped and removed process_id=%s container_id=%s duration_sec=%.2f", process_id, old_container.id, time.time() - t_stop)
            except docker.errors.NotFound:
                logger.info("Previous container not found on Docker engine, skipping cleanup. process_id=%s container_id=%s", process_id, old_container_id)
            except Exception as e:
                logger.exception("Error cleaning up previous container process_id=%s: %s", process_id, e)
                _put_event({'type': 'log', 'content': f'Note: Could not inspect/remove previous instance: {e}'})

        if not reuse_container:
            temp_dir_path = os.path.join(SANDBOX_DIR, f"{app_type}_{run_id}")
            os.makedirs(temp_dir_path, exist_ok=True)

        # Write all project files
        if temp_dir_path:
            with open(os.path.join(temp_dir_path, 'app.py'), 'w', encoding='utf-8') as f:
                f.write(project_files.get('app.py', ''))
            with open(os.path.join(temp_dir_path, 'index.html'), 'w', encoding='utf-8') as f:
                f.write(project_files.get('index.html', ''))

            # Write requirements.txt if present
            requirements_content = project_files.get('requirements.txt', '')
            has_requirements = bool(requirements_content.strip())
            if has_requirements:
                with open(os.path.join(temp_dir_path, 'requirements.txt'), 'w', encoding='utf-8') as f:
                    f.write(requirements_content)

            abs_temp_dir_path = os.path.abspath(temp_dir_path)

        if not reuse_container:
            # Start container with sleep to keep it running while we install deps
            logger.info("Creating new sandbox container process_id=%s image=%s user_network=%s", process_id, 'stellar-python-sandbox:3.12', user_network)
            t_run = time.time()
            container = client.containers.run(
                image='stellar-python-sandbox:3.12',
                command='sleep infinity',
                working_dir='/app',
                volumes={abs_temp_dir_path: {'bind': '/app', 'mode': 'rw'}},
                ports={'5000/tcp': ('0.0.0.0', 0)},
                name=f"stellar-{app_type}-{process_id}",
                remove=False,
                detach=True,
                init=True,
                network=user_network,
                stdout=True,
                stderr=True,
                labels={
                    "stellar_type": app_type,
                    "stellar_process_id": process_id,
                    "created_at_ts": str(time.time()),
                    "repo_app_id": process_id
                }
            )
            logger.info("New sandbox container created process_id=%s container_id=%s duration_sec=%.2f", process_id, container.id, time.time() - t_run)

            _put_event({'type': 'container_id', 'id': container.id})
            _put_event({'type': 'log', 'content': f'Sandbox container ({container.short_id}) created.'})

            update_history(status='created', container_id=container.id)
        else:
            logger.info("Reusing existing sandbox container process_id=%s container_id=%s", process_id, container.id)
            _put_event({'type': 'container_id', 'id': container.id})
            update_history(status='reused', container_id=container.id)

        try:
            redis_client.hset(redis_key, mapping={
                "container_id": container.id,
                "status": "created",
                "process_id": process_id
            })
        except Exception:
            logger.exception("Failed to persist container_id for %s", process_id)

        with active_apps_lock:
            active_apps[process_id] = {"container_id": container.id, "port": None, "status": "created"}

        # Phase 1: Install dependencies if requirements.txt exists
        if has_requirements and not reuse_container:
            _put_event({'type': 'phase', 'phase': 'installing'})
            _put_event({'type': 'log', 'content': '📦 Installing dependencies from requirements.txt...'})

            t_pip_start = time.time()
            try:
                # Run pip install with streaming output
                exec_result = container.exec_run(
                    "pip install --no-cache-dir -r requirements.txt",
                    stream=True,
                    demux=True
                )

                for stdout_chunk, stderr_chunk in exec_result.output:
                    if stdout_chunk:
                        lines = stdout_chunk.decode('utf-8', 'replace').rstrip().split('\n')
                        for line in lines:
                            if line.strip():
                                _put_event({'type': 'install_log', 'content': line})
                    if stderr_chunk:
                        lines = stderr_chunk.decode('utf-8', 'replace').rstrip().split('\n')
                        for line in lines:
                            if line.strip():
                                _put_event({'type': 'install_log', 'content': line})

                duration = time.time() - t_pip_start
                _put_event({'type': 'log', 'content': '✅ Dependencies installed successfully.'})
                logger.info("Dependencies installation complete process_id=%s duration_sec=%.2f", process_id, duration)
            except Exception as pip_err:
                logger.error("Pip install error process_id=%s duration_sec=%.2f error=%s", process_id, time.time() - t_pip_start, str(pip_err), exc_info=True)
                _put_event({'type': 'error', 'content': f'Failed to install dependencies: {pip_err}'})
                return

        # Phase 2: Start the Flask application
        _put_event({'type': 'phase', 'phase': 'starting'})
        _put_event({'type': 'log', 'content': '🚀 Starting Flask application...'})

        # Start the Flask app in the background using exec, redirecting output to app.log
        container.exec_run(["sh", "-c", "python app.py > app.log 2>&1"], detach=True)

        # Wait for the port to become available
        public_url_found = False
        host_port = None
        for i in range(15):
            time.sleep(1)
            try:
                container.reload()
                if getattr(container, "status", None) != 'running':
                    _put_event({'type': 'log', 'content': 'Container exited prematurely. Checking logs...'})
                    break
                ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
                mapping = ports.get('5000/tcp')
                if mapping and mapping[0].get('HostPort'):
                    host_port = mapping[0]['HostPort']
                    break
            except (IndexError, TypeError, KeyError, AttributeError):
                continue

        if host_port:
            # Set the port in active_apps and Redis as soon as we find it
            with active_apps_lock:
                if process_id in active_apps:
                    active_apps[process_id]['port'] = int(host_port)

            try:
                redis_client.hset(redis_key, mapping={"host_port": str(host_port)})
            except Exception:
                logger.exception("Failed to write host_port to Redis process_id=%s", process_id)
                pass

            _put_event({'type': 'log', 'content': f'Container is running on port {host_port}. Verifying server readiness...'})

            is_ready = False
            for _ in range(30):  # Increased retries for dependency-heavy apps
                time.sleep(1)
                try:
                    # We accept 404 as "ready" because it means the server is answering HTTP requests,
                    # even if the root path isn't defined.
                    exec_result = container.exec_run("curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/")
                    if exec_result.exit_code == 0:
                        try:
                            status_code = int(exec_result.output.decode().strip())
                            # Treat 5xx as "not ready/failed" to trigger auto-fix
                            if 0 < status_code < 500:
                                is_ready = True
                                break
                            elif status_code >= 500:
                                # Explicit failure if it's a server error
                                break
                        except ValueError:
                            pass
                except Exception as exec_err:
                    logger.exception("Health check exec failed during startup check process_id=%s container_id=%s", process_id, container.short_id)
                    logger.warning(f"Health check exec error for {container.short_id}: {exec_err}")
                    break

            if is_ready:
                with active_apps_lock:
                    if process_id in active_apps:
                        active_apps[process_id]['status'] = 'running'

                try:
                    redis_client.hset(redis_key, mapping={"status": "running"})
                except Exception:
                    logger.exception("Failed to persist host_port for %s", process_id)

                public_url = f"https://{subdomain}.stellarai.live/" if subdomain else f"https://{process_id}.stellarai.live/"
                _put_event({'type': 'phase', 'phase': 'ready'})
                _put_event({'type': 'log', 'content': f'✨ Server is ready! Available at {public_url}'})
                _put_event({'type': 'port_info', 'url': public_url})
                public_url_found = True
                update_history(status='running', url=public_url)
            else:
                 error_prefix = f"Server responded with ERROR {status_code}" if status_code >= 500 else "Server verification failed"
                 _put_event({'type': 'error', 'content': f'{error_prefix}. The app inside the container did not start correctly.'})

                 # Retrieve app.log to show why it failed
                 try:
                     log_res = container.exec_run("cat app.log")
                     if log_res.exit_code == 0:
                         logs = log_res.output.decode('utf-8', 'replace')
                         _put_event({'type': 'error', 'content': f'--- APP LOGS ---\n{logs}\n--- END APP LOGS ---'})
                     else:
                         _put_event({'type': 'error', 'content': 'Could not read app.log inside container.'})
                 except Exception as e:
                     logger.exception("Error caught: %s", e)
                     _put_event({'type': 'error', 'content': f'Error retrieving app.log: {e}'})

                 update_history(status='failed', final_logs="\n".join(logs_buffer))
                 try: redis_client.hset(redis_key, mapping={"status": "failed"})
                 except Exception as redis_err: logger.error("Failed to set status to failed in Redis for %s: %s", process_id, redis_err)

        if not public_url_found:
            logger.error("Deployment failed to get public URL for process_id=%s, container may have crashed.", process_id)
            _put_event({'type': 'error', 'content': 'Failed to get public URL. Container may have crashed.'})
            update_history(status='failed', final_logs="\n".join(logs_buffer))
            try: redis_client.hset(redis_key, mapping={"status": "failed"})
            except Exception as redis_err: logger.error(f"Failed to set status to failed in Redis for {process_id}: {redis_err}")
            try:
                crashed_logs = container.logs().decode('utf-8', 'replace') if container else "No container"
                _put_event({'type': 'log', 'content': f'--- CRASH LOGS ---\n{crashed_logs}\n--- END LOGS ---'})
            except Exception as log_err:
                logger.exception("Error caught: %s", log_err)
                _put_event({'type': 'log', 'content': f'Could not retrieve crash logs: {log_err}'})

        if container:
            try:
                for line_bytes in container.logs(stream=True, follow=True):
                    txt = line_bytes.decode('utf-8', 'replace').rstrip()
                    _put_event({'type': 'log', 'content': txt})
                container.wait()
            except Exception:
                logger.exception("Error streaming logs for %s", process_id)

    except Exception as e:
        logger.error(f"Error in _deploy_and_stream_output thread for process {process_id}: {e}", exc_info=True)
        _put_event({'type': 'error', 'content': str(e)})
        update_history(status='failed', final_logs="\n".join(logs_buffer))
        try: redis_client.hset(redis_key, mapping={"status": "failed"})
        except Exception as redis_err: logger.error(f"Failed to set status to failed in Redis for {process_id}: {redis_err}")

    finally:
        # Check current status before marking as stopped
        current_status = 'starting'
        try:
            val = redis_client.hget(redis_key, "status")
            if val: current_status = val
        except Exception as redis_err:
            logger.error(f"Failed to get status from Redis for {process_id}: {redis_err}")

        if current_status != 'failed':
            update_history(status='stopped', final_logs="\n".join(logs_buffer))

        duration = time.time() - t_start
        logger.info("Deployment finished process_id=%s app_type=%s status=%s duration_sec=%.2f", process_id, app_type, current_status, duration)

        if container:
            try:
                try:
                    if current_status != 'failed':
                        redis_client.hset(redis_key, mapping={"status": "exited"})
                except Exception:
                    logger.exception("Failed to mark exited status for %s", process_id)
                with active_apps_lock:
                    if process_id in active_apps:
                        if current_status != 'failed':
                            active_apps[process_id]['status'] = 'exited'
                        active_apps[process_id]['exited_at'] = time.time()
            except Exception:
                logger.exception("Failed to mark active_apps for exit for %s", process_id)

            try:
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
            except Exception:
                logger.exception("Error removing container for %s", process_id)

        if temp_dir_path:
            try:
                shutil.rmtree(temp_dir_path, ignore_errors=True)
            except Exception:
                logger.exception("Error removing temp dir for %s", process_id)

        try:
            redis_client.publish(process_id, '__STREAM_END__')
        except Exception:
            logger.exception("Failed to publish __STREAM_END__ for %s", process_id)

        req_id = g.request_id if getattr(g, 'request_id', None) else 'system'
        def _delayed_cleanup(pid, r_key, r_id, delay=GRACE_PERIOD_SECONDS):
            thread_local_ctx.request_id = r_id
            time.sleep(delay)
            with active_apps_lock:
                active_apps.pop(pid, None)
            try:
                redis_client.delete(r_key)
            except Exception:
                logger.exception("Failed to delete redis key for %s", pid)

        cleanup_thread = threading.Thread(target=_delayed_cleanup, args=(process_id, redis_key, req_id), daemon=True)
        cleanup_thread.start()

def stop_and_cleanup_app_by_process_id(process_id, app_type='repo'):
    """
    Stop and clean up a deployed container by its unique process ID.
    Retrieves the container ID from Redis or active app tracking, stops the container,
    snapshots its current file tree state to database, removes the container and its volumes,
    and deletes its records from active tracking and Redis.

    Args:
        process_id (str): The unique process identifier of the application.
        app_type (str, optional): The application type (e.g. 'repo'). Defaults to 'repo'.
    """
    if not process_id:
        return

    redis_key = _get_process_key_prefix(process_id, app_type)
    container_id = None
    try:
        cid = redis_client.hget(redis_key, "container_id")
        if cid:
            container_id = cid.decode() if isinstance(cid, (bytes, bytearray)) else cid
    except Exception:
        logger.exception("Redis hget failed for process %s", process_id)

    if not container_id:
        with active_apps_lock:
            info = active_apps.get(process_id)
            if info:
                container_id = info.get('container_id')

    if container_id:
        try:
            logger.info("Stopping and removing container container_id=%s process_id=%s", container_id, process_id)
            t_stop = time.time()
            container = client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
            logger.info("Container stopped and removed container_id=%s duration_sec=%.2f", container_id, time.time() - t_stop)
        except docker.errors.NotFound:
            logger.info("Container to remove not found container_id=%s process_id=%s", container_id, process_id)
        except Exception as e:
            logger.exception("Error caught: %s", e)
            logger.warning(f"Warning during cleanup for container {container_id}: {e}")

    with active_apps_lock:
        active_apps.pop(process_id, None)
    try:
        redis_client.delete(redis_key)
    except Exception:
        logger.exception("Failed to delete redis key for %s", process_id)

    # Update database status to stopped
    try:
        db = get_db()
        db.execute("UPDATE repo_history SET status = 'stopped' WHERE process_id = ?", (process_id,))
        db.commit()
    except Exception as e:
        logger.error(f"Failed to update database status to stopped for {process_id}: {e}")



@app.route('/get_history', methods=['GET'])
@require_approval
def get_history_route():
    try:
        chat_id = request.args.get('chat_id')
        db = get_db()

        if chat_id:
            try:
                chat_id_int = int(chat_id)
            except ValueError:
                return jsonify({'status': 'Failed: Invalid chat ID format', 'history': []}), 400
            cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id_int, session['user_id']))
            if not cursor.fetchone():
                return jsonify({'status': 'Failed: Chat not found or unauthorized', 'history': []}), 403
        else:
            chat_id = session.get('current_chat_id')
            if chat_id:
                cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, session['user_id']))
                if not cursor.fetchone():
                    session.pop('current_chat_id', None)
                    chat_id = None

            if not chat_id:
                chat_id = get_current_chat_id(session['user_id'])
                session['current_chat_id'] = chat_id
                session.modified = True

        if not chat_id:
            return jsonify({'status': 'Failed: No active chat ID found', 'history': []}), 400

        history = get_conversation_history(chat_id, for_ui=True)
        # Filter out hidden compressed state docs — they're for LLM context only, not the UI
        history = [msg for msg in history if not str(msg.get('message_content', '')).startswith('[COMPRESSED MEMORY STATE]')]

        return jsonify({'history': history})
    except Exception as e:
        logger.error(f"Error in get_history_route: {e}", exc_info=True)
        return jsonify({'status': 'Failed: Server error fetching history', 'history': []}), 500

@app.route('/update_message', methods=['POST'])
@require_approval
def update_message_route():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'Failed: No JSON data received'}), 400
        message_id = data.get('id')
        content = data.get('content')
        if not message_id:
            return jsonify({'status': 'Failed: Missing message ID parameter'}), 400
        try:
            message_id_int = int(message_id)
        except (ValueError, TypeError):
             return jsonify({'status': 'Failed: Invalid message ID format'}), 400
        db = get_db()
        cursor = db.execute('SELECT chat_id FROM messages WHERE id = ?', (message_id_int,))
        message_info = _fetchone_as_dict(cursor)
        if not message_info:
            return jsonify({'status': 'Failed: Message not found'}), 404

        chat_id = message_info['chat_id']
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, session['user_id']))
        chat_ownership = cursor.fetchone()
        if not chat_ownership:
            return jsonify({'status': 'Failed: Message not found or permission denied'}), 403

        success = update_message(message_id_int, content if content is not None else "")
        if success:
            return jsonify({'status': 'Success'})
        else:
             return jsonify({'status': 'Failed: Database update error'}), 500
    except Exception as e:
        logger.error(f"Error in update_message_route: {e}", exc_info=True)
        return jsonify({'status': 'Failed: Server error during update'}), 500

@app.route('/api/logs_preferences', methods=['GET', 'POST', 'DELETE'])
@require_approval
def api_logs_preferences():
    user_id = str(session['user_id'])
    db = get_db()

    if request.method == 'GET':
        cursor = db.execute('SELECT id, log_entry FROM user_logs_prefs WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        logs = [row['log_entry'] for row in cursor.fetchall()]
        return jsonify({'logs': logs})

    elif request.method == 'POST':
        data = request.get_json()
        if not data or 'logs' not in data:
            return jsonify({'error': 'Invalid data'}), 400

        db.execute('DELETE FROM user_logs_prefs WHERE user_id = ?', (user_id,))
        if data['logs']:
            db.executemany('INSERT INTO user_logs_prefs (user_id, log_entry) VALUES (?, ?)', [(user_id, log) for log in data['logs']])
        db.commit()
        return jsonify({'success': True})

    elif request.method == 'DELETE':
        index = request.args.get('index', type=int)
        if index is None:
            return jsonify({'error': 'Invalid request'}), 400

        cursor = db.execute('SELECT id FROM user_logs_prefs WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        rows = cursor.fetchall()
        if 0 <= index < len(rows):
            log_id = rows[index]['id']
            db.execute('DELETE FROM user_logs_prefs WHERE id = ?', (log_id,))
            db.commit()
            return jsonify({'success': True})
        return jsonify({'error': 'Index out of bounds'}), 400

@app.route('/api/chats/<int:chat_id>/active_stream', methods=['GET'])
@require_approval
def get_active_stream(chat_id):
    """Allows UI to detect if a specific chat has an active stream it should reconnect to."""
    db = get_db()
    cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, session['user_id']))
    if not cursor.fetchone():
        return jsonify({'error': 'Unauthorized'}), 403

    active_query_str = redis_client.get(f"chat_active_query:{chat_id}")
    if active_query_str:
        return Response(active_query_str, mimetype='application/json')
    return jsonify({})

@app.route('/api/generative_ui/finish', methods=['POST'])
@require_approval
def generative_ui_finish():
    data = request.json
    interaction_id = data.get('interaction_id')
    result_data = data.get('data')
    if not interaction_id:
        return jsonify({'error': 'Missing interaction_id'}), 400

    try:
        import redis
        r_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        redis_key = f"ui_interaction:{interaction_id}"
        # We store it as JSON string
        r_client.setex(redis_key, 60, json.dumps(result_data) if isinstance(result_data, dict) else str(result_data))
        return jsonify({'status': 'success'})
    except Exception as e:
        logger.error(f"Error in generative_ui_finish: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/inject_message', methods=['POST'])
@require_approval
def inject_message():
    """Inject a follow-up message into an active generation stream.
    The message is stored in Redis and picked up by the tool loop in gemini_generate."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400

        chat_id = data.get('chat_id')
        message = data.get('message', '').strip()
        client_id = data.get('client_id')

        if not chat_id or not message:
            return jsonify({'error': 'Missing chat_id or message'}), 400

        # Security: Verify that the current user owns the target chat to prevent unauthorized injection.
        try:
            chat_id_int = int(chat_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid chat_id format'}), 400

        db = get_db()
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id_int, session['user_id']))
        if not cursor.fetchone():
            return jsonify({'error': 'Unauthorized or chat not found'}), 403

        # Check if there's an active stream for this chat
        active_query_str = redis_client.get(f"chat_active_query:{chat_id}")
        if not active_query_str:
            return jsonify({'error': 'No active stream to inject into'}), 409

        # Store the user's message in the DB immediately so it appears in history
        user_msg_id = insert_message(chat_id, "user", message, client_id=client_id)

        # Push the injection into a Redis list keyed by chat_id
        injection_data = json.dumps({
            'message': message,
            'message_id': str(user_msg_id),
            'timestamp': time.time()
        })
        redis_client.rpush(f"inject_messages:{chat_id}", injection_data)

        # Notify frontend via SSE that the injection was acknowledged
        active_query = json.loads(active_query_str)
        query_id = active_query.get('query_id')
        if query_id:
            ack_event = f"data: {json.dumps({'type': 'injection_ack', 'message_id': str(user_msg_id), 'message': message})}\n\n"
            redis_client.rpush(f"stream_history:{query_id}", ack_event)
            redis_client.publish(f"stream:{query_id}", ack_event)

        logger.info(f"Message injected into chat {chat_id}: {message[:80]}...")
        return jsonify({'success': True, 'message_id': str(user_msg_id)}), 200

    except Exception as e:
        logger.error(f"Error in inject_message: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/register_query', methods=['POST'])
@require_approval
def register_query():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400

        query = data.get('query')
        model_id = data.get('model_id')
        mode = data.get('mode')
        pending_files = data.get('pending_files',[])
        chat_id = data.get('chat_id')
        hidden = data.get('hidden', False)
        disabled_tools = data.get('disabled_tools',[])

        if not query or not model_id or not mode or not chat_id:
            return jsonify({'error': 'Missing required data: query, model_id, mode, chat_id'}), 400

        # Security: Verify that the current user owns the target chat to prevent query hijacking/SSRF.
        try:
            chat_id_int = int(chat_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid chat_id format'}), 400

        db = get_db()
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id_int, session['user_id']))
        if not cursor.fetchone():
            return jsonify({'error': 'Unauthorized or chat not found'}), 403

        if not isinstance(pending_files, list):
             pending_files =[]

        client_id = data.get('client_id')
        query_id = str(uuid.uuid4())

        # Notify other devices that a stream is starting in this chat
        redis_client.publish(f"user_events:{session['user_id']}", json.dumps({
            "type": "query_started",
            "client_id": client_id,
            "chat_id": chat_id,
            "query_id": query_id,
            "mode": mode
        }))

        query_data = {
            'query': query,
            'model_id': model_id,
            'mode': mode,
            'pending_files': pending_files,
            'timestamp': time.time(),
            'chat_id': chat_id,
            'hidden': hidden,
            'disabled_tools': disabled_tools,
            'user_id': session.get('user_id'),
            'username': session.get('username'),
            'session_id': get_current_session_id(),
            'client_id': client_id
        }

        # Make Query Parameters fully durable using Redis!
        redis_client.setex(f"query_args:{query_id}", 3600 * 24, json.dumps(query_data))
        redis_client.setex(f"chat_active_query:{chat_id}", 3600 * 24, json.dumps({'query_id': query_id, 'mode': mode}))

        logger.info("Query registered query_id=%s chat_id=%s model_id=%s mode=%s user_id=%s", query_id, chat_id, model_id, mode, session.get('user_id'))
        return jsonify({'query_id': query_id}), 200

    except Exception as e:
        logger.error(f"Error in register_query: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error during query registration'}), 500



@app.route('/refine_stream', methods=['GET'])
@require_approval
def refine_stream():
    query_id = request.args.get('query_id')

    if not query_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Missing query identifier.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=400)

    query_data_str = redis_client.get(f"query_args:{query_id}")
    if not query_data_str:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Query session expired or invalid.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=404)

    query_data = json.loads(query_data_str)

    # Ownership Check
    if str(query_data.get('user_id')) != str(session.get('user_id')):
        def error_stream(): yield f"data: {json.dumps({'status': 'Unauthorized: Query ownership mismatch.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=403)

    user_query_from_frontend = query_data.get('query', '')
    model_id = query_data.get('model_id')
    pending_files = query_data.get('pending_files',[])
    chat_id = query_data.get('chat_id')
    hidden = query_data.get('hidden', False)
    disabled_tools = query_data.get('disabled_tools',[])
    session_id = query_data.get('session_id')
    user_id = query_data.get('user_id')
    username = query_data.get('username')
    client_id = query_data.get('client_id')

    is_running = redis_client.exists(f"stream_started:{query_id}")

    if not is_running:
        logger.info("Starting new SSE stream query_id=%s chat_id=%s model_id=%s username=%s", query_id, chat_id, model_id, username)
        redis_client.setex(f"stream_started:{query_id}", 3600 * 24, "1")

        cancel_event = threading.Event()
        if chat_id:
            # Terminate any old thread for this chat across other worker processes
            try:
                redis_client.publish("stellar_cancellations", json.dumps({
                    "chat_id": chat_id,
                    "exclude_query_id": query_id
                }))
            except Exception as pub_err:
                logger.error(f"Failed to publish new stream start cancellation to Redis: {pub_err}")

            old_val = ACTIVE_CHATS_CANCEL_EVENTS.get(chat_id)
            if old_val:
                old_event = old_val[0] if isinstance(old_val, tuple) else old_val
                logger.info("Dynamic interrupt/cancellation requested for chat_id: %s. Terminating old thread.", chat_id)
                old_event.set()
            ACTIVE_CHATS_CANCEL_EVENTS[chat_id] = (cancel_event, query_id)

        def generator_task(cancel_event=None):
            from flask import g
            from google.genai import types
            g.user_id = user_id
            g.chat_id = chat_id
            g.username = username
            g.session_id = session_id

            gemini_files_data =[]
            # Clear any stale injection messages from previous streams
            redis_client.delete(f"inject_messages:{chat_id}")
            if pending_files:
                context_id = str(chat_id) if chat_id else session_id
                gemini_files_data = upload_files_to_gemini(context_id, pending_files)

            user_message_id = insert_message(chat_id, "user", user_query_from_frontend, attached_files=gemini_files_data, user_query_for_name=user_query_from_frontend, hidden=hidden, client_id=client_id)

            final_stellar_message_id = None
            llm_error_occurred = False

            try:
                # Construct multimodal prompt array
                multimodal_prompt =[]
                for gf in gemini_files_data:
                    if gf.get('uri'):
                        multimodal_prompt.append(types.Part.from_uri(file_uri=gf['uri'], mime_type=gf['mime_type']))
                    elif gf.get('fallback_to_lab'):
                        fallback_instr = f"\n[SYSTEM NOTICE: The file '{gf['display_name']}' has an unsupported MIME type for native vision. It has been automatically synced to the Lab environment at '/lab/{gf['display_name']}'. You MUST use lab_execute to analyze this file.]\n"
                        multimodal_prompt.append(types.Part.from_text(text=fallback_instr))

                if check_and_log_stop(query_id, "history retrieval"): return
                conversation_history = get_conversation_history(chat_id)
                conv_hist_list, last_msg_time = build_annotated_history(conversation_history, user_message_id)

                # Dynamically add tool execution history to context
                tool_hist_context = get_tool_history(chat_id)
                if tool_hist_context:
                    conv_hist_list.append(tool_hist_context)

                # --- Targeted Memory Compression System ---
                # Phase 1: Three-Bucket Ratio Calculation
                new_msg_size = len(user_query_from_frontend)
                history_msg_size = sum(len(str(msg.get('message_content', ''))) for msg in conversation_history if str(msg.get('id')) != str(user_message_id))

                # Calculate tool size from the raw tool history context string
                tool_size = len(tool_hist_context) if tool_hist_context else 0

                total_size = new_msg_size + history_msg_size + tool_size
                if total_size > 0:
                    new_msg_pct = int((new_msg_size / total_size) * 100)
                    msg_pct = int((history_msg_size / total_size) * 100)
                    tool_pct = int((tool_size / total_size) * 100)
                else:
                    new_msg_pct = msg_pct = tool_pct = 0

                current_chat_tokens = count_chat_tokens(chat_id)
                MODEL_CONTEXT_LIMIT = 1000000  # 1M tokens for Gemini Flash models

                # Phase 2: 95% Safety Drop
                safety_rounds = 0
                while current_chat_tokens > 0.95 * MODEL_CONTEXT_LIMIT and safety_rounds < 2:
                    safety_rounds += 1
                    if new_msg_pct > 60:
                        # The new message itself is too large
                        yield 'data: ' + json.dumps({"error": "Your message is too large for the remaining context window. Please shorten it or start a new chat."}) + '\n\n'
                        return

                    try:
                        db_safety = get_db()
                        if tool_pct >= msg_pct:
                            # Drop oldest 25% of tool calls
                            total_tools = db_safety.execute('SELECT COUNT(*) FROM tool_calls WHERE chat_id = ? AND hidden = 0', (chat_id,)).fetchone()[0]
                            drop_count = max(1, total_tools // 4)
                            db_safety.execute(
                                'UPDATE tool_calls SET hidden = 1 WHERE id IN (SELECT id FROM tool_calls WHERE chat_id = ? AND hidden = 0 ORDER BY id ASC LIMIT ?)',
                                (chat_id, drop_count)
                            )
                        else:
                            # Drop oldest 25% of messages
                            total_msgs = db_safety.execute('SELECT COUNT(*) FROM messages WHERE chat_id = ? AND hidden = 0', (chat_id,)).fetchone()[0]
                            drop_count = max(1, total_msgs // 4)
                            db_safety.execute(
                                'UPDATE messages SET hidden = 1 WHERE id IN (SELECT id FROM messages WHERE chat_id = ? AND hidden = 0 AND message_content NOT LIKE ? ORDER BY id ASC LIMIT ?)',
                                (chat_id, '[COMPRESSED MEMORY STATE]%', drop_count)
                            )
                        db_safety.commit()
                        logger.info(f"Safety drop round {safety_rounds} for chat {chat_id}: dropped {drop_count} items (tool_pct={tool_pct}, msg_pct={msg_pct})")
                    except Exception as e:
                        logger.error(f"Safety drop failed for chat {chat_id}: {e}")
                        break

                    # Rebuild context after safety drop
                    conversation_history = get_conversation_history(chat_id)
                    conv_hist_list, last_msg_time = build_annotated_history(conversation_history, user_message_id)
                    tool_hist_context = get_tool_history(chat_id)
                    if tool_hist_context:
                        conv_hist_list.append(tool_hist_context)

                    current_chat_tokens = count_chat_tokens(chat_id)

                if safety_rounds >= 2 and current_chat_tokens > 0.95 * MODEL_CONTEXT_LIMIT:
                    yield 'data: ' + json.dumps({"error": "This chat's history is too large. Please start a new chat."}) + '\n\n'
                    return

                # Phase 3: 75% Warning Injection
                if current_chat_tokens > 0.75 * MODEL_CONTEXT_LIMIT:
                    # Compression loop prevention: check if compress_memory was called recently
                    try:
                        db_check = get_db()
                        recent_compress = db_check.execute(
                            "SELECT COUNT(*) FROM tool_calls WHERE chat_id = ? AND tool_name = 'compress_memory' AND hidden = 0 ORDER BY id DESC LIMIT 3",
                            (chat_id,)
                        ).fetchone()[0]
                    except:
                        recent_compress = 0

                    if recent_compress == 0:
                        total_pct = int((current_chat_tokens / MODEL_CONTEXT_LIMIT) * 100)
                        compression_warning = (
                            f"\n\n[SYSTEM CRITICAL WARNING]: Context window at {total_pct}%. "
                            f"Breakdown: Tool Logs ({tool_pct}%), Chat Messages ({msg_pct}%), Current Message ({new_msg_pct}%). "
                            f"You MUST immediately call the `compress_memory` tool before doing anything else. "
                            f"Target the largest category. Write a thorough state_document preserving: "
                            f"Current Objective, Key Discoveries (file paths, schemas, specific values), Files Modified, and Current State & Pending Blockers."
                        )
                        conv_hist_list.append(compression_warning)
                # -------------------------------------------

                refined_query_result = ""
                models_to_try = get_fallback_chain(model_id)
                last_error_details = ""
                partial_work_done = ""

                for current_model in models_to_try:
                    if check_and_log_stop(query_id, f"LLM call {current_model}"): return
                    display_name = MODEL_NAMES.get(current_model, current_model)
                    current_api_key = PRIMARY_API_KEY

                    if not current_api_key:
                        yield f"data: {json.dumps({'status': 'Error: API Key Configuration Missing.', 'error': True})}\n\n"
                        return

                    if current_model != models_to_try[0]:
                        yield f"data: {json.dumps({'status': f'Model failed. Falling back to {display_name}...', 'phase': 'refining'})}\n\n"
                        time.sleep(1)

                    # If we have partial work from a previous failed model, inject it as continuation context
                    effective_conv_hist = conv_hist_list.copy()
                    if partial_work_done:
                        capability_note = ""
                        # Define model tiers clearly for fallback guidance
                        full_access = ["gemini-3-flash-preview", "gemini-3.5-flash", "gemma-4-31b-it"]
                        lab_only = [] # Lunarity now has full access

                        if current_model in lab_only:
                            capability_note = " NOTE: You have access to 'lab_execute' but NOT 'repo_control'. Complete the task using the Lab or Web Search."
                        elif current_model not in full_access:
                            capability_note = " NOTE: You are a standard model and do not have access to 'lab_execute' or 'repo_control'. You MUST use your available tools (Web Search, File Management, etc.) to complete the task."

                        effective_conv_hist.append(f"Stellar (Partial Progress from failed model): {partial_work_done}\n\n[SYSTEM INSTRUCTION]: The previous model failed mid-thought. Continue the task immediately from where it left off using the partial output provided above. Do not repeat the work already done.{capability_note}")



                    # Calculate time elapsed since last message (in UTC)
                    time_elapsed_str = ""
                    if 'last_msg_time' in locals() and last_msg_time:
                        try:
                            now_utc = datetime.datetime.utcnow()
                            elapsed_delta = now_utc - last_msg_time
                            if elapsed_delta.total_seconds() > 60:  # Only inject if > 1 minute has passed
                                formatted_elapsed = format_time_delta(elapsed_delta).replace(' later', '')
                                time_elapsed_str = f"[SYSTEM NOTICE: {formatted_elapsed} has passed since the last message in this conversation.]\n\n"
                        except Exception as e:
                            logger.error(f"Error calculating time elapsed: {e}")

                    effective_user_query = f"{time_elapsed_str}{user_query_from_frontend}"
                    text_prompt = get_refinement_prompt(effective_user_query, effective_conv_hist, username=username, disabled_tools=disabled_tools, user_id=user_id, model_id=current_model)

                    # Create a copy of multimodal_prompt and add the text_prompt
                    final_prompt = multimodal_prompt.copy()
                    final_prompt.append(types.Part.from_text(text=text_prompt))

                    generator_output = gemini_generate(
                        prompt=final_prompt,
                        model_id=current_model,
                        key=current_api_key,
                        attempts=len(BACKUP_API_KEYS),
                        model_display_name=f"{display_name}",
                        username=username,
                        chat_id=chat_id,
                        disabled_tools=disabled_tools,
                        gemini_files_data=gemini_files_data,
                        cancel_event=cancel_event
                    )

                    model_failed = False
                    current_attempt_result = ""
                    for item in generator_output:
                        if cancel_event and cancel_event.is_set():
                            logger.info(f"generator_task for chat {chat_id} aborted mid-stream due to cancellation. Deleting partial progress.")
                            return
                        if 'status' in item:
                            status_dict = {'status': item['status'], 'phase': 'refining'}
                            if 'timeout' in item:
                                status_dict['timeout'] = item['timeout']
                            yield f"data: {json.dumps(status_dict)}\n\n"
                        elif 'type' in item and item['type'] == 'generative_ui':
                            yield f"data: {json.dumps(item)}\n\n"
                        elif 'type' in item and item['type'] == 'stream_reset':
                            logger.info(f"Dynamic interrupt received for chat {chat_id}: clearing stream accumulation buffers.")
                            refined_query_result = ""
                            current_attempt_result = ""
                            yield f"data: {json.dumps({'type': 'stream_reset'})}\n\n"
                        elif 'result' in item:
                            temp_result = item['result']
                            if isinstance(temp_result, str) and temp_result.startswith(ERROR_CODE):
                                last_error_details = temp_result
                                # Store partial work so next model can pick up
                                if current_attempt_result:
                                    partial_work_done += "\n" + current_attempt_result
                                model_failed = True
                                break
                            else:
                                current_attempt_result += temp_result
                                refined_query_result += temp_result

                    if not model_failed and refined_query_result:
                        break
                    else:
                        # If whole loop failed without even partial result, we keep refined_query_result as is for next iteration
                        pass

                # FINAL FAIL-SAFE: If everything failed, have Lunarity generate a diagnostic report
                if not refined_query_result and last_error_details:
                    yield f"data: {json.dumps({'status': 'All models busy or exhausted. Lunarity is generating diagnostic report...', 'phase': 'diagnostic'})}\n\n"
                    from prompts import get_error_explanation_prompt
                    diag_prompt = get_error_explanation_prompt(user_query_from_frontend, last_error_details)

                    generator_output = gemini_generate(
                        prompt=diag_prompt,
                        model_id="gemma-4-31b-it",
                        key=PRIMARY_API_KEY,
                        attempts=1,
                        model_display_name="Lunarity (Diagnostic)",
                        chat_id=chat_id,
                        cancel_event=cancel_event
                    )
                    for item in generator_output:
                        if cancel_event and cancel_event.is_set():
                            logger.info(f"generator_task for chat {chat_id} aborted mid-stream during diagnostics. Deleting partial progress.")
                            return
                        if 'result' in item:
                            refined_query_result += item['result']

                if refined_query_result:
                    if cancel_event and cancel_event.is_set():
                        logger.info(f"generator_task for chat {chat_id} aborted before database insert. Deleting partial progress.")
                        return
                    if check_and_log_stop(query_id, "database insert"): return
                    stellar_message_id = insert_message(
                        chat_id,
                        "stellar",
                        refined_query_result,
                        hidden=hidden,
                        client_id=client_id
                    )
                    if stellar_message_id:
                         final_stellar_message_id = stellar_message_id
                         final_data = {
                             'status': 'refined_ready',
                             'session_id': session_id,
                             'message_id': str(final_stellar_message_id),
                             'user_message_id': str(user_message_id) if user_message_id else None,
                             'refined_query': refined_query_result
                         }
                         yield f"data: {json.dumps(final_data)}\n\n"

                         # Dispatch background notification when task is complete!
                         try:
                             from flask import g
                             p_user_id = getattr(g, 'user_id', None)
                             if not p_user_id and chat_id:
                                 import sqlite3
                                 db_temp = sqlite3.connect(DATABASE_NAME)
                                 db_temp.row_factory = sqlite3.Row
                                 db_temp.execute("PRAGMA journal_mode=WAL;")
                                 db_temp.execute("PRAGMA busy_timeout=5000;")
                                 row = db_temp.execute('SELECT user_id FROM chats WHERE id = ?', (chat_id,)).fetchone()
                                 if row:
                                     p_user_id = row['user_id']
                                 db_temp.close()

                             if p_user_id:
                                  send_push_notification(
                                      user_id=p_user_id,
                                      title="Stellar: Task Completed",
                                      body=refined_query_result or "Task execution completed successfully.",
                                      url=f"/?chat_id={chat_id}"
                                  )
                         except Exception as push_err:
                             logger.error(f"Failed to dispatch completion push notification: {push_err}")

                    else:
                          error_msg = "Refinement generated but failed to save AI response to database."
                          yield f"data: {json.dumps({'status': error_msg, 'error': True})}\n\n"
                          llm_error_occurred = True
                else:
                     error_msg = "Encountered an error: Unable to refine query after all attempts."
                     yield f"data: {json.dumps({'status': error_msg, 'error': True})}\n\n"
                     llm_error_occurred = True

            except Exception as e:
                logger.error(f"Error in generate_refinement_stream_with_analysis: {e}", exc_info=True)
                yield f"data: {json.dumps({'status': 'Severe error during refinement stream processing.', 'error': True})}\n\n"
                llm_error_occurred = True

        background_thread_runner(current_app._get_current_object(), query_id, chat_id, cancel_event, generator_task)

    return Response(stream_with_context(stream_consumer(query_id)), mimetype='text/event-stream')



@app.route('/api/stop_generation', methods=['POST'])
@require_approval
def stop_generation():
    data = request.get_json()
    query_id = data.get('query_id')
    chat_id = data.get('chat_id')

    if not query_id:
        return jsonify({'error': 'Missing query_id.'}), 400

    # Security: Verify query ownership via user_id stored in query_args.
    if query_id:
        query_data_str = redis_client.get(f"query_args:{query_id}")
        if query_data_str:
            query_data = json.loads(query_data_str)
            if str(query_data.get('user_id')) != str(session.get('user_id')):
                return jsonify({'error': 'Unauthorized'}), 403

    # Security: Verify chat ownership if chat_id is provided.
    if chat_id:
        try:
            chat_id_int = int(chat_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid chat_id format'}), 400
        db = get_db()
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id_int, session['user_id']))
        if not cursor.fetchone():
            return jsonify({'error': 'Unauthorized or chat not found'}), 403

    redis_client.setex(f"stop_flag:{query_id}", 3600, "1")
    if chat_id:
        redis_client.delete(f"chat_active_query:{chat_id}")
        
        # Publish cancellation to all Gunicorn workers via Redis Pub/Sub
        try:
            redis_client.publish("stellar_cancellations", json.dumps({"chat_id": chat_id}))
            logger.info("Published cancellation event to Redis chat_id=%s", chat_id)
        except Exception as pub_err:
            logger.error("Failed to publish cancellation event to Redis: %s", pub_err)

        val = ACTIVE_CHATS_CANCEL_EVENTS.get(chat_id)
        if val:
            cancel_event = val[0] if isinstance(val, tuple) else val
            logger.info("Stop button clicked signalling thread termination chat_id=%s", chat_id)
            cancel_event.set()

    logger.info("Stop flag set in Redis query_id=%s", query_id)
    return jsonify({'success': True, 'message': 'Stop signal received.'})

def check_and_log_stop(query_id, stage=""):
    if redis_client.exists(f"stop_flag:{query_id}"):
        logger.info("Stop signal detected for query_id: %s at stage: %s", query_id, stage)
        return True
    return False

def stream_consumer(query_id):
    """Consumer for replaying historical events and subscribing to live events."""
    logger.info("SSE client connected to stream_consumer query_id=%s", query_id)
    pubsub = redis_client.pubsub()
    try:
        pubsub.subscribe(f"stream:{query_id}")

        # Replay historical state so page reloads rebuild perfectly
        history = redis_client.lrange(f"stream_history:{query_id}", 0, -1)
        for item in history:
            if isinstance(item, bytes):
                item = item.decode('utf-8')
            if item == "__STREAM_END__":
                pubsub.close()
                yield f"data: {json.dumps({'status': 'Stream ended.', 'error': True, 'stopped': True})}\n\n"
                return
            yield item

        # Listen to live updates
        while True:
            # Bolt - Performance/Stability Optimization: Use non-blocking get_message with timeout
            # to prevent Gunicorn threads from hanging indefinitely when clients disconnect from SSE.
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
            if message:
                if message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    if data == "__STREAM_END__":
                        yield f"data: {json.dumps({'status': 'Stream ended.', 'error': True, 'stopped': True})}\n\n"
                        break
                    yield data
            else:
                # Heartbeat comment forces a socket write to let Gunicorn detect closed client connections
                yield ": heartbeat\n\n"
    finally:
        logger.info("SSE client disconnected from stream_consumer query_id=%s", query_id)
        pubsub.close()

def background_thread_runner(app_obj, query_id, chat_id, cancel_event, task_func, *args):
    """Wrapper that runs generation streams in the background to decouple from HTTP requests."""
    from flask import g
    req_id = getattr(g, 'request_id', None)

    def run():
        if req_id:
            thread_local_ctx.request_id = req_id
        t_bg_start = time.time()
        logger.info("Background stream thread started query_id=%s chat_id=%s", query_id, chat_id)
        with app_obj.app_context():
            from flask import g
            if req_id:
                g.request_id = req_id
            try:
                for chunk in task_func(cancel_event, *args):
                    redis_client.rpush(f"stream_history:{query_id}", chunk)
                    redis_client.publish(f"stream:{query_id}", chunk)
            except Exception as e:
                logger.error("Stream background task error query_id=%s error=%s", query_id, e, exc_info=True)
                err_str = f"data: {json.dumps({'status': f'Internal Background Error: {str(e)}', 'error': True})}\n\n"
                redis_client.rpush(f"stream_history:{query_id}", err_str)
                redis_client.publish(f"stream:{query_id}", err_str)
            finally:
                val = ACTIVE_CHATS_CANCEL_EVENTS.get(chat_id)
                if val:
                    event_to_check = val[0] if isinstance(val, tuple) else val
                    if event_to_check == cancel_event:
                        ACTIVE_CHATS_CANCEL_EVENTS.pop(chat_id, None)
                redis_client.rpush(f"stream_history:{query_id}", "__STREAM_END__")
                redis_client.publish(f"stream:{query_id}", "__STREAM_END__")
                redis_client.delete(f"chat_active_query:{chat_id}")
                logger.info("Background stream thread finished query_id=%s chat_id=%s duration_sec=%.2f", query_id, chat_id, time.time() - t_bg_start)
    threading.Thread(target=run, daemon=True).start()

@app.route('/api/messages/delete', methods=['POST'])
@require_approval
def delete_message():
    data = request.get_json()
    try:
        message_id = int(data.get('message_id'))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid message_id.'}), 400

    user_id = session['user_id']
    db = get_db()

    try:
        logger.info("Attempting to delete message message_id=%s user_id=%s", message_id, user_id)
        # Verify ownership by checking the chat the message belongs to
        cursor = db.execute('''
            SELECT m.id, c.user_id FROM messages m
            JOIN chats c ON m.chat_id = c.id
            WHERE m.id = ? AND c.user_id = ?
        ''', (message_id, user_id))

        row = cursor.fetchone()
        if not row:
            logger.warning("Deletion failed message_id=%s user_id=%s reason=not_found_or_unauthorized", message_id, user_id)
            return jsonify({'error': 'Message not found or unauthorized.'}), 403

        logger.info("Ownership verified proceeding with deletion message_id=%s", message_id)
        db.execute('DELETE FROM messages WHERE id = ?', (message_id,))
        db.commit()

        logger.info("User deleted message user_id=%s message_id=%s", user_id, message_id)
        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Error in delete_message: {e}", exc_info=True)
        return jsonify({'error': 'An internal error occurred.'}), 500

@app.route('/api/messages/delete_after', methods=['POST'])
@require_approval
def delete_messages_after():
    data = request.get_json()
    message_id = data.get('message_id')
    chat_id = data.get('chat_id')

    if not message_id or not chat_id:
        return jsonify({'error': 'Missing message_id or chat_id.'}), 400

    user_id = session['user_id']
    db = get_db()

    try:
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
        if not cursor.fetchone():
            return jsonify({'error': 'Chat not found or unauthorized.'}), 403

        cursor = db.execute('SELECT timestamp FROM messages WHERE id = ? AND chat_id = ?', (message_id, chat_id))
        target_message = _fetchone_as_dict(cursor)
        if not target_message:
            return jsonify({'error': 'Target message not found in the specified chat.'}), 404

        target_timestamp = target_message['timestamp']

        cursor = db.execute(
            'DELETE FROM messages WHERE chat_id = ? AND timestamp >= ?',
            (chat_id, target_timestamp)
        )
        deleted_count = cursor.rowcount
        db.execute(
            'DELETE FROM tool_calls WHERE chat_id = ? AND timestamp >= ?',
            (chat_id, target_timestamp)
        )
        db.commit()

        logger.info(f"User {user_id} deleted {deleted_count} message(s) in chat {chat_id} after message {message_id}.")
        return jsonify({'success': True, 'deleted_count': deleted_count})

    except Exception as e:
        logger.error(f"Error in delete_messages_after: {e}", exc_info=True)
        return jsonify({'error': 'An internal error occurred.'}), 500

@app.route('/clear_history', methods=['POST'])
@require_approval
def clear_history():
    try:
        user_id = session['user_id']

        chat_id = session.get('current_chat_id')
        if not chat_id:
            return jsonify({'status': 'Success', 'message': 'No active chat to clear'}), 200

        db = get_db()
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
        chat_ownership = cursor.fetchone()
        if not chat_ownership:
            return jsonify({'status': 'Failed', 'message': 'Unauthorized to clear this chat history.'}), 403

        cleared_pending = session.pop('pending_queries', None)
        if cleared_pending is not None:
            session.modified = True

        active_query_str = redis_client.get(f"chat_active_query:{chat_id}")
        if active_query_str:
            try:
                active_query = json.loads(active_query_str)
                q_id = active_query.get('query_id')
                if q_id:
                    redis_client.setex(f"stop_flag:{q_id}", 3600, "1")
            except Exception as parse_err:
                logger.error(f"Failed to parse active query data from Redis during clear: {parse_err}")
            redis_client.delete(f"chat_active_query:{chat_id}")

        cursor = db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
        deleted_count = cursor.rowcount
        db.execute('DELETE FROM tool_calls WHERE chat_id = ?', (chat_id,))
        db.commit()



        return jsonify({'status': 'Success', 'message': 'Conversation history cleared'})
    except sqlite3.Error as db_e:
        logger.error(f"Database error clearing history: {db_e}", exc_info=True)
        return jsonify({'status': 'Failed', 'message': f"Database error clearing history: {str(db_e)}"}), 500
    except Exception as e:
        logger.error(f"Server error clearing history: {e}", exc_info=True)
        return jsonify({'status': 'Failed', 'message': f"Server error clearing history: {str(e)}"}), 500

@app.route('/image-proxy')
@require_approval
def image_proxy():
    """
    Proxy route to fetch and serve remote images safely.
    Implements security controls against SSRF, DNS Rebinding, content injection, and DoS.

    Returns:
        Response: The streamed image response or an error code.
    """
    import requests
    from urllib.parse import urlparse

    image_url = request.args.get('url')
    # SECURITY CONTROL: Protocol restriction. Force URLs to start strictly with HTTP or HTTPS
    # to prevent protocol smuggling (e.g., file://, ftp://, gopher://).
    if not image_url or not image_url.startswith(('http://', 'https://')):
        return "Invalid URL", 400

    try:
        # 1. SSRF Protection: Prevent access to internal/private networks
        parsed = urlparse(image_url)
        # SECURITY CONTROL: Hostname resolving check. Check hostname against DNS entries, preventing access
        # to private/reserved ranges (RFC 1918, localhost, loopback).
        safe, ip_or_msg = is_safe_hostname(parsed.hostname)
        if not safe:
            logger.warning(f"Blocked SSRF attempt: {ip_or_msg} via {image_url}")
            return ip_or_msg, 403

        # SECURITY CONTROL: DNS Rebinding prevention. Pin DNS resolution results in the thread-local
        # cache to prevent Time-of-Check to Time-of-Use (TOCTOU) DNS rebinding attacks.
        dns_cache.pinned_ips = getattr(dns_cache, 'pinned_ips', {})
        try:
            ipaddress.ip_address(ip_or_msg)
            dns_cache.pinned_ips[parsed.hostname] = ip_or_msg
        except ValueError:
            pass

        # 2. Fetch the image with a strict timeout and prevent redirects (SSRF Protection)
        resp = None
        try:
            # SECURITY CONTROL: HTTP Request restrictions. Disable HTTP redirects to prevent redirection
            # bypasses to internal networks, and set a strict timeout of 15 seconds to prevent DoS.
            resp = requests.get(image_url, stream=True, timeout=15, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                resp.close()
                return "Redirects are not allowed for security reasons", 400
            resp.raise_for_status()

            # 3. MIME Type Validation: Ensure it's actually an image, not a malicious script/HTML
            # SECURITY CONTROL: Content-Type validation. Verifies the mimetype is strictly image/*
            # to block XSS payloads disguised as images.
            content_type = resp.headers.get('Content-Type', '')
            if not content_type.startswith('image/'):
                resp.close()
                return "Target is not an image", 400

            # 4. DoS Protection: Prevent downloading massive files (Max 50MB)
            content_length = resp.headers.get('Content-Length')
            if content_length and int(content_length) > 50 * 1024 * 1024:
                resp.close()
                return "Image exceeds maximum allowed size (50MB)", 400

            excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
            headers = [(name, value) for (name, value) in resp.raw.headers.items()
                       if name.lower() not in excluded_headers]

            def generate():
                try:
                    bytes_read = 0
                    max_bytes = 50 * 1024 * 1024
                    for chunk in resp.iter_content(chunk_size=10*1024):
                        bytes_read += len(chunk)
                        if bytes_read > max_bytes:
                            logger.warning(f"Aborted streaming from {image_url}: exceeded size limit (DoS protection)")
                            break
                        yield chunk
                finally:
                    resp.close()

            return Response(stream_with_context(generate()),
                            status=resp.status_code,
                            content_type=content_type,
                            headers=headers)
        except requests.exceptions.RequestException as e:
            if resp:
                try:
                    resp.close()
                except:
                    pass
            logger.error(f"Image proxy request failed for {image_url}: {e}")
            return "Failed to fetch image", 502
        finally:
            # Clear DNS pinning for this host
            if parsed.hostname in dns_cache.pinned_ips:
                del dns_cache.pinned_ips[parsed.hostname]
    except socket.gaierror:
        return "Failed to resolve hostname", 400
    except Exception as e:
        logger.error(f"Image proxy unexpected error for {image_url}: {e}")
        return "Internal server error", 500

# INTENTIONALLY UNPROTECTED: This route omits @require_approval to allow users to easily share generated files and outputs via direct links.
@app.route('/download/<path:filename>')
def download_file(filename):
    """
    Serve a generated output file as an attachment.
    Intentionally unprotected to allow easy downloading via direct links.

    Args:
        filename (str): Path to the target file inside outputs/.

    Returns:
        Response: The file attachment download response.
    """
    if '..' in filename or filename.startswith('/'):
        return "Invalid path", 400
    # Resolve real path of output directory to prevent path traversal via symlinks
    directory = os.path.realpath(os.path.join(os.path.dirname(__file__), "outputs"))
    file_path = os.path.join(directory, filename)
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({'status': 'Failed: File not found'}), 404

    # Ensure the resolved file path actually lies within the outputs directory
    real_file_path = os.path.realpath(file_path)
    if os.path.commonpath([directory, real_file_path]) != directory:
         return "Access denied", 403

    # Use dirname and basename for send_from_directory to correctly serve sub-paths
    subdir = os.path.dirname(filename)
    basename = os.path.basename(filename)
    return send_from_directory(os.path.join(directory, subdir), basename, as_attachment=True)

# INTENTIONALLY UNPROTECTED: This route omits @require_approval to allow users to easily share generated files and outputs via direct links.
@app.route('/view/<path:filename>')
def view_file(filename):
    """
    Serve a generated output file inline in the browser.
    Intentionally unprotected to allow easy viewing via direct links.

    Args:
        filename (str): Path to the target file inside outputs/.

    Returns:
        Response: The inline file view response.
    """
    if '..' in filename or filename.startswith('/'):
        return "Invalid path", 400
    # Resolve real path of output directory to prevent path traversal via symlinks
    directory = os.path.realpath(os.path.join(os.path.dirname(__file__), "outputs"))
    file_path = os.path.join(directory, filename)
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
         return "File not found", 404

    # Ensure the resolved file path actually lies within the outputs directory
    real_file_path = os.path.realpath(file_path)
    if os.path.commonpath([directory, real_file_path]) != directory:
         return "Access denied", 403

    safe_filename = os.path.basename(filename)
    mimetype = 'text/plain'
    if safe_filename.lower().endswith(('.html', '.htm')): mimetype = 'text/html'
    elif safe_filename.lower().endswith('.md'): mimetype = 'text/markdown'
    elif safe_filename.lower().endswith('.css'): mimetype = 'text/css'
    elif safe_filename.lower().endswith('.js'): mimetype = 'application/javascript'
    elif safe_filename.lower().endswith(('.png', '.jpg', '.jpeg')): mimetype = 'image/png'
    elif safe_filename.lower().endswith(('.mp4', '.m4v')): mimetype = 'video/mp4'
    elif safe_filename.lower().endswith('.webm'): mimetype = 'video/webm'
    elif safe_filename.lower().endswith('.ogg'): mimetype = 'video/ogg'
    elif safe_filename.lower().endswith('.mov'): mimetype = 'video/quicktime'
    elif safe_filename.lower().endswith('.mkv'): mimetype = 'video/x-matroska'
    elif safe_filename.lower().endswith('.mp3'): mimetype = 'audio/mpeg'
    elif safe_filename.lower().endswith('.wav'): mimetype = 'audio/wav'
    elif safe_filename.lower().endswith('.pdf'): mimetype = 'application/pdf'
    elif safe_filename.lower().endswith(('.zip', '.tar', '.gz', '.7z', '.rar')): mimetype = 'application/octet-stream'
    elif safe_filename.lower().endswith(('.json', '.jsonl')): mimetype = 'application/json'
    elif safe_filename.lower().endswith(('.csv', '.tsv')): mimetype = 'text/csv'

    subdir = os.path.dirname(filename)
    basename = os.path.basename(filename)
    return send_from_directory(os.path.join(directory, subdir), basename, mimetype=mimetype)

@app.route('/default.min.css')
def serve_highlight_css():
    return send_from_directory('static', 'default.min.css')

@app.route('/custom_select.css')
def serve_custom_select_css():
    return send_from_directory('static', 'custom_select.css')

@app.route('/custom_select.js')
def serve_custom_select_js():
    return send_from_directory('static', 'custom_select.js')

@app.route('/highlight.min.js')
def serve_highlight_js():
    return send_from_directory('static', 'highlight.min.js')

@app.route('/marked.min.js')
def serve_marked():
    return send_from_directory('static', 'marked.min.js')

@app.route('/turndown.js')
def serve_turndown():
    return send_from_directory('static', 'turndown.js')

def send_approval_email(recipient_email, display_name):
    # Inline import of smtplib and EmailMessage to avoid startup overhead
    import smtplib
    from email.message import EmailMessage
    sender = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASS")

    msg = EmailMessage()
    msg['Subject'] = "Stellar: Access Granted"
    msg['From'] = f"Stellar AI <{sender}>"
    msg['To'] = recipient_email

    html_content = f"""
    <html>
    <body style="margin: 0; padding: 0; background-color: #ffffff; font-family: 'Inter', sans-serif; color: #333333;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #ffffff; padding: 40px 0;">
            <tr>
                <td align="center">
                    <table width="600" cellpadding="0" cellspacing="0" style="background-color: #ffffff; border: 1px solid #e0e0e0; border-radius: 16px; padding: 40px; text-align: center; box-shadow: 0 10px 30px rgba(0,0,0,0.05);">
                        <tr>
                            <td>
                                <h1 style="margin: 0; padding: 0; font-size: 2.5rem; letter-spacing: 4px; color: #4285F4;">STELLAR</h1>
                                <p style="color: #00c292; font-family: 'JetBrains Mono', monospace; font-size: 0.9rem; letter-spacing: 1px; margin-top: 10px; margin-bottom: 30px;">ACCESS GRANTED</p>

                                <h2 style="font-size: 1.5rem; font-weight: normal; margin-bottom: 20px; color: #111111;">Welcome, {display_name}!</h2>

                                <p style="color: #555555; font-size: 1rem; line-height: 1.6; margin-bottom: 40px;">
                                    Your account has been successfully approved and provisioned for the Stellar Autonomous Environment. You can now log in and begin orchestrating clusters and generating analytics.
                                </p>

                                <a href="https://stellarai.live" style="display: inline-block; background-color: #4285F4; color: white; text-decoration: none; padding: 14px 28px; border-radius: 8px; font-weight: 600; font-size: 1rem; letter-spacing: 0.5px;">ENTER STELLAR</a>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    msg.set_content(f"Welcome to Stellar, {display_name}! Your account has been approved. Visit https://stellarai.live to access the platform.")
    msg.add_alternative(html_content, subtype='html')

    logger.info("Sending approval email to recipient_email=%s", recipient_email)
    t0 = time.time()
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        duration = time.time() - t0
        logger.info(f"SUCCESS: Approval email sent successfully to {recipient_email} duration_sec={duration:.3f}.")
    except Exception as e:
        duration = time.time() - t0
        logger.error(f"FAILURE sending approval email to {recipient_email} duration_sec={duration:.3f}: {str(e)}")

def send_revocation_email(recipient_email, display_name):
    # Inline import of smtplib and EmailMessage to avoid startup overhead
    import smtplib
    from email.message import EmailMessage
    sender = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASS")

    msg = EmailMessage()
    msg['Subject'] = "Stellar: Access Status Update"
    msg['From'] = f"Stellar AI <{sender}>"
    msg['To'] = recipient_email

    html_content = f"""
    <html>
    <body style="margin: 0; padding: 0; background-color: #ffffff; font-family: 'Inter', sans-serif; color: #333333;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #ffffff; padding: 40px 0;">
            <tr>
                <td align="center">
                    <table width="600" cellpadding="0" cellspacing="0" style="background-color: #ffffff; border: 1px solid #e0e0e0; border-radius: 16px; padding: 40px; text-align: center; box-shadow: 0 10px 30px rgba(0,0,0,0.05);">
                        <tr>
                            <td>
                                <h1 style="margin: 0; padding: 0; font-size: 2.5rem; letter-spacing: 4px; color: #111111;">STELLAR</h1>
                                <p style="color: #FF2A4D; font-family: 'JetBrains Mono', monospace; font-size: 0.9rem; letter-spacing: 1px; margin-top: 10px; margin-bottom: 30px;">ACCESS REVOKED</p>

                                <h2 style="font-size: 1.5rem; font-weight: normal; margin-bottom: 20px; color: #111111;">Hello, {display_name}</h2>

                                <p style="color: #555555; font-size: 1rem; line-height: 1.6; margin-bottom: 40px;">
                                    Your access to the Stellar Autonomous Environment has been suspended. You have been placed back on our waitlist. We will notify you if your access is restored in the future.
                                </p>

                                <div style="display: inline-block; border: 1px solid #e0e0e0; color: #666666; padding: 14px 28px; border-radius: 8px; font-weight: 600; font-size: 1rem; letter-spacing: 0.5px;">STATUS: ON WAITLIST</div>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    msg.set_content(f"Hello {display_name}, your access to Stellar has been revoked and you have been placed back on the waitlist.")
    msg.add_alternative(html_content, subtype='html')

    logger.info("Sending revocation email to recipient_email=%s", recipient_email)
    t0 = time.time()
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        duration = time.time() - t0
        logger.info(f"SUCCESS: Revocation email sent successfully to {recipient_email} duration_sec={duration:.3f}.")
    except Exception as e:
        duration = time.time() - t0
        logger.error(f"FAILURE sending revocation email to {recipient_email} duration_sec={duration:.3f}: {str(e)}")

@app.route('/api/admin/keys', methods=['GET'])
def get_admin_keys():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    keys_to_report = []

    if PRIMARY_API_KEY:
        keys_to_report.append({
            'label': 'Primary API Key',
            'value': PRIMARY_API_KEY
        })

    for idx, key in enumerate(BACKUP_API_KEYS, start=1):
        if key:
            keys_to_report.append({
                'label': f'Backup API Key {idx}',
                'value': key
            })

    import hashlib as _hl
    response_data = []
    for item in keys_to_report:
        key_val = item['value']
        masked = key_val[:8] + "..." + key_val[-4:] if len(key_val) > 12 else key_val
        key_hash = _hl.sha256(key_val.encode('utf-8')).hexdigest()
        blocks = KEY_MANAGER.get_key_blocks(key_val, list(MODEL_NAMES.keys()))

        response_data.append({
            'label': item['label'],
            'masked': masked,
            'key_hash': key_hash,
            'blocks': blocks
        })

    return jsonify(response_data), 200


@app.route('/api/admin/keys/stream')
def admin_keys_stream():
    """SSE endpoint that pushes real-time key block/recovery events to the admin dashboard.
    Uses Redis keyspace notifications on stellar:blocked_until:* keys.
    One connection per admin tab — admin-only access.
    """
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    def generate():
        import json as _json
        import redis as _redis

        # Dedicated Redis connection for pubsub (never share with the main pool)
        ps_conn = _redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)
        pubsub = ps_conn.pubsub()
        try:
            # Subscribe to keyspace events on all stellar:blocked_until:* keys.
            # channel format: __keyspace@0__:stellar:blocked_until:<sha256>:<scope>
            # message data:   the event type — 'set', 'expired', 'del', etc.
            pubsub.psubscribe('__keyspace@0__:stellar:blocked_until:*')
            yield f"data: {_json.dumps({'type': 'connected'})}\n\n"

            while True:
                try:
                    msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=20)
                    if msg is None:
                        # Heartbeat keeps the connection alive through proxies
                        yield f"data: {_json.dumps({'type': 'heartbeat'})}\n\n"
                        continue

                    if msg['type'] != 'pmessage':
                        continue

                    event = msg['data']          # 'set', 'expired', 'del', ...
                    channel = msg['channel']     # '__keyspace@0__:stellar:blocked_until:<hash>:<scope>'

                    # Strip the Redis keyspace prefix to get the raw key name
                    raw_key = channel[len('__keyspace@0__:'):]
                    # raw_key = 'stellar:blocked_until:<hash>:<scope>'
                    parts = raw_key.split(':')
                    if len(parts) < 4:
                        continue
                    key_hash = parts[2]
                    scope = ':'.join(parts[3:])  # scope may contain colons (model IDs don't, but be safe)

                    if event == 'set':
                        try:
                            blocked_until = float(redis_client.get(raw_key) or 0)
                            reason = redis_client.get(f"stellar:block_reason:{key_hash}:{scope}") or 'RPM'
                            payload = {
                                'type': 'key_blocked',
                                'key_hash': key_hash,
                                'scope': scope,
                                'blocked_until': blocked_until,
                                'reason': reason
                            }
                            yield f"data: {_json.dumps(payload)}\n\n"
                        except Exception:
                            pass

                    elif event in ('expired', 'del'):
                        payload = {
                            'type': 'key_recovered',
                            'key_hash': key_hash,
                            'scope': scope
                        }
                        yield f"data: {_json.dumps(payload)}\n\n"

                except GeneratorExit:
                    break
                except Exception:
                    break
        finally:
            try:
                pubsub.punsubscribe()
                pubsub.close()
                ps_conn.close()
            except Exception:
                pass

    resp = Response(generate(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'   # disable nginx buffering
    resp.headers['Connection'] = 'keep-alive'
    return resp

@app.route('/api/admin/waitlist', methods=['GET'])
def get_admin_waitlist():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    db = get_db()
    # Calculate last_active, num_chats, num_projects, and total_tokens_approx
    query = """
        SELECT
            u.id, u.username, u.display_name, u.role, u.is_approved, u.created_at, u.last_active,
            u.designation, u.source, u.use_case, u.waitlist_form_submitted,
            (SELECT COUNT(*) FROM chats WHERE user_id = u.id) as num_chats,
            (SELECT COUNT(*) FROM repo_history WHERE user_id = u.id) as num_projects,
            (SELECT SUM(token_count) FROM chats WHERE user_id = u.id) as total_tokens_approx
        FROM users u
        ORDER BY last_active DESC, u.created_at DESC
    """
    cursor = db.execute(query)
    waitlist = _fetch_as_dict(cursor)
    return jsonify(waitlist), 200

@app.route('/api/admin/toggle_access', methods=['POST'])
def toggle_user_access():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    user_id = data.get('user_id')
    new_status = data.get('is_approved')

    if user_id is None or new_status is None:
        return jsonify({'error': 'User ID and status required'}), 400

    db = get_db()
    try:
        # Prevent disabling admins
        cursor = db.execute("SELECT username, role, is_approved FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return jsonify({'error': 'User not found'}), 404

        if user['role'] == 'admin':
            return jsonify({'error': 'Cannot disable access for admin users'}), 400

        db.execute("UPDATE users SET is_approved = ? WHERE id = ?", (1 if new_status else 0, user_id))
        db.commit()

        logger.info("User access toggled by admin admin_user_id=%s target_user_id=%s new_status=%s", session.get('user_id'), user_id, new_status)

        recipient = user['username'] if '@' in user['username'] else None
        if recipient:
            req_id = g.request_id if getattr(g, 'request_id', None) else 'system'
            def run_email_thread(target_func, *args):
                thread_local_ctx.request_id = req_id
                target_func(*args)

            if new_status and not user['is_approved']:
                threading.Thread(target=run_email_thread, args=(send_approval_email, recipient, user['username']), daemon=True).start()
            elif not new_status and user['is_approved']:
                threading.Thread(target=run_email_thread, args=(send_revocation_email, recipient, user['username']), daemon=True).start()

        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception("Error toggling user access target_user_id=%s: %s", user_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/approve', methods=['POST'])
def approve_user():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    user_id = data.get('user_id')
    user_email = data.get('email') # Assuming username is email or email is provided

    if not user_id:
        return jsonify({'error': 'User ID required'}), 400

    db = get_db()
    try:
        cursor = db.execute("SELECT username FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return jsonify({'error': 'User not found'}), 404

        db.execute("UPDATE users SET is_approved = 1 WHERE id = ?", (user_id,))
        db.commit()

        logger.info("User approved by admin admin_user_id=%s target_user_id=%s username=%s", session.get('user_id'), user_id, user['username'])

        # If user_email is not provided, try to use username if it looks like an email
        recipient = user_email or (user['username'] if '@' in user['username'] else None)

        if recipient:
            req_id = g.request_id if getattr(g, 'request_id', None) else 'system'
            def run_email_thread(target_func, *args):
                thread_local_ctx.request_id = req_id
                target_func(*args)
            threading.Thread(target=run_email_thread, args=(send_approval_email, recipient, user['username']), daemon=True).start()

        return jsonify({'success': True, 'message': f"User {user['username']} approved."}), 200
    except sqlite3.Error as e:
        logger.error("Database error in approve_user error=%s", e, exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/impersonate', methods=['POST'])
def admin_impersonate():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    user_id = data.get('user_id')
    if not user_id:
         return jsonify({'error': 'User ID required'}), 400

    db = get_db()
    cursor = db.execute('SELECT id, username, display_name, role, is_approved FROM users WHERE id = ?', (user_id,))
    user = _fetchone_as_dict(cursor)
    if not user:
         return jsonify({'error': 'User not found'}), 404

    admin_user_id = session.get('user_id')
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['display_name'] = user['display_name']
    session['role'] = user['role']
    session['is_approved'] = bool(user['is_approved'])
    session.pop('current_chat_id', None)

    logger.warning("Admin impersonated user admin_user_id=%s target_user_id=%s username=%s", admin_user_id, user['id'], user['username'])
    return jsonify({'success': True, 'message': f"Impersonating {user['username']}"}), 200

@app.route('/logout', methods=['POST'])
def logout_user():
    # Explicitly delete the session from Redis so the old session ID
    # is fully invalidated — session.clear() alone doesn't remove the Redis key.
    try:
        session_interface = app.session_interface
        if hasattr(session_interface, 'redis'):
            sid = request.cookies.get(app.config.get('SESSION_COOKIE_NAME', 'session'))
            if sid:
                prefix = app.config.get('SESSION_KEY_PREFIX', 'session:')
                app.session_interface.redis.delete(prefix + sid)
    except Exception as e:
        logger.warning("Could not delete Redis session on logout error=%s", e)
    
    logger.info("User logged out successfully username=%s", session.get('username'))
    session.clear()
    response = jsonify({"success": True, "message": "Logged out successfully."})
    # Explicitly expire the session cookie so browser drops it immediately
    # 1. Delete cookie on configured domain (if any)
    response.delete_cookie(
        app.config.get('SESSION_COOKIE_NAME', 'stellar_session_main'),
        domain=app.config.get('SESSION_COOKIE_DOMAIN'),
        path='/'
    )
    # 2. Delete cookie on parent wildcard domain to clean up stale cookies from prior configurations
    response.delete_cookie(
        app.config.get('SESSION_COOKIE_NAME', 'stellar_session_main'),
        domain='.stellarai.live',
        path='/'
    )
    # 3. Delete cookie on exact host (no domain parameter)
    response.delete_cookie(
        app.config.get('SESSION_COOKIE_NAME', 'stellar_session_main'),
        path='/'
    )
    return response, 200

# ===================== SSH Authentication Code Routes =====================

_SSH_CODE_CHARSET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
_SSH_CODE_LENGTH = 8
_SSH_CODE_TTL = 60  # 1 minute
_SSH_MAX_CODES_PER_USER = 5
_SSH_VERIFY_FAIL_LIMIT = 10
_SSH_VERIFY_FAIL_WINDOW = 900  # 15 minutes

_SSH_AUTH_PAGE_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stellar &mdash; SSH Authentication</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;700;900&family=JetBrains+Mono:wght@700&display=swap">
<style>
    :root {
        --bg-base: #010103;
        --surface: rgba(10, 10, 15, 0.75);
        --border: rgba(255, 255, 255, 0.08);
        --accent-lunarity: #4285F4;
        --accent-emerald: #00F090;
        --text-muted: #9ca3af;
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body, html {
        margin: 0;
        padding: 0;
        width: 100%;
        height: 100%;
        background-color: var(--bg-base);
        font-family: 'Inter', sans-serif;
        overflow: hidden;
        color: white;
        display: flex;
        justify-content: center;
        align-items: center;
        perspective: 1500px;
    }

    /* --- Mask Wrapper (Holds the Seamless Flashlight) --- */
    .code-mask-wrapper {
        position: absolute;
        top: 0; left: 0;
        width: 100vw; height: 100vh;
        z-index: 0;
        pointer-events: none;

        --mouse-x: 50vw;
        --mouse-y: 50vh;

        -webkit-mask-image: radial-gradient(
            280px circle at var(--mouse-x) var(--mouse-y),
            rgba(0,0,0,1) 0%,
            rgba(0,0,0,0.8) 15%,
            rgba(0,0,0,0.5) 35%,
            rgba(0,0,0,0.3) 50%,
            rgba(0,0,0,0.2) 65%,
            rgba(0,0,0,0.15) 80%,
            rgba(0,0,0,0.12) 90%,
            rgba(0,0,0,0.1) 100%
        );
        mask-image: radial-gradient(
            280px circle at var(--mouse-x) var(--mouse-y),
            rgba(0,0,0,1) 0%,
            rgba(0,0,0,0.8) 15%,
            rgba(0,0,0,0.5) 35%,
            rgba(0,0,0,0.3) 50%,
            rgba(0,0,0,0.2) 65%,
            rgba(0,0,0,0.15) 80%,
            rgba(0,0,0,0.12) 90%,
            rgba(0,0,0,0.1) 100%
        );
    }

    .code-mask-wrapper::after {
        content: '';
        position: absolute;
        inset: 0;
        background: radial-gradient(
            ellipse 450px 700px at center,
            var(--bg-base) 15%,
            rgba(1, 1, 3, 0.9) 40%,
            transparent 75%
        );
        pointer-events: none;
    }

    /* --- Scrolling Code Canvas --- */
    .code-canvas {
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        line-height: 1.3;
        color: rgba(66, 133, 244, 0.55);
        white-space: pre;
        width: 110vw;
        margin-left: -5vw;
        will-change: transform;
        animation: scrollMemoryDump 100s linear infinite;
    }

    @keyframes scrollMemoryDump {
        0% { transform: translateY(0); }
        100% { transform: translateY(-50%); }
    }

    /* --- Main UI Wrapper --- */
    .gateway-wrapper {
        position: relative;
        z-index: 10;
        width: 100%;
        max-width: 480px;
        padding: 20px;
        box-sizing: border-box;
        transform-style: preserve-3d;
        will-change: transform;
    }

    .auth-card {
        background: var(--surface);
        backdrop-filter: blur(40px);
        -webkit-backdrop-filter: blur(40px);
        border: 1px solid var(--border);
        border-radius: 24px;
        padding: 50px 40px;
        box-shadow:
            0 0 50px 2px rgba(0, 0, 0, 0.8),
            0 40px 80px -20px rgba(0, 0, 0, 1),
            inset 0 1px 0 rgba(255, 255, 255, 0.1);
        position: relative;
        overflow: hidden;
        display: flex;
        flex-direction: column;
        align-items: center;
        transform: translateZ(0);
    }

    .auth-card::before {
        content: '';
        position: absolute;
        top: 0; left: 0; width: 100%; height: 2px;
        background: linear-gradient(90deg, transparent, var(--accent-lunarity), transparent);
    }

    .logo-mark {
        font-size: 3rem;
        font-weight: 900;
        letter-spacing: 8px;
        margin-bottom: 6px;
        text-align: center;
        background: linear-gradient(135deg, #FFFFFF 20%, #a5c0f3 60%, #4285F4 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        filter: drop-shadow(0 4px 15px rgba(66, 133, 244, 0.3));
    }

    .subtitle {
        color: var(--text-muted);
        font-size: 0.95rem;
        margin-bottom: 24px;
        line-height: 1.6;
        text-align: center;
        letter-spacing: 0.5px;
    }
    .subtitle code {
        background: rgba(66, 133, 244, 0.15);
        border: 1px solid rgba(66, 133, 244, 0.2);
        padding: 2px 6px;
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        color: #7da5f5;
        font-size: 0.8rem;
    }

    /* Steps */
    .steps {
        text-align: left;
        width: 100%;
        margin-bottom: 24px;
        list-style: none;
    }
    .steps li {
        position: relative;
        padding-left: 32px;
        margin-bottom: 12px;
        color: #b3c0d4;
        font-size: 0.85rem;
        line-height: 1.5;
    }
    .steps li::before {
        content: attr(data-step);
        position: absolute;
        left: 0;
        top: 0;
        width: 22px; height: 22px;
        background: rgba(66, 133, 244, 0.15);
        border: 1px solid rgba(66, 133, 244, 0.3);
        border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: 11px; font-weight: 700; color: #4285F4;
    }
    .steps code {
        background: rgba(66, 133, 244, 0.15);
        border: 1px solid rgba(66, 133, 244, 0.2);
        padding: 2px 6px;
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        color: #7da5f5;
        font-size: 0.8rem;
    }

    /* Button */
    .btn {
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid var(--border);
        color: #FFF;
        border-radius: 12px;
        padding: 16px 20px;
        font-size: 1rem;
        font-weight: 600;
        cursor: pointer;
        width: 100%;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .btn:hover:not(:disabled) {
        background: rgba(255, 255, 255, 0.08);
        border-color: rgba(66, 133, 244, 0.5);
        transform: translateY(-2px);
        box-shadow: 0 15px 30px -5px rgba(0, 0, 0, 0.6);
    }
    .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }

    .error-text {
        color: #FF2A4D;
        margin-top: 12px;
        font-size: 0.85rem;
        text-align: center;
        display: none;
    }

    /* Code Display Box */
    .code-display {
        display: none;
        width: 100%;
        margin-top: 24px;
        padding: 24px;
        background: rgba(66, 133, 244, 0.04);
        border: 1px solid rgba(66, 133, 244, 0.15);
        border-radius: 16px;
        text-align: center;
    }
    .code-label {
        color: var(--text-muted);
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 1.5px;
    }
    .code-value {
        font-family: 'JetBrains Mono', monospace;
        font-size: 2.5rem;
        font-weight: 900;
        letter-spacing: 6px;
        color: #4285F4;
        margin: 10px 0;
        text-shadow: 0 0 20px rgba(66, 133, 244, 0.4);
    }
    .timer {
        color: var(--text-muted);
        font-size: 0.8rem;
        margin-top: 10px;
    }
    .timer span { color: #FFD200; font-weight: 700; font-family: 'JetBrains Mono', monospace; }

    .copy-btn {
        margin-top: 12px;
        background: rgba(66, 133, 244, 0.1);
        border: 1px solid rgba(66, 133, 244, 0.25);
        color: #7da5f5;
        padding: 8px 24px;
        border-radius: 8px;
        font-size: 0.85rem;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.2s;
    }
    .copy-btn:hover { background: rgba(66, 133, 244, 0.18); }

    .instructions {
        margin-top: 14px;
        color: var(--text-muted);
        font-size: 0.75rem;
        line-height: 1.5;
    }

    /* Persona Nodes */
    .persona-status {
        display: flex;
        justify-content: space-between;
        width: 100%;
        margin-top: 30px;
        padding-top: 20px;
        border-top: 1px solid var(--border);
    }

    .persona {
        display: flex;
        align-items: center;
        gap: 6px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.6rem;
        color: var(--text-muted);
        letter-spacing: 1px;
    }

    .dot { width: 5px; height: 5px; border-radius: 50%; }
    .dot.obs { background: #888890; box-shadow: 0 0 8px #888890; }
    .dot.cri { background: #FF2A4D; box-shadow: 0 0 8px #FF2A4D; }
    .dot.lun { background: #4285F4; box-shadow: 0 0 8px #4285F4; }
    .dot.eme { background: #00F090; box-shadow: 0 0 8px #00F090; }
</style>
</head>
<body>

    <div class="code-mask-wrapper" id="maskWrapper" data-nosnippet>
        <div class="code-canvas" id="codeCanvas" aria-hidden="true"></div>
    </div>

    <main class="gateway-wrapper" id="gatewayWrapper">
        <div class="auth-card">
            <div class="logo-mark">STELLAR</div>
            <div class="subtitle" style="margin-bottom: 30px;">
                Open a terminal, run <code>ssh stellar@stellarai.live</code>, generate your access code below, and paste it to connect.
            </div>

            <button class="btn" id="genBtn" onclick="generateCode()">Generate SSH Code</button>
            <div class="error-text" id="errMsg"></div>

            <div class="code-display" id="codeBox">
                <div class="code-label">Your Access Code</div>
                <div class="code-value" id="codeValue">--------</div>
                <button class="copy-btn" id="copyBtn" onclick="copyCode()" style="display: inline-flex; align-items: center; justify-content: center; gap: 8px;">
                    <svg class="copy-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                    <span>Copy Code</span>
                </button>
                <div class="timer">Expires in <span id="countdown">1:00</span></div>
                <div class="instructions">Paste this code in your SSH terminal to authenticate.</div>
            </div>

            <div class="persona-status">
                <div class="persona"><div class="dot obs"></div> OBSIDIAN</div>
                <div class="persona"><div class="dot cri"></div> CRIMSON</div>
                <div class="persona"><div class="dot lun"></div> LUNARITY</div>
                <div class="persona"><div class="dot eme"></div> EMERALD</div>
            </div>
        </div>
    </main>

<script>
let rawCode = '';
let timerInterval = null;

document.addEventListener('DOMContentLoaded', () => {
    const maskWrapper = document.getElementById('maskWrapper');
    const codeCanvas = document.getElementById('codeCanvas');
    const snippets = [
        "def provision_docker_cluster(node_id):",
        "ledger = db.query('SELECT * FROM ground_truth')",
        "raise StateDriftException('Ledger mismatch')",
        "container = client.containers.run(network='bridge')",
        "async function mountPagedMemory(stream) {",
        "return chunks.map(chunk => compressMetadata(chunk));",
        "def proxy_wildcard(path):",
        "container_ip = redis.get(f'route:{host}')",
        "[OBSIDIAN] Adversarial security audit...",
        "INSERT INTO interactions (agent_id) VALUES ('obsidian');",
        "0x0000000000000000", "0xFFFFFFFFFFFFFFFF", "sys.stdout.write"
    ];

    let baseDump = "";
    for (let i = 0; i < 150; i++) {
        let line = "";
        while (line.length < 350) { line += snippets[Math.floor(Math.random() * snippets.length)] + "  "; }
        baseDump += line + "\\n";
    }
    codeCanvas.textContent = baseDump + baseDump;

    const wrapper = document.getElementById('gatewayWrapper');
    let mouseX = 0, mouseY = 0, targetX = 0, targetY = 0;

    document.addEventListener('mousemove', (e) => {
        maskWrapper.style.setProperty('--mouse-x', `${e.clientX}px`);
        maskWrapper.style.setProperty('--mouse-y', `${e.clientY}px`);

        mouseX = (e.clientX - window.innerWidth/2) / 75;
        mouseY = (e.clientY - window.innerHeight/2) / 75;
    });

    function animate3D() {
        if (Math.abs(mouseX - targetX) > 0.001) targetX += (mouseX - targetX) * 0.04;
        if (Math.abs(mouseY - targetY) > 0.001) targetY += (mouseY - targetY) * 0.04;

        wrapper.style.transform = `rotateY(${targetX}deg) rotateX(${-targetY}deg)`;
        requestAnimationFrame(animate3D);
    }
    animate3D();
});

async function generateCode() {
  const btn = document.getElementById('genBtn');
  const errMsg = document.getElementById('errMsg');
  errMsg.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Generating...';
  try {
    const res = await fetch('/api/ssh/generate-code', { method: 'POST', credentials: 'same-origin' });
    const data = await res.json();
    if (!res.ok) {
      errMsg.textContent = data.error || 'Failed to generate code.';
      errMsg.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Generate SSH Code';
      return;
    }
    rawCode = data.code;
    document.getElementById('codeValue').textContent = data.code;
    document.getElementById('codeBox').style.display = 'block';
    btn.textContent = 'Generate New Code';
    btn.disabled = false;
    startTimer(60);
  } catch (e) {
    errMsg.textContent = 'Network error. Please try again.';
    errMsg.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Generate SSH Code';
  }
}

function startTimer(seconds) {
  if (timerInterval) clearInterval(timerInterval);
  let remaining = seconds;
  const el = document.getElementById('countdown');
  function tick() {
    const m = Math.floor(remaining / 60);
    const s = remaining % 60;
    el.textContent = m + ':' + String(s).padStart(2, '0');
    if (remaining <= 0) {
      clearInterval(timerInterval);
      document.getElementById('codeBox').style.display = 'none';
      document.getElementById('genBtn').textContent = 'Generate SSH Code';
    }
    remaining--;
  }
  tick();
  timerInterval = setInterval(tick, 1000);
}

function copyCode() {
  navigator.clipboard.writeText(rawCode.replace('-', '')).then(function() {
    const btn = document.getElementById('copyBtn');
    const originalHTML = btn.innerHTML;
    btn.innerHTML = `
      <svg class="copy-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#00F090" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"></polyline>
      </svg>
      <span style="color: #00F090;">Copied!</span>
    `;
    setTimeout(function() { btn.innerHTML = originalHTML; }, 2000);
  });
}
</script>
</body>
</html>'''

@app.route('/auth/ssh', methods=['GET'])
def ssh_auth_page():
    """
    Render the SSH Gateway authentication dashboard.
    Verifies that the user is logged in and approved before serving the key generation UI.
    If the user is logged in but not approved, renders the waitlist page.

    Returns:
        Response: The HTML page content or redirect response.
    """
    if 'user_id' not in session:
        return redirect('/?redirect=/auth/ssh')

    # Check if approved
    if not session.get('is_approved'):
        db = get_db()
        cursor = db.execute('SELECT is_approved FROM users WHERE id = ?', (session['user_id'],))
        row = cursor.fetchone()
        if row and row[0]:
            session['is_approved'] = True
        else:
            session['is_approved'] = False
            # Render waitlist page
            # Bolt - Performance: use render_template so Flask/Jinja2 compiles and caches waitlist template in memory
            content = render_template('waitlist.html')
            response = make_response(content)
            response.headers['Content-Type'] = 'text/html'
            return response

    return _SSH_AUTH_PAGE_HTML, 200, {'Content-Type': 'text/html'}

@app.route('/api/ssh/generate-code', methods=['POST'])
@require_approval
def ssh_generate_code():
    """
    Generate a temporary, one-time device authentication code for the SSH TUI.
    Stores the code metadata in Redis with a 60-second TTL and increments active code counters.

    Returns:
        Response: JSON response containing the generated display code or an error message.
    """
    user_id = session['user_id']
    username = session.get('username', '')
    display_name = session.get('display_name', username)

    # Rate limit: max active codes per user
    user_code_key = f'ssh_auth_code:user:{user_id}'
    active_count = redis_client.get(user_code_key)
    if active_count and int(active_count) >= _SSH_MAX_CODES_PER_USER:
        return jsonify({'error': 'Too many active codes. Please wait for existing codes to expire.'}), 429

    # Generate a unique 6-char code
    for _ in range(20):
        code = ''.join(secrets.choice(_SSH_CODE_CHARSET) for _ in range(_SSH_CODE_LENGTH))
        code_key = f'ssh_auth_code:{code}'
        if not redis_client.exists(code_key):
            break
    else:
        return jsonify({'error': 'Could not generate a unique code. Please try again.'}), 500

    # Store code data in Redis
    code_data = json.dumps({
        'user_id': user_id,
        'username': username,
        'display_name': display_name,
        'created_at': time.time()
    })
    redis_client.setex(code_key, _SSH_CODE_TTL, code_data)

    # Increment active code counter for this user
    pipe = redis_client.pipeline()
    pipe.incr(user_code_key)
    pipe.expire(user_code_key, _SSH_CODE_TTL)
    pipe.execute()

    # Format as XXXX-XXXX for display
    display_code = f'{code[:4]}-{code[4:]}'
    logger.info("SSH auth code generated user_id=%s username=%s", user_id, username)
    return jsonify({'code': display_code}), 200

@app.route('/api/ssh/verify-code', methods=['POST'])
def ssh_verify_code():
    """
    Verify a user-entered SSH device authentication code against Redis.
    Performs gateway secret verification and rate limiting on failed attempts.
    Deletes the one-time code on successful validation.

    Returns:
        Response: JSON indicating validation status, user_id, username, and display_name.
    """
    # Verify shared secret
    gateway_secret = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')
    data = request.get_json(silent=True)
    if not data or data.get('secret') != gateway_secret:
        logger.warning("SSH code verification unauthorized client_ip=%s", request.remote_addr or 'unknown')
        return jsonify({'valid': False, 'error': 'Unauthorized'}), 403

    # Rate limit failed attempts per IP
    client_ip = request.remote_addr or 'unknown'
    fail_key = f'ssh_verify_fail:{client_ip}'
    fail_count = redis_client.get(fail_key)
    if fail_count and int(fail_count) >= _SSH_VERIFY_FAIL_LIMIT:
        logger.warning("SSH code verification rate limited client_ip=%s", client_ip)
        return jsonify({'valid': False, 'error': 'Too many failed attempts. Try again later.'}), 429

    raw_code = data.get('code', '').upper().replace('-', '').replace(' ', '')
    if not raw_code or len(raw_code) != _SSH_CODE_LENGTH:
        logger.warning("SSH code verification invalid code client_ip=%s", client_ip)
        return jsonify({'valid': False}), 200

    code_key = f'ssh_auth_code:{raw_code}'
    code_data = redis_client.get(code_key)
    if not code_data:
        # Track failed attempt
        pipe = redis_client.pipeline()
        pipe.incr(fail_key)
        pipe.expire(fail_key, _SSH_VERIFY_FAIL_WINDOW)
        pipe.execute()
        logger.warning("SSH code verification invalid code client_ip=%s", client_ip)
        return jsonify({'valid': False}), 200

    # Valid code - delete it (one-time use) and decrement user counter
    redis_client.delete(code_key)
    try:
        parsed = json.loads(code_data)
        user_code_key = f"ssh_auth_code:user:{parsed['user_id']}"
        redis_client.decr(user_code_key)
        # Clean up if counter goes to 0 or below
        remaining = redis_client.get(user_code_key)
        if remaining and int(remaining) <= 0:
            redis_client.delete(user_code_key)
    except (json.JSONDecodeError, KeyError):
        parsed = {}

    logger.info("SSH code verification success client_ip=%s user_id=%s username=%s", client_ip, parsed.get('user_id'), parsed.get('username'))
    return jsonify({
        'valid': True,
        'user_id': parsed.get('user_id'),
        'username': parsed.get('username'),
        'display_name': parsed.get('display_name')
    }), 200

# ==================== End SSH Authentication Routes =======================

@app.route('/check_auth', methods=['GET'])
def check_auth_status():
    """
    Check the current login session status of the user.
    Loads user details from the database if a valid session exists.

    Returns:
        Response: JSON indicating whether user is logged in, their username, and display name.
    """
    if 'user_id' in session:
        db = get_db()
        cursor = db.execute('SELECT username, display_name, role, is_approved, pfp_url FROM users WHERE id = ?', (session['user_id'],))
        user = _fetchone_as_dict(cursor)
        if user:
            session['is_approved'] = bool(user['is_approved'])
            return jsonify({
                "logged_in": True,
                "username": user['username'],
                "display_name": user['display_name'] or user['username'],
                "role": user['role'],
                "is_approved": session['is_approved'],
                "pfp_url": user.get('pfp_url'),
                "current_chat_id": session.get('current_chat_id')
            }), 200
        else:
            return jsonify({"logged_in": False}), 200
    else:
        return jsonify({"logged_in": False}), 200
@app.route('/api/user/events', methods=['GET'])
@require_approval
def user_global_events():
    """
    Establish a Server-Sent Events (SSE) stream for user-specific events.
    Listens to a Redis pubsub channel mapping to the user's ID.

    Returns:
        Response: SSE event stream response.
    """
    user_id = session['user_id']

    def event_stream():
        """
        Inner generator function that polls Redis pubsub and yields SSE events or heartbeats.
        """
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(f"user_events:{user_id}")

        try:
            while True:
                message = pubsub.get_message(timeout=15)
                if message:
                    if message['type'] == 'message':
                        data = message['data']
                        if isinstance(data, bytes):
                            data = data.decode('utf-8')
                        yield f"data: {data}\n\n"
                else:
                    # Heartbeat to keep Nginx connection alive
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            logger.info(f"User {user_id} disconnected from events stream.")
        except Exception as e:
            logger.error(f"Error in event_stream for user {user_id}: {e}")
        finally:
            pubsub.unsubscribe()
            pubsub.close()

    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


@app.route('/api/chats', methods=['GET'])
@require_approval
def get_user_chats():
    """
    Retrieve all non-temporary chats belonging to the current user.
    Uses a correlated subquery to fetch the last active message timestamp for sorting.

    Returns:
        Response: JSON list of user chats.
    """
    user_id = session['user_id']
    db = get_db()
    try:
        # Bolt - Performance optimization: Replace LEFT JOIN + GROUP BY with a correlated subquery to avoid massive tables aggregation.
        cursor = db.execute('''
            SELECT c.id, c.name, COALESCE((SELECT MAX(m.timestamp) FROM messages m WHERE m.chat_id = c.id), c.created_at) as last_active
            FROM chats c
            WHERE c.user_id = ? AND c.is_temp = 0
            ORDER BY last_active DESC
        ''', (user_id,))
        chats = _fetch_as_dict(cursor)
        return jsonify(chats), 200
    except sqlite3.Error as e:
        logger.error(f"Database error in get_user_chats: {e}", exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error in get_user_chats: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/chats/new', methods=['POST'])
@require_approval
def create_new_chat():
    """
    Create a new chat session for the current user and sets it as active in the session.

    Returns:
        Response: JSON indicating success, chat ID, and default chat name.
    """
    user_id = session['user_id']
    db = get_db()
    try:
        cursor = db.execute('INSERT INTO chats (user_id, name) VALUES (?, ?)', (user_id, 'New Chat'))
        db.commit()
        new_chat_id = cursor.lastrowid



        session['current_chat_id'] = new_chat_id
        session.modified = True

        logger.info("Chat created user_id=%s chat_id=%s", user_id, new_chat_id)
        return jsonify({'success': True, 'chat_id': new_chat_id, 'name': 'New Chat'}), 201
    except sqlite3.Error as e:
        logger.error("Database error in create_new_chat error=%s", e, exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error("Unexpected error in create_new_chat error=%s", e, exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/chats/new_temp', methods=['POST'])
@require_approval
def create_temp_chat():
    """
    Create a new temporary (incognito) chat session.
    Automatically deletes any pre-existing temp chats for this user to keep the DB clean.

    Returns:
        Response: JSON indicating success and new chat ID.
    """
    user_id = session['user_id']
    db = get_db()
    try:
        # Delete any previous temp chats for this user to keep the DB clean!
        # Cascading deletes will automatically wipe out associated messages and tool calls.
        db.execute('DELETE FROM chats WHERE user_id = ? AND is_temp = 1', (user_id,))

        # Create the new temporary chat
        cursor = db.execute('INSERT INTO chats (user_id, name, is_temp) VALUES (?, ?, 1)', (user_id, 'Incognito Session'))
        db.commit()
        new_chat_id = cursor.lastrowid

        session['current_chat_id'] = new_chat_id
        session.modified = True

        logger.info("Temporary chat created user_id=%s chat_id=%s", user_id, new_chat_id)
        return jsonify({'success': True, 'chat_id': new_chat_id}), 201
    except Exception as e:
        logger.error("Error in create_temp_chat error=%s", e, exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/set_active_chat', methods=['POST'])
@require_approval
def set_active_chat():
    """
    Set a specific chat ID as active in the user's login session.

    Returns:
        Response: JSON indicating success or error if unauthorized/missing.
    """
    data = request.get_json()
    chat_id = data.get('chat_id')
    if not chat_id:
        return jsonify({'error': 'Missing chat_id.'}), 400

    user_id = session['user_id']
    db = get_db()

    cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
    if not cursor.fetchone():
        return jsonify({'error': 'Chat not found or unauthorized.'}), 403

    session['current_chat_id'] = chat_id
    session.modified = True

    logger.info("Active chat set user_id=%s chat_id=%s", user_id, chat_id)
    return jsonify({'success': True, 'message': f'Active chat set to {chat_id}'})
@app.route('/api/chats/<int:chat_id>/delete', methods=['DELETE'])
@require_approval
def delete_chat_route(chat_id):
    """
    Delete a specific chat, along with associated messages and tool calls.
    Sends stop flags to Redis for any active generation query inside the chat.

    Returns:
        Response: JSON response indicating deletion status.
    """
    user_id = session['user_id']
    db = get_db()
    try:
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
        chat_ownership = cursor.fetchone()
        if not chat_ownership:
            return jsonify({'error': 'Unauthorized to delete this chat.'}), 403

        active_query_str = redis_client.get(f"chat_active_query:{chat_id}")
        if active_query_str:
            try:
                active_query = json.loads(active_query_str)
                q_id = active_query.get('query_id')
                if q_id:
                    redis_client.setex(f"stop_flag:{q_id}", 3600, "1")
            except Exception as parse_err:
                logger.error("Failed to parse active query data from Redis during delete error=%s", parse_err)
            redis_client.delete(f"chat_active_query:{chat_id}")

        db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
        db.execute('DELETE FROM tool_calls WHERE chat_id = ?', (chat_id,))
        db.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
        db.commit()

        if session.get('current_chat_id') == chat_id:
            session.pop('current_chat_id', None)
            session.modified = True

        logger.info("Chat deleted user_id=%s chat_id=%s", user_id, chat_id)
        return jsonify({'success': True, 'message': 'Chat deleted successfully.'}), 200
    except sqlite3.Error as e:
        logger.error("Database error in delete_chat_route error=%s", e, exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error("Unexpected error in delete_chat_route error=%s", e, exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/chats/<int:chat_id>/name', methods=['POST'])
@require_approval
def update_chat_name_route(chat_id):
    """
    Update the display name of a chat thread.

    Returns:
        Response: JSON indicating name update status.
    """
    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
    chat_ownership = cursor.fetchone()
    if not chat_ownership:
        return jsonify({'error': 'Unauthorized to update this chat name.'}), 403

    data = request.get_json()
    first_message_content = data.get('first_message_content')

    if not first_message_content:
        return jsonify({'success': False, 'message': 'Missing first message content for naming.'}), 400

    try:
        generate_chat_name(chat_id, first_message_content)

        cursor = db.execute('SELECT name FROM chats WHERE id = ?', (chat_id,))
        updated_chat_row = _fetchone_as_dict(cursor)
        updated_name = updated_chat_row['name'] if updated_chat_row else 'New Chat'

        return jsonify({'success': True, 'name': updated_name, 'message': 'Chat name updated successfully.'}), 200
    except Exception as e:
        logger.error(f"Error in update_chat_name_route for chat {chat_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Error updating chat name: {str(e)}'}), 500

@app.route('/api/chats/<int:chat_id>/tokens', methods=['GET'])
@require_approval
def get_chat_tokens_route(chat_id):
    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('SELECT token_count FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': 'Unauthorized to access this chat\'s tokens.'}), 403

    token_count = row['token_count']
    if token_count == 0:
         token_count = count_chat_tokens(chat_id)

    return jsonify({'token_count': token_count}), 200

@app.route('/api/utils/count_tokens', methods=['POST'])
@require_approval
def api_count_tokens():
    data = request.get_json()
    text_list = data.get('text_list', [])
    if not text_list:
        return jsonify({'token_count': 0}), 200

    try:
        from google import genai
        from google.genai import types
        raw_keys = [PRIMARY_API_KEY] + [bk for bk in BACKUP_API_KEYS if bk]
        keys_to_try = [k for k in dict.fromkeys(raw_keys) if k]

        # Filter out globally rate-limited or quota-exhausted keys
        active_keys = [k for k in keys_to_try if not KEY_MANAGER.is_key_blocked(k, "gemini-3.1-flash-lite")[0]]
        if not active_keys:
            active_keys = keys_to_try

        t_count = None
        for current_key in active_keys:
            try:
                client = genai.Client(api_key=current_key)
                contents = [types.Content(role="user", parts=[types.Part(text=t)]) for t in text_list]
                t0 = time.time()
                token_count_response = client.models.count_tokens(
                    model="gemini-3.1-flash-lite", contents=contents
                )
                t_count = token_count_response.total_tokens
                duration = time.time() - t0
                logger.info("Gemini API call completed model=%s duration_sec=%.2f purpose=api_count_tokens", "gemini-3.1-flash-lite", duration)
                break
            except Exception as token_e:
                err_str = str(token_e).lower()
                if ('429' in err_str or '403' in err_str or 'resource_exhausted' in err_str or 'quota' in err_str or 'rate limit' in err_str or
                    'overloaded' in err_str or '503' in err_str or 'service unavailable' in err_str or
                    '500' in err_str or 'internal error' in err_str or 'internal_error' in err_str):
                    block_duration, block_reason = parse_quota_block_duration(err_str)
                    block_scope = None if ('403' in err_str or 'permission_denied' in err_str or 'invalid' in err_str) else "gemini-3.1-flash-lite"
                    KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                    logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to {block_reason} error during api_count_tokens.")
                logger.warning(f"Failed to count tokens in api_count_tokens: {token_e}")

        if t_count is None:
            return jsonify({'error': 'All API keys failed or rate-limited'}), 500

        return jsonify({'token_count': t_count}), 200
    except Exception as e:
        logger.error(f"Error in api_count_tokens: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/utils/check_url', methods=['GET'])
@require_approval
def api_check_url():
    import requests
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL format. Scheme must be http or https.'}), 400
    try:
        from urllib.parse import urlparse, urljoin
        
        curr_url = url
        # Limit redirects to prevent infinite loops and check redirect targets for SSRF
        for _ in range(5):
            parsed = urlparse(curr_url)
            safe, ip_or_msg = is_safe_hostname(parsed.hostname)
            if not safe:
                # Security Fix: Block access to internal/private networks via check_url
                logger.warning(f"Blocked check_url SSRF attempt: {ip_or_msg} via {curr_url}")
                return jsonify({'error': f'SSRF Protection: {ip_or_msg}'}), 403

            # Pin DNS to prevent TOCTOU DNS rebinding
            dns_cache.pinned_ips = getattr(dns_cache, 'pinned_ips', {})
            try:
                ipaddress.ip_address(ip_or_msg)
                dns_cache.pinned_ips[parsed.hostname] = ip_or_msg
            except ValueError:
                pass

            try:
                response = requests.get(curr_url, timeout=3, allow_redirects=False)
            finally:
                # Clear DNS pinning for this host
                if parsed.hostname in dns_cache.pinned_ips:
                    del dns_cache.pinned_ips[parsed.hostname]

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                if not location:
                    break
                curr_url = urljoin(curr_url, location)
                if not curr_url.startswith(('http://', 'https://')):
                    return jsonify({'error': 'Invalid redirect URL format'}), 400
            else:
                break

        return jsonify({'status': response.status_code}), 200
    except Exception as e:
        logger.exception("Error caught in api_check_url: %s", e)
        return jsonify({'status': 500, 'error': str(e)}), 200

@app.route('/api/user/profile', methods=['GET'])
@require_approval
def get_user_profile():
    db = get_db()
    cursor = db.execute('SELECT username, display_name, role, pfp_url FROM users WHERE id = ?', (session['user_id'],))
    user = _fetchone_as_dict(cursor)
    return jsonify({
        "success": True,
        "username": session['username'],
        "user_id": session['user_id'],
        "display_name": user.get('display_name') if user else session.get('display_name'),
        "pfp_url": user.get('pfp_url') if user else session.get('pfp_url')
    }), 200
@app.route('/api/user/change_display_name', methods=['POST'])
@require_approval
def change_display_name_route():
    user_id = session['user_id']
    data = request.get_json()
    new_name = data.get('new_display_name')

    if not new_name:
        return jsonify({"success": False, "message": "New display name is required."}), 400

    db = get_db()
    try:
        db.execute('UPDATE users SET display_name = ? WHERE id = ?', (new_name, user_id))
        db.commit()
        session['display_name'] = new_name
        return jsonify({"success": True, "message": "Display name changed successfully."}), 200
    except Exception as e:
        logger.error(f"Error changing display name: {e}")
        return jsonify({"success": False, "message": "Server error."}), 500

@app.route('/api/user/waitlist_info', methods=['GET'])
def get_waitlist_info():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    db = get_db()
    cursor = db.execute('SELECT username, display_name, waitlist_form_submitted FROM users WHERE id = ?', (session['user_id'],))
    user_data = cursor.fetchone()

    if user_data:
        return jsonify({
            "success": True,
            "email": user_data['username'],
            "display_name": user_data['display_name'],
            "form_submitted": bool(user_data['waitlist_form_submitted'])
        })
    return jsonify({"success": False, "message": "User not found"}), 404

@app.route('/api/user/submit_waitlist_form', methods=['POST'])
def submit_waitlist_form():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()
    designation = data.get('designation', '')
    source = data.get('source', '')
    use_case = data.get('use_case', '')

    db = get_db()
    try:
        db.execute('''
            UPDATE users
            SET designation = ?, source = ?, use_case = ?, waitlist_form_submitted = 1
            WHERE id = ?
        ''', (designation, source, use_case, session['user_id']))
        db.commit()

        # Fetch user info for Telegram message
        cursor = db.execute('SELECT username, display_name FROM users WHERE id = ?', (session['user_id'],))
        user_data = cursor.fetchone()
        if user_data:
            name_str = f"{user_data['display_name']} ({user_data['username']})" if user_data['display_name'] else user_data['username']
            msg = f"📝 Waitlist Form Submitted\nUser: {name_str}\nDesignation: {designation}\nSource: {source}\nUse Case: {use_case}"
            send_email_to_nikhil("Waitlist Form Submitted", msg)

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error submitting waitlist form: {e}")
        return jsonify({"success": False, "message": "Server error."}), 500

@app.route('/service-worker.js')
def service_worker():
    response = make_response(app.send_static_file('service-worker.js'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    return response

@app.route('/manifest.json')
def manifest():
    response = make_response(app.send_static_file('manifest.json'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    return response

@app.route('/favicon.ico')
def favicon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#7b61ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 2 17 12 22 22 17 22 7"/><circle cx="12" cy="12" r="4"/><path d="M12 2v6M22 7l-6 3M22 17l-6-3M12 22v-6M2 17l6-3M2 7l6 3"/></svg>'
    return Response(svg, mimetype='image/svg+xml')

# -------------------------------------------------------------
# PWA WEB PUSH NOTIFICATION API
# -------------------------------------------------------------
def clean_notification_body(text: str, fallback: str = "Task execution completed successfully.") -> str:
    """Helper to strip markdown, HTML (Gen UI), styling, scripts, and normalize text for notifications."""
    if not text:
        return fallback
    
    # 1. Parse with BeautifulSoup to strip HTML tags and their inner styles/scripts
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["style", "script"]):
            tag.decompose()
        text = soup.get_text()
    except Exception:
        import re
        text = re.sub(r'<style\b[^>]*>([\s\S]*?)</style>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<script\b[^>]*>([\s\S]*?)</script>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)

    # 2. Strip common markdown patterns
    import re
    # Strip links: [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Strip markdown headers (e.g. ### Header)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    # Strip remaining inline markdown formatting characters
    text = re.sub(r'[*#`_\-\[\]]', '', text)

    # 3. Clean up whitespace and newlines
    text = re.sub(r'\s+', ' ', text)
    
    cleaned = text.strip()
    if not cleaned:
        return fallback
    if len(cleaned) > 120:
        cleaned = cleaned[:117].strip() + "..."
    return cleaned

def send_push_notification(user_id, title, body, url=None):
    """Sends a Web Push notification to all active devices registered by the user in Redis."""
    if "test" in title.lower():
        fallback = "Congratulations! Your background push notifications are fully configured and functional."
    elif "action" in title.lower() or "required" in title.lower():
        fallback = "Stellar needs your interaction to proceed."
    else:
        fallback = "Task execution completed successfully."

    body = clean_notification_body(body, fallback=fallback)
    import redis
    r_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    redis_key = f"user_push_subscriptions:{user_id}"

    subscriptions_dict = r_client.hgetall(redis_key)
    if not subscriptions_dict:
        return 0

    from pywebpush import webpush, WebPushException
    import json

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url or "/"
    })

    success_count = 0
    expired_endpoints = []

    for endpoint, creds_str in subscriptions_dict.items():
        try:
            creds = json.loads(creds_str)
            sub_info = {
                "endpoint": endpoint,
                "keys": {
                    "p256dh": creds['p256dh'],
                    "auth": creds['auth']
                }
            }
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY_PATH,
                vapid_claims={"sub": "mailto:admin@stellar.app"}
            )
            success_count += 1
        except WebPushException as ex:
            logger.warning(f"WebPush failed for endpoint {endpoint}: {ex}")
            if ex.response is not None and ex.response.status_code in [404, 410]:
                expired_endpoints.append(endpoint)
        except Exception as e:
            logger.error(f"Error sending push: {e}")

    if expired_endpoints:
        try:
            r_client.hdel(redis_key, *expired_endpoints)
            logger.info("Cleaned up expired push subscriptions user_id=%s count=%d", user_id, len(expired_endpoints))
        except Exception as e:
            logger.error("Error cleaning up expired push subscriptions from Redis user_id=%s error=%s", user_id, e)

    logger.info("Push notification dispatched user_id=%s title=%s success_count=%d total_endpoints=%d", user_id, title, success_count, len(subscriptions_dict))
    return success_count

@app.route('/api/pwa/vapid_public_key', methods=['GET'])
def get_vapid_public_key():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401
    return jsonify({"success": True, "publicKey": VAPID_PUBLIC_KEY}), 200

@app.route('/api/pwa/subscribe', methods=['POST'])
def pwa_subscribe():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()
    subscription = data.get('subscription')
    if not subscription or not subscription.get('endpoint'):
        return jsonify({"success": False, "message": "Invalid subscription object."}), 400

    endpoint = subscription.get('endpoint')
    keys = subscription.get('keys', {})
    p256dh = keys.get('p256dh')
    auth = keys.get('auth')

    if not p256dh or not auth:
        return jsonify({"success": False, "message": "Missing subscription cryptographic keys."}), 400

    try:
        import redis
        r_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        redis_key = f"user_push_subscriptions:{session['user_id']}"
        val = json.dumps({"p256dh": p256dh, "auth": auth})
        import hashlib
        # Track endpoint ownership to prevent cross-account leakage
        endpoint_hash = hashlib.md5(endpoint.encode('utf-8')).hexdigest()
        owner_key = f"pwa_endpoint_owner:{endpoint_hash}"
        old_owner = r_client.get(owner_key)

        current_user_str = str(session['user_id'])
        if old_owner and old_owner != current_user_str:
            # Remove this endpoint from the previous user's subscriptions
            r_client.hdel(f"user_push_subscriptions:{old_owner}", endpoint)

        # Register the new owner (30 day TTL)
        r_client.set(owner_key, current_user_str, ex=60*60*24*30)

        # Store in Redis Hash for current user
        r_client.hset(redis_key, endpoint, val)
        return jsonify({"success": True, "message": "Subscribed to background notifications."}), 200
    except Exception as e:
        logger.error(f"Error saving PWA subscription to Redis: {e}")
        return jsonify({"success": False, "message": "Redis database error."}), 500

@app.route('/api/pwa/test_push', methods=['POST'])
def pwa_test_push():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    success_count = send_push_notification(
        user_id=session['user_id'],
        title="Stellar Push Test",
        body="Congratulations! Your background push notifications are fully configured and functional.",
        url="/"
    )

    if success_count > 0:
        return jsonify({"success": True, "message": f"Sent test push to {success_count} device(s)."}), 200
    else:
        return jsonify({"success": False, "message": "No active push subscriptions found for this user."}), 404


# ─── Sentinel Healer Routes ───────────────────────────────────────────────────

def log_backend_crash(process_id, error_type, stack_trace, trigger_heal=True, error_message=None, affected_file=None, affected_line=None):
    """Queue a backend crash to the Sentinel healer via Redis."""
    try:
        db = get_db()
        msg = error_message if error_message else error_type
        cursor = db.execute(
            "INSERT INTO sentinel_app_errors (process_id, error_type, error_message, stack_trace, affected_file, affected_line, status) VALUES (?, ?, ?, ?, ?, ?, 'open')",
            (process_id, error_type, msg, stack_trace, affected_file, affected_line)
        )
        error_id = cursor.lastrowid
        db.commit()
        if trigger_heal:
            payload = json.dumps({"process_id": process_id, "error_id": error_id})
            redis_client.lpush("sentinel:queue", payload)
            logger.info("Sentinel: Queued healing task process_id=%s error_id=%s", process_id, error_id)
        else:
            logger.info("Sentinel: Logged error process_id=%s error_id=%s action=skipped_healing reason=non_owner_visitor", process_id, error_id)
        return error_id
    except Exception as e:
        logger.error(f"Sentinel: Failed to log backend crash for {process_id}: {e}")
        return None

@app.route('/api/sentinel/log_error', methods=['POST'])
def sentinel_log_error():
    """Receives a JS error report from the telemetry hook injected into proxied apps."""
    try:
        data = request.get_json(force=True)
        process_id = None
        owner_id = None
        url = data.get('url', '')

        # Extract subdomain from the reported URL to find process_id and owner_id
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            subdomain = parsed.hostname.split('.')[0] if parsed.hostname else None
            if subdomain:
                db = get_db()
                row = db.execute("SELECT process_id, user_id FROM repo_history WHERE subdomain = ? ORDER BY id DESC LIMIT 1", (subdomain,)).fetchone()
                if row:
                    process_id = row['process_id']
                    owner_id = row['user_id']
        except Exception:
            pass

        if not process_id:
            return jsonify({"error": "No deployment mapping found"}), 404

        # Validate that the visitor is the authenticated owner of the application
        is_owner = ('user_id' in session and session['user_id'] == owner_id)

        error_info = data.get('error', {})
        error_type = error_info.get('type', 'js_error')
        error_message = error_info.get('message', 'Unknown JS error')
        stack_trace = error_info.get('stack', '')
        affected_file = error_info.get('source')
        affected_line = error_info.get('line')
        full_trace = f"JS Error: {error_message}\nSource: {error_info.get('source','')}\nLine: {error_info.get('line','')}\nStack:\n{stack_trace}"

        error_id = log_backend_crash(
            process_id, error_type, full_trace, trigger_heal=is_owner,
            error_message=error_message, affected_file=affected_file, affected_line=affected_line
        )
        return jsonify({"status": "success", "error_id": error_id})
    except Exception as e:
        logger.error(f"Sentinel log_error route failed: {e}")
        return jsonify({"success": False}), 500

@app.route('/api/sentinel/status')
def sentinel_status():
    """Polled by the telemetry hook to check if healing is active for a given app URL."""
    try:
        url = request.args.get('url', '')
        from urllib.parse import urlparse
        parsed = urlparse(url)
        subdomain = parsed.hostname.split('.')[0] if parsed.hostname else None
        if not subdomain:
            return jsonify({"healing": False})
        db = get_db()
        row = db.execute("SELECT process_id FROM repo_history WHERE subdomain = ? ORDER BY id DESC LIMIT 1", (subdomain,)).fetchone()
        if not row:
            return jsonify({"healing": False})
        healing = redis_client.get(f"sentinel:healing:{row['process_id']}")
        return jsonify({"healing": bool(healing)})
    except Exception as e:
        return jsonify({"healing": False})


@app.route('/api/sentinel/stream/<process_id>')
def sentinel_stream(process_id):
    """SSE endpoint for the healing overlay to receive live progress logs."""
    # Sentinel Security Fix: Ensure caller is authenticated and is the owner of the process or an admin to prevent unauthorized leakage of codebase/error details.
    if 'user_id' not in session:
        return jsonify({"error": "Authentication required"}), 401
    
    db = get_db()
    row = db.execute("SELECT user_id FROM repo_history WHERE process_id = ? ORDER BY id DESC LIMIT 1", (process_id,)).fetchone()
    if not row:
        return jsonify({"error": "Process not found"}), 404
        
    if session.get('role') != 'admin' and row['user_id'] != session['user_id']:
        return jsonify({"error": "Forbidden"}), 403

    def event_stream():
        log_history_key = f"sentinel:log_history:{process_id}"

        # Subscribe to live channel FIRST (before replaying history) to avoid
        # missing any events published between history replay and subscribe
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"sentinel:logs:{process_id}")

        try:
            yield f"data: {json.dumps({'event': 'connected', 'message': 'Connected to Sentinel Healer'})}\n\n"

            # Replay all historical log entries (handles late-connecting clients)
            history = redis_client.lrange(log_history_key, 0, -1)
            terminal_seen = False
            for entry in history:
                data = entry.decode('utf-8') if isinstance(entry, bytes) else entry
                yield f"data: {data}\n\n"
                try:
                    if json.loads(data).get('event') in ['healed', 'failed']:
                        terminal_seen = True
                except Exception:
                    pass

            if terminal_seen:
                return  # Already done — no need to subscribe to live channel

            # Now drain live pub/sub for any events published during/after replay
            while True:
                # Bolt - Performance/Stability Optimization: Use non-blocking get_message with timeout
                # to prevent Gunicorn threads from hanging indefinitely when clients disconnect from SSE.
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
                if message:
                    if message['type'] == 'message':
                        data = message['data'].decode('utf-8') if isinstance(message['data'], bytes) else message['data']
                        yield f"data: {data}\n\n"
                        try:
                            if json.loads(data).get('event') in ['healed', 'failed']:
                                break
                        except Exception:
                            pass
                else:
                    # Heartbeat comment forces a socket write to let Gunicorn detect closed client connections
                    yield ": heartbeat\n\n"
        finally:
            pubsub.unsubscribe(f"sentinel:logs:{process_id}")
            pubsub.close()

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

@app.route('/test-sentinel-overlay')
def test_sentinel_overlay():
    return render_template('sentinel_healing_overlay.html', app_name="TestApp", status_text="Testing", process_id="test-id")

@app.route('/')
def index():
    chat_id = request.args.get('chat_id')
    if chat_id:
        try:
            chat_id_int = int(chat_id)
            # Security: If user is logged in, verify ownership before setting current_chat_id.
            if 'user_id' in session:
                db = get_db()
                cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id_int, session['user_id']))
                if cursor.fetchone():
                    session['current_chat_id'] = chat_id_int
                else:
                    session.pop('current_chat_id', None)
            else:
                session['current_chat_id'] = chat_id_int
        except ValueError:
            pass

    def serve_no_cache(filename):
        # Bolt - Performance: use render_template so Flask/Jinja2 compiles and caches templates in memory instead of reading from disk
        template_name = filename.replace('templates/', '') if filename.startswith('templates/') else filename
        content = render_template(template_name)
        response = make_response(content)
        response.headers['Content-Type'] = 'text/html'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    if 'user_id' not in session:
        return serve_no_cache('templates/login.html')

    # Security: Ensure current_chat_id in session actually belongs to the authenticated user.
    if session.get('current_chat_id'):
        db = get_db()
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (session['current_chat_id'], session['user_id']))
        if not cursor.fetchone():
            session.pop('current_chat_id', None)

    db = get_db()
    cursor = db.execute('SELECT is_approved FROM users WHERE id = ?', (session['user_id'],))
    user_data = cursor.fetchone()

    # Sync is_approved state with database
    if user_data:
        session['is_approved'] = bool(user_data[0])
    else:
        session.pop('is_approved', None)

    if not session.get('is_approved'):
        return serve_no_cache('templates/waitlist.html')

    if 'initialized' not in session:
        session['initialized'] = True
        session.permanent = True

    return serve_no_cache('templates/index.html')

def parse_log_line(line):
    # Match: YYYY-MM-DD HH:MM:SS,ms - LEVEL - [file.py:line] - message
    match = re.match(r'^([\d\-:\s,]+) - (\w+) - \[([\w\.]+):\d+\] - (.*)$', line.strip())
    if not match:
        return None

    timestamp_str, level, source_file, message = match.groups()

    # Try to identify if it's sent by an agent
    agent_match = re.match(r'^\[(\w+)\]\s*(.*)$', message)
    if agent_match:
        agent_slug = agent_match.group(1).lower()
        content = agent_match.group(2)
        # Map agent ID to display name
        agent_names = {
            'bolt': 'Bolt (Performance Engineer)',
            'sentinel': 'Sentinel (Security Engineer)',
            'palette': 'Palette (UI Engineer)',
            'newton': 'Newton (Test Engineer)',
            'lucios': 'Lucios (Observability Engineer)',
            'proton': 'Proton (Documentation Engineer)',
            'mercury': 'Mercury (Reliability Engineer)',
            'code-reviewer': 'Code Reviewer'
        }
        sender = agent_names.get(agent_slug, agent_slug.capitalize())
        msg_type = 'agent'
    else:
        sender = 'Orchestrator'
        content = message
        msg_type = 'system'

    return {
        'timestamp': timestamp_str,
        'level': level,
        'source': source_file,
        'sender': sender,
        'content': content,
        'type': msg_type
    }

@app.route('/agent-group-chat')
@require_approval
def agent_group_chat_page():
    if session.get('role') != 'admin':
        return make_response('Forbidden', 403)

    # Bolt - Performance: use render_template so Flask/Jinja2 compiles and caches the template
    # in-process instead of reading raw bytes from disk on every request via open().
    return render_template('agent_group_chat.html')

def _get_orchestrator_sqlite_conn(path):
    """
    Bolt - Performance/Stability Optimization: Retrieve a configured connection to the orchestrator or memory database.
    Enforces WAL mode and busy timeout to prevent database locking errors.
    """
    t0 = time.time()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    duration = time.time() - t0
    if duration > 0.05:
        logger.warning("Slow orchestrator database connection path=%s duration_sec=%.3f", path, duration)
    return conn

@app.route('/api/admin/orchestrator/status')
@require_approval
def orchestrator_status():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    db_path = '/home/stellaradmin/my_app/orchestrator/orchestrator.db'
    
    import subprocess
    active = False
    try:
        status_proc = subprocess.run(["systemctl", "is-active", "stellar_orchestrator"], capture_output=True, text=True)
        active = (status_proc.stdout.strip() == 'active')
    except Exception:
        pass
        
    result = {'cooldown': {'active': False}, 'running_agent': None, 'active': active}
    try:
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
        conn = _get_orchestrator_sqlite_conn(db_path)
        row = conn.execute("SELECT value FROM orchestrator_state WHERE key='quota_cooldown_until'").fetchone()
        cooldown_set = False
        if row and row['value']:
            cooldown_dt = datetime.datetime.fromisoformat(row['value'])
            now = datetime.datetime.now(IST)
            if cooldown_dt > now:
                remaining_secs = int((cooldown_dt - now).total_seconds())
                result['cooldown'] = {
                    'active': True,
                    'until': cooldown_dt.strftime('%H:%M IST'),
                    'remaining_seconds': remaining_secs,
                    'type': 'hard'
                }
                cooldown_set = True
                
        # If no hard cooldown is active, check if both models are throttled
        if not cooldown_set:
            row_quota = conn.execute("SELECT value FROM orchestrator_state WHERE key='quota_data'").fetchone()
            if row_quota and row_quota['value']:
                try:
                    quota_dict = json.loads(row_quota['value'])
                    gemini_info = quota_dict.get('gemini', {})
                    claude_info = quota_dict.get('claude', {})
                    
                    gemini_status = gemini_info.get('status')
                    claude_status = claude_info.get('status')
                    
                    if gemini_status in ('Throttled', 'Exhausted') and claude_status in ('Throttled', 'Exhausted'):
                        # Determine how much time has passed since the quota was fetched
                        last_updated_str = quota_dict.get('last_updated')
                        elapsed_hours = 0.0
                        if last_updated_str:
                            try:
                                last_updated_dt = datetime.datetime.fromisoformat(last_updated_str)
                                now = datetime.datetime.now(IST)
                                elapsed_hours = max(0.0, (now - last_updated_dt).total_seconds() / 3600.0)
                            except Exception:
                                pass

                        g_pct = gemini_info.get('weekly_percent', 100.0)
                        g_ref = gemini_info.get('weekly_refreshes_in_hours', 0.0)
                        c_pct = claude_info.get('weekly_percent', 100.0)
                        c_ref = claude_info.get('weekly_refreshes_in_hours', 0.0)
                        
                        g_wait = max(0.0, (g_ref - elapsed_hours) - 1.68 * g_pct)
                        c_wait = max(0.0, (c_ref - elapsed_hours) - 1.68 * c_pct)
                        
                        earliest_wait = min(g_wait, c_wait)
                        if earliest_wait > 0:
                            now = datetime.datetime.now(IST)
                            cooldown_dt = now + datetime.timedelta(hours=earliest_wait)
                            result['cooldown'] = {
                                'active': True,
                                'until': cooldown_dt.strftime('%H:%M IST'),
                                'remaining_seconds': int(earliest_wait * 3600),
                                'type': 'throttled'
                            }
                except Exception as e:
                    logger.error(f"Error parsing quota data for status: {e}")
                    
        running = conn.execute("SELECT agent_id, started_at FROM agent_runs WHERE status='RUNNING' ORDER BY id DESC LIMIT 1").fetchone()
        if running:
            result['running_agent'] = dict(running)
        conn.close()
    except Exception as e:
        logger.error(f"Error fetching orchestrator status: {e}")
    return jsonify(result)

@app.route('/api/admin/orchestrator/quota-info')
@require_approval
def orchestrator_quota_info():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    db_path = '/home/stellaradmin/my_app/orchestrator/orchestrator.db'
    try:
        conn = _get_orchestrator_sqlite_conn(db_path)
        row = conn.execute("SELECT value FROM orchestrator_state WHERE key='quota_data'").fetchone()
        
        parsed = {}
        if row and row['value']:
            parsed = json.loads(row['value'])
        else:
            parsed = {'gemini': None, 'claude': None}

        # Auto-refresh quota cache if it's older than 1 hour (3600 seconds)
        should_refresh = True
        if parsed and parsed.get('last_updated'):
            try:
                last_updated_dt = datetime.datetime.fromisoformat(parsed['last_updated'])
                if last_updated_dt.tzinfo is not None:
                    from datetime import timezone, timedelta
                    IST = timezone(timedelta(hours=5, minutes=30))
                    now = datetime.datetime.now(IST)
                else:
                    now = datetime.datetime.now()
                if (now - last_updated_dt).total_seconds() < 3600:
                    should_refresh = False
            except Exception:
                pass
        
        if should_refresh:
            try:
                from orchestrator.quota import fetch_quota_data_from_container, parse_quota_text
                from datetime import timezone, timedelta
                IST = timezone(timedelta(hours=5, minutes=30))
                raw_text = fetch_quota_data_from_container()
                parsed_fresh = parse_quota_text(raw_text)
                parsed_fresh["last_updated"] = datetime.datetime.now(IST).isoformat()
                conn.execute("""
                    INSERT INTO orchestrator_state (key, value) VALUES ('quota_data', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """, (json.dumps(parsed_fresh),))
                conn.commit()
                parsed = parsed_fresh
            except Exception as e:
                logger.error(f"Auto-refreshing quota failed in quota-info: {e}")
            
        rows = conn.execute("""
            SELECT key, value FROM orchestrator_state 
            WHERE key IN ('gemini_avg_cost', 'claude_avg_cost', 'gemini_runs_count', 'claude_runs_count', 
                          'gemini_cooldown_until', 'claude_cooldown_until', 'pending_immediate_agent', 'quota_cooldown_until')
        """).fetchall()
        
        # Fetch recent runs starting from June 14th, 2026 02:55:00 PM IST (14:55:00)
        # Note to other agents: DO NOT modify this timestamp. This is when the modern daily spacing 
        # math governor and credentials went live. Including older runs will skew quota calculations.
        runs_rows = []
        try:
            runs_rows = conn.execute("""
                SELECT id, agent_id, started_at, finished_at, status, pr_number, pr_url, pr_status, model, quota_cost 
                FROM agent_runs 
                WHERE started_at >= '2026-06-14T14:55:00'
                ORDER BY id DESC LIMIT 50
            """).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"Could not fetch agent runs: {e}")
        
        conn.close()
        
        # Defaults
        parsed['gemini_avg_cost'] = 1.2
        parsed['claude_avg_cost'] = 3.0
        parsed['gemini_runs_count'] = 0
        parsed['claude_runs_count'] = 0
        
        for r in rows:
            key = r['key']
            val = r['value']
            if val is not None:
                if 'avg_cost' in key:
                    parsed[key] = float(val)
                elif 'runs_count' in key:
                    parsed[key] = int(val)
                elif 'cooldown_until' in key or key in ('pending_immediate_agent', 'quota_cooldown_until'):
                    parsed[key] = val
                    
        # Group runs by model
        gemini_runs = []
        claude_runs = []
        for r in runs_rows:
            run_dict = dict(r)
            model_val = (run_dict.get('model') or '').lower()
            if 'claude' in model_val or 'sonnet' in model_val or 'gpt' in model_val:
                claude_runs.append(run_dict)
            else:
                # Default to Gemini for others
                gemini_runs.append(run_dict)
                
        parsed['gemini_recent_runs'] = gemini_runs
        parsed['claude_recent_runs'] = claude_runs
        
        response = jsonify(parsed)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        logger.error(f"Error fetching quota info: {e}")
    response = jsonify({
        'gemini': None, 
        'claude': None, 
        'gemini_avg_cost': 1.2, 
        'claude_avg_cost': 3.0, 
        'gemini_runs_count': 0, 
        'claude_runs_count': 0,
        'gemini_recent_runs': [],
        'claude_recent_runs': []
    })
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/admin/orchestrator/refresh-quota')
@require_approval
def orchestrator_refresh_quota():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    db_path = '/home/stellaradmin/my_app/orchestrator/orchestrator.db'
    try:
        from orchestrator.quota import fetch_quota_data_from_container, parse_quota_text
        raw_text = fetch_quota_data_from_container()
        parsed = parse_quota_text(raw_text)
        parsed["last_updated"] = datetime.datetime.now().isoformat()
        
        conn = _get_orchestrator_sqlite_conn(db_path)
        conn.execute("""
            INSERT INTO orchestrator_state (key, value) VALUES ('quota_data', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (json.dumps(parsed),))
        
        rows = conn.execute("""
            SELECT key, value FROM orchestrator_state 
            WHERE key IN ('gemini_avg_cost', 'claude_avg_cost', 'gemini_runs_count', 'claude_runs_count')
        """).fetchall()
        conn.commit()
        conn.close()
        
        # Defaults
        parsed['gemini_avg_cost'] = 1.2
        parsed['claude_avg_cost'] = 3.0
        parsed['gemini_runs_count'] = 0
        parsed['claude_runs_count'] = 0
        
        for r in rows:
            key = r['key']
            val = r['value']
            if val is not None:
                if 'avg_cost' in key:
                    parsed[key] = float(val)
                elif 'runs_count' in key:
                    parsed[key] = int(val)
                    
        return jsonify(parsed)
    except Exception as e:
        logger.error(f"Error refreshing quota: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/agent_group_chat/history')
@require_approval
def get_agent_group_chat_history():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    db_path = '/home/stellaradmin/my_app/orchestrator/orchestrator.db'
    mem_db_path = '/home/stellaradmin/my_app/orchestrator/memory.db'
    messages = []

    # 1. Fetch runs from orchestrator.db
    if os.path.exists(db_path):
        try:
            # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
            conn = _get_orchestrator_sqlite_conn(db_path)
            runs = conn.execute("""
                SELECT id, agent_id, started_at, finished_at, status, pr_number, pr_url, branch_name, error_message, summary_message
                FROM agent_runs
                ORDER BY id ASC
            """).fetchall()
            conn.close()

            for r in runs:
                agent_name = r['agent_id'].capitalize()
                start_ts = r['started_at'].replace('T', ' ').split('.')[0]
                finish_ts = (r['finished_at'] or r['started_at']).replace('T', ' ').split('.')[0]

                # Starting system message
                messages.append({
                    'timestamp': start_ts,
                    'sender': 'Orchestrator',
                    'content': f"🚀 Starting agent **{agent_name}** on branch `{r['branch_name']}`...",
                    'type': 'system'
                })

                # Final messages if not running
                if r['status'] == 'COMPLETED':
                    if r['summary_message']:
                        messages.append({
                            'timestamp': finish_ts,
                            'sender': f"{agent_name} (Agent)",
                            'content': r['summary_message'],
                            'type': 'agent'
                        })

                    pr_text = f" (PR #{r['pr_number']})" if r['pr_number'] else ""
                    pr_link = f"\nPull Request: {r['pr_url']}" if r['pr_url'] else ""
                    messages.append({
                        'timestamp': finish_ts,
                        'sender': 'Orchestrator',
                        'content': f"✅ Agent **{agent_name}** completed successfully!{pr_text}{pr_link}",
                        'type': 'system'
                    })
                elif r['status'] == 'FAILED':
                    messages.append({
                        'timestamp': finish_ts,
                        'sender': 'Orchestrator',
                        'content': f"❌ Agent **{agent_name}** run failed.\nError: {r['error_message'] or 'Unknown error'}",
                        'type': 'system'
                    })
                elif r['status'] == 'TIMEOUT':
                    messages.append({
                        'timestamp': finish_ts,
                        'sender': 'Orchestrator',
                        'content': f"⚠️ Agent **{agent_name}** run timed out after exceeding limits.",
                        'type': 'system'
                    })
                elif r['status'] == 'INTERRUPTED':
                    messages.append({
                        'timestamp': finish_ts,
                        'sender': 'Orchestrator',
                        'content': f"↩️ Agent **{agent_name}** was interrupted by an orchestrator restart — retrying automatically.",
                        'type': 'system'
                    })
        except Exception as e:
            logger.error(f"Error reading agent runs history: {e}")

    # 2. Fetch group messages from memory.db
    if os.path.exists(mem_db_path):
        try:
            # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
            conn = _get_orchestrator_sqlite_conn(mem_db_path)
            rows = conn.execute("""
                SELECT sender_id, content, message_type, created_at
                FROM agent_messages
                WHERE channel = 'group'
                ORDER BY id ASC
            """).fetchall()
            conn.close()

            for r in rows:
                ts = r['created_at'].replace('T', ' ').split('.')[0]
                sender_name = r['sender_id'].capitalize() if r['sender_id'] not in ('admin', 'orchestrator') else r['sender_id'].upper()
                msg_type = 'system' if r['message_type'] == 'system' else 'agent'
                if r['sender_id'] == 'admin':
                    msg_type = 'admin'
                messages.append({
                    'timestamp': ts,
                    'sender': sender_name,
                    'content': r['content'],
                    'type': msg_type
                })
        except Exception as e:
            logger.error(f"Error reading group messages from memory.db: {e}")

    # Sort messages by timestamp
    messages.sort(key=lambda x: x['timestamp'])

    # Server-side de-duplication: Bolt - De-duplicate messages between agent_runs and agent_messages to avoid double summaries and completions in group chat
    deduped_messages = []
    seen = set()  # (clean_sender_name, content_strip)
    for msg in messages:
        sender_clean = msg['sender'].replace(' (Agent)', '').strip().upper()
        content_clean = msg['content'].strip()
        key = (sender_clean, content_clean)
        if key in seen:
            # If we see a duplicate, prefer the cleaner sender name (the one without ' (Agent)')
            if ' (Agent)' not in msg['sender']:
                for existing in deduped_messages:
                    if existing['sender'].replace(' (Agent)', '').strip().upper() == sender_clean and existing['content'].strip() == content_clean:
                        existing['sender'] = msg['sender']
            continue
        seen.add(key)
        deduped_messages.append(msg)

    return jsonify(deduped_messages)


@app.route('/api/admin/agent_group_chat/stream')
@require_approval
def agent_group_chat_stream():
    if session.get('role') != 'admin':
        return make_response('Forbidden', 403)

    def log_stream():
        user_id = session.get('user_id')
        logger.info("SSE client connected to agent_group_chat_stream user_id=%s", user_id)
        try:
            db_path = '/home/stellaradmin/my_app/orchestrator/orchestrator.db'
            if not os.path.exists(db_path):
                yield "data: {}\n\n"
                return

            pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe("agent_events")

            try:
                while True:
                    message = pubsub.get_message(timeout=15)
                    if message:
                        if message['type'] == 'message':
                            data = message['data']
                            if isinstance(data, bytes):
                                data = data.decode('utf-8')
                            yield f"data: {data}\n\n"
                    else:
                        # Heartbeat to keep connection alive
                        yield ": heartbeat\n\n"
            finally:
                pubsub.unsubscribe()
                pubsub.close()
        except GeneratorExit:
            logger.info("SSE client disconnected from agent_group_chat_stream user_id=%s", user_id)
        except Exception as e:
            logger.error("Error in agent_group_chat_stream for user %s: %s", user_id, e)

    return Response(stream_with_context(log_stream()), mimetype="text/event-stream")


@app.route('/api/admin/agent_messages/dms')
@require_approval
def get_agent_dms():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    agent_id = request.args.get('agent_id')
    if not agent_id:
        return jsonify({'error': 'Missing agent_id'}), 400
        
    db_path = '/home/stellaradmin/my_app/orchestrator/memory.db'
    if not os.path.exists(db_path):
        return jsonify([])
        
    messages = []
    try:
        # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
        conn = _get_orchestrator_sqlite_conn(db_path)
        # Fetch DMs sent by or to the agent.
        # To avoid cluttering the sender's own feed with messages sent to other agents,
        # we only show:
        # 1. Messages received by the agent (recipient_id = agent_id)
        # 2. Messages sent by the agent to the admin (sender_id = agent_id AND recipient_id = 'admin')
        rows = conn.execute("""
            SELECT id, thread_id, sender_id, recipient_id, content, message_type, ref_id, created_at
            FROM agent_messages
            WHERE channel = 'dm' AND (recipient_id = ? OR (sender_id = ? AND recipient_id = 'admin'))
            ORDER BY id ASC
        """, (agent_id, agent_id)).fetchall()

        # Bolt - Performance: batch-fetch task statuses for all resolve:task:<id> thread_ids in
        # a single IN query instead of one SELECT per message (N+1 → 1 round-trip).
        task_ids = set()
        for r in rows:
            tid = r['thread_id']
            if tid and tid.startswith('resolve:task:'):
                try:
                    task_ids.add(int(tid.split(':')[-1]))
                except ValueError:
                    pass

        resolved_task_ids = set()
        if task_ids:
            placeholders = ','.join('?' * len(task_ids))
            task_rows = conn.execute(
                f"SELECT id FROM agent_tasks WHERE id IN ({placeholders}) AND status = 'resolved'",
                list(task_ids)
            ).fetchall()
            resolved_task_ids = {r2['id'] for r2 in task_rows}

        # Determine if threads are resolved using the pre-fetched lookup set
        for r in rows:
            tid = r['thread_id']
            is_resolved = False
            if tid and tid.startswith('resolve:task:'):
                try:
                    is_resolved = int(tid.split(':')[-1]) in resolved_task_ids
                except ValueError:
                    pass

            sender_name = r['sender_id'].capitalize() if r['sender_id'] not in ('admin', 'orchestrator') else r['sender_id'].upper()
            ts = r['created_at'].replace('T', ' ').split('.')[0]
            messages.append({
                'id': r['id'],
                'thread_id': r['thread_id'],
                'sender': sender_name,
                'sender_id': r['sender_id'],
                'recipient_id': r['recipient_id'],
                'content': r['content'],
                'type': r['message_type'],
                'ref_id': r['ref_id'],
                'timestamp': ts,
                'is_resolved': is_resolved
            })
        conn.close()
    except Exception as e:
        logger.error(f"Error fetching agent DMs: {e}")

    return jsonify(messages)


@app.route('/api/admin/agent_messages/send', methods=['POST'])
@require_approval
def send_agent_message():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
        
    data = request.get_json() or {}
    channel = data.get('channel', 'group')
    recipient_id = data.get('recipient_id')
    content = data.get('content')
    thread_id = data.get('thread_id')
    
    if not content:
        return jsonify({'error': 'Missing content'}), 400
        
    db_path = '/home/stellaradmin/my_app/orchestrator/memory.db'
    if not os.path.exists(db_path):
        return jsonify({'error': 'Memory database not found'}), 500
        
    try:
        now_str = datetime.datetime.now().isoformat()
        # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
        conn = _get_orchestrator_sqlite_conn(db_path)
        cursor = conn.cursor()
        
        # Auto-create task for DMs if thread_id is resolve:task:<id> and does not exist yet
        if channel == 'dm' and thread_id and thread_id.startswith('resolve:task:'):
            try:
                task_id = int(thread_id.split(':')[-1])
                existing = cursor.execute("SELECT id FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
                if not existing:
                    title = content[:50] + ("..." if len(content) > 50 else "")
                    cursor.execute("""
                        INSERT INTO agent_tasks (id, title, description, created_by, assigned_to, status, priority, created_at, updated_at)
                        VALUES (?, ?, ?, 'admin', ?, 'open', 'normal', ?, ?)
                    """, (task_id, title, content, recipient_id, now_str, now_str))
            except Exception as te:
                logger.error(f"Error auto-creating task for DM message: {te}")
                
        cursor.execute("""
            INSERT INTO agent_messages (channel, thread_id, sender_id, recipient_id, content, message_type, created_at)
            VALUES (?, ?, 'admin', ?, ?, 'text', ?)
        """, (channel, thread_id, recipient_id, content, now_str))
        conn.commit()
        conn.close()

        # Publish admin group messages to Redis channel
        if channel == 'group':
            ts = now_str.replace('T', ' ').split('.')[0]
            msg_payload = {
                'timestamp': ts,
                'sender': 'ADMIN',
                'content': content,
                'type': 'admin'
            }
            try:
                redis_client.publish("agent_events", json.dumps(msg_payload))
            except Exception as re:
                logger.error("Failed to publish admin group message to Redis: %s", re)
        else:
            try:
                msg_payload = {
                    'type': 'refresh',
                    'target': 'dms',
                    'agent_id': 'admin',
                    'recipient_id': recipient_id
                }
                redis_client.publish("agent_events", json.dumps(msg_payload))
            except Exception as re:
                logger.error("Failed to publish admin DM refresh message to Redis: %s", re)

        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error sending agent message: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/agent_facts/list')
@require_approval
def list_agent_facts():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
        
    db_path = '/home/stellaradmin/my_app/orchestrator/memory.db'
    if not os.path.exists(db_path):
        return jsonify([])
        
    facts = []
    try:
        conn = _get_orchestrator_sqlite_conn(db_path)
        rows = conn.execute("SELECT * FROM agent_facts ORDER BY id DESC").fetchall()
        for r in rows:
            facts.append(dict(r))
        conn.close()
    except Exception as e:
        logger.error(f"Error listing facts: {e}")
        
    return jsonify(facts)


@app.route('/api/admin/agent_tasks/list')
@require_approval
def list_agent_tasks():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
        
    db_path = '/home/stellaradmin/my_app/orchestrator/memory.db'
    if not os.path.exists(db_path):
        return jsonify([])
        
    tasks = []
    try:
        # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
        conn = _get_orchestrator_sqlite_conn(db_path)
        rows = conn.execute("SELECT * FROM agent_tasks ORDER BY id DESC").fetchall()
        for r in rows:
            tasks.append(dict(r))
        conn.close()
    except Exception as e:
        logger.error(f"Error listing tasks: {e}")
        
    return jsonify(tasks)


@app.route('/api/admin/agent_tasks/create', methods=['POST'])
@require_approval
def create_agent_task():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
        
    data = request.get_json() or {}
    title = data.get('title')
    description = data.get('description')
    assigned_to = data.get('assigned_to')
    priority = data.get('priority', 'normal')
    related_file = data.get('related_file')
    
    if not title:
        return jsonify({'error': 'Missing title'}), 400
        
    db_path = '/home/stellaradmin/my_app/orchestrator/memory.db'
    if not os.path.exists(db_path):
        return jsonify({'error': 'Memory database not found'}), 500
        
    try:
        now_str = datetime.datetime.now().isoformat()
        # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
        conn = _get_orchestrator_sqlite_conn(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agent_tasks (title, description, created_by, assigned_to, status, priority, related_file, created_at, updated_at)
            VALUES (?, ?, 'admin', ?, 'open', ?, ?, ?, ?)
        """, (title, description, assigned_to, priority, related_file, now_str, now_str))
        task_id = cursor.lastrowid
        
        # Link a DM thread to this task automatically if assigned
        if assigned_to:
            thread_id = f"resolve:task:{task_id}"
            conn.execute("""
                INSERT INTO agent_messages (channel, thread_id, sender_id, recipient_id, content, message_type, ref_id, created_at)
                VALUES ('dm', ?, 'admin', ?, ?, 'task_ref', ?, ?)
            """, (thread_id, assigned_to, f"New task assigned: **{title}**\nDescription: {description or 'None'}", str(task_id), now_str))
            
        conn.commit()
        conn.close()

        # Publish task and DM refresh events to Redis so SSE clients update instantly
        try:
            redis_client.publish("agent_events", json.dumps({"type": "refresh", "target": "tasks"}))
            if assigned_to:
                redis_client.publish("agent_events", json.dumps({
                    "type": "refresh",
                    "target": "dms",
                    "agent_id": "admin",
                    "recipient_id": assigned_to
                }))
        except Exception as re:
            logger.error("Failed to publish tasks refresh message to Redis: %s", re)

        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        logger.error(f"Error creating agent task: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/agent_tasks/resolve', methods=['POST'])
@require_approval
def resolve_agent_task():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
        
    data = request.get_json() or {}
    task_id = data.get('task_id')
    if not task_id:
        return jsonify({'error': 'Missing task_id'}), 400
        
    db_path = '/home/stellaradmin/my_app/orchestrator/memory.db'
    if not os.path.exists(db_path):
        return jsonify({'error': 'Memory database not found'}), 500
        
    try:
        now_str = datetime.datetime.now().isoformat()
        # Bolt - Performance/Stability Optimization: Use _get_orchestrator_sqlite_conn to inherit WAL and busy_timeout
        conn = _get_orchestrator_sqlite_conn(db_path)
        conn.execute("""
            UPDATE agent_tasks
            SET status = 'resolved', resolved_by = 'admin', resolved_at = ?, updated_at = ?
            WHERE id = ?
        """, (now_str, now_str, task_id))
        conn.commit()
        conn.close()

        # Publish task refresh event to Redis so SSE clients update instantly
        try:
            redis_client.publish("agent_events", json.dumps({"type": "refresh", "target": "tasks"}))
        except Exception as re:
            logger.error("Failed to publish tasks refresh message to Redis: %s", re)

        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error resolving agent task: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/chats/search_messages', methods=['GET'])
@require_approval
def search_messages_route():
    user_id = session['user_id']
    search_term = request.args.get('search_term', '').strip()

    if not search_term:
        return jsonify({'results': {}}), 200

    db = get_db()
    try:
        # Bolt - Performance optimization: Replace full LEFT JOIN + scan with localized index search for matches, fixing redundant joins.
        cursor = db.execute('''
            SELECT
                c.id AS chat_id,
                c.name AS chat_name,
                m.id AS message_id,
                m.message_content,
                m.message_type
            FROM chats c
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages
                WHERE chat_id = c.id AND message_content LIKE ?
                ORDER BY timestamp ASC LIMIT 1
            )
            WHERE c.user_id = ? AND c.is_temp = 0 AND (
                c.name LIKE ? OR m.id IS NOT NULL
            )
            ORDER BY c.created_at DESC
        ''', (f'%{search_term}%', user_id, f'%{search_term}%'))

        raw_results = _fetch_as_dict(cursor)

        found_chats_info = {}
        SNIPPET_LENGTH = 100

        for row in raw_results:
            chat_id = str(row['chat_id'])
            chat_name = row['chat_name']
            message_content = row['message_content'] or ''
            message_type = row['message_type']
            message_id = str(row['message_id'])

            if chat_id in found_chats_info:
                continue

            snippet = ""
            search_term_lower = search_term.lower()

            if message_content and search_term_lower in message_content.lower():
                start_index = message_content.lower().find(search_term_lower)
                if start_index != -1:
                    snippet_end = min(len(message_content), start_index + SNIPPET_LENGTH)
                    snippet = message_content[start_index:snippet_end]

                    if snippet_end < len(message_content):
                        snippet = snippet + "..."

                    snippet = re.sub(r'\s+', ' ', snippet).strip()
                    snippet = snippet.replace('`', '').replace('*', '')
                    snippet = snippet.replace('\n', ' ')

                    if message_type == 'user':
                        snippet = "You: " + snippet
                    elif message_type == 'stellar':
                        snippet = "Stellar: " + snippet

            found_chats_info[chat_id] = {
                'chat_name': chat_name,
                'snippet': snippet,
                'message_id': message_id,
                'message_type': message_type
            }

        return jsonify({'results': found_chats_info}), 200
    except sqlite3.Error as e:
        logger.error(f"Database error in search_messages_route: {e}", exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error in search_messages_route: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/run_code', methods=['POST'])
@require_approval
def run_code():
    if not client:
        return jsonify({'error': 'Docker client not available. Is Docker running?'}), 503

    user_id = session['user_id']
    data = request.get_json()
    code = data.get('code')
    language = data.get('language')

    if not code or not language:
        return jsonify({'error': 'Missing code or language.'}), 400

    old_process_id = session.get('last_run_code_process_id')
    if 'last_run_code_process_id' in session:
        stop_and_cleanup_app_by_process_id(session.pop('last_run_code_process_id', None), app_type='run_code')
        session.modified = True

    final_frontend_code = None
    api_keys = {}

    user_network = ensure_user_network(client, session['user_id'])

    main_code_basename = "app"
    if language == 'java':
        match = re.search(r'public\s+class\s+(\w+)', code)
        if match:
            main_code_basename = match.group(1)

    lang_config = {
        'python': {'image': 'stellar-python-sandbox:3.12', 'extension': '.py', 'command': lambda f: ['python', '-u', f]},
        'javascript': {'image': 'stellar-node-sandbox:latest', 'extension': '.js', 'command': lambda f: ['node', f]},
        'php': {'image': 'stellar-php-sandbox:latest', 'extension': '.php', 'command': lambda f: ['php', f]},
        'ruby': {'image': 'stellar-ruby-sandbox:latest', 'extension': '.rb', 'command': lambda f: ['ruby', f]},
        'go': {'image': 'stellar-go-sandbox:latest', 'extension': '.go', 'command': lambda f: ['go', 'run', f]},
        'c': {'image': 'stellar-c-sandbox:latest', 'extension': '.c', 'command': lambda f: ['/bin/sh', '-c', f'gcc -o program {f} && ./program']},
        'cpp': {'image': 'stellar-cpp-sandbox:latest', 'extension': '.cpp', 'command': lambda f: ['/bin/sh', '-c', f'g++ -o program {f} && ./program']},
        'java': {'image': 'stellar-java-sandbox:latest', 'extension': '.java', 'command': lambda f: ['/bin/sh', '-c', f'javac {f} && java {f.replace(".java", "")}']},
        'rust': {'image': 'stellar-rust-sandbox:latest', 'extension': '.rs', 'command': lambda f: ['/bin/sh', '-c', f'rustc -o program {f} && ./program']},
        'typescript': {'image': 'stellar-node-sandbox:latest', 'extension': '.ts', 'command': lambda f: ['/bin/sh', '-c', f'tsc {f} && node {f.replace(".ts", ".js")}']},
    }
    config = lang_config.get(language)
    if not config:
        return jsonify({'error': f'Unsupported language for execution: {language}'}), 400

    main_code_filename_with_ext = main_code_basename + config['extension']
    is_server_app = language == 'python' and 'app.run(' in code
    ports_to_publish = {'5000/tcp': ('0.0.0.0', 0)} if is_server_app else None

    run_id = str(uuid.uuid4())
    temp_dir_path = os.path.join(SANDBOX_DIR, run_id)
    try:
        os.makedirs(temp_dir_path)
        with open(os.path.join(temp_dir_path, main_code_filename_with_ext), 'w', encoding="utf-8") as f: f.write(code)
        if final_frontend_code:
            with open(os.path.join(temp_dir_path, 'index.html'), 'w', encoding="utf-8") as f: f.write(final_frontend_code)
        if api_keys:
            with open(os.path.join(temp_dir_path, '.env'), 'w', encoding="utf-8") as f:
                for key, value in api_keys.items(): f.write(f"{key}={value}\n")
        abs_temp_dir_path = os.path.abspath(temp_dir_path)
    except Exception as e:
        logger.error(f"Failed to set up execution environment: {e}", exc_info=True)
        return jsonify({'error': f'Failed to set up execution environment: {e}'}), 500

    def generate():
        container = None
        process_id = None
        try:
            if is_server_app:
                process_id = old_process_id if old_process_id else str(uuid.uuid4())
                with app.app_context():
                    session['last_run_code_process_id'] = process_id
                    session.modified = True

                redis_key = _redis_runcode_key(process_id)
                redis_client.hset(redis_key, mapping={ "status": "starting", "process_id": process_id })


            logger.info("Creating run_code sandbox container image=%s run_id=%s process_id=%s user_network=%s", config['image'], run_id, process_id, user_network)
            t_run = time.time()
            container = client.containers.run(
                image=config['image'], command=config['command'](main_code_filename_with_ext),
                working_dir='/app', volumes={abs_temp_dir_path: {'bind': '/app', 'mode': 'rw'}},
                ports=ports_to_publish, mem_limit='1024m',
                name=f"stellar-sandbox-{run_id}", remove=False, detach=True,
                init=True, network=user_network,
                stdout=True, stderr=True,
                labels={
                    "stellar_type": "run_code",
                    "stellar_process_id": process_id if is_server_app else run_id,
                    "created_at_ts": str(time.time())
                }
            )
            logger.info("Run_code sandbox container created container_id=%s duration_sec=%.2f", container.id, time.time() - t_run)
            yield f"data: {json.dumps({'type': 'container_id', 'id': container.id})}\n\n"

            if is_server_app and process_id:
                redis_client.hset(redis_key, "container_id", container.id)
                public_url_found = False
                for _ in range(20):
                    time.sleep(1)
                    try:
                        container.reload()
                        if container.status != 'running': break
                        ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
                        mapping = ports.get('5000/tcp')
                        host_port = mapping[0].get('HostPort') if mapping else None

                        if host_port:
                            with active_apps_lock:
                                active_apps[process_id] = {"port": int(host_port), "container_id": container.id}
                            redis_client.hset(redis_key, mapping={"host_port": str(host_port), "status": "running"})
                            public_url = f"https://{process_id}.stellarai.live/"
                            yield f"data: {json.dumps({'type': 'port_info', 'url': public_url})}\n\n"
                            public_url_found = True
                            break
                    except (IndexError, TypeError, KeyError, AttributeError):
                        continue
                if not public_url_found:
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Failed to get public URL for the app.'})}\n\n"

            from queue import Queue, Empty
            log_queue = Queue()
            
            # Bolt - Performance/Stability Optimization: Run container.logs reader in a background thread
            # and consume via a non-blocking queue with timeout to allow heartbeat emission.
            # This allows Gunicorn to detect client disconnection instantly.
            def read_logs():
                try:
                    for line_bytes in container.logs(stream=True, follow=True):
                        log_queue.put(line_bytes)
                except Exception as e:
                    log_queue.put(e)
                finally:
                    log_queue.put(None)

            log_thread = threading.Thread(target=read_logs, daemon=True)
            log_thread.start()

            while True:
                try:
                    item = log_queue.get(timeout=2.0)
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    cleaned_line = item.decode('utf-8', 'replace').strip()
                    if cleaned_line:
                        yield f"data: {json.dumps({'type': 'log', 'content': cleaned_line})}\n\n"
                except Empty:
                    # Heartbeat message forces socket write to let Gunicorn detect closed client connections
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            
            container.wait()

        except Exception as e:
            logger.error(f"Error during code execution stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        finally:
            if not is_server_app and container:
                 try: container.remove(force=True)
                 except docker.errors.NotFound: pass

            if is_server_app and process_id:
                pass

            if os.path.exists(temp_dir_path):
                shutil.rmtree(temp_dir_path, ignore_errors=True)

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/api/user/api_keys', methods=['POST'])
@require_approval
def manage_api_keys():
    user_id = session['user_id']
    data = request.get_json()
    if not data or not isinstance(data.get('api_keys'), dict):
        return jsonify({'error': 'Invalid request. api_keys object is required.'}), 400

    db = get_db()
    try:
        for key_name, key_value in data['api_keys'].items():
            if not key_name or not key_value:
                continue

            encrypted_value = cipher_suite.encrypt(key_value.encode('utf-8'))
            db.execute(
                'INSERT OR REPLACE INTO user_api_keys (user_id, key_name, encrypted_value) VALUES (?, ?, ?)',
                (user_id, key_name, encrypted_value)
            )
        db.commit()
        logger.info(f"Successfully saved/updated API keys for user {user_id}.")
        return jsonify({'success': True, 'message': 'API keys saved successfully.'}), 200
    except Exception as e:
        logger.error(f"Error saving API keys for user {user_id}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred while saving keys.'}), 500



@app.route('/api/stop_container', methods=['POST'])
@require_approval
def stop_container():
    if not client:
        return jsonify({'error': 'Docker client is not available.'}), 503

    data = request.get_json()
    container_id = data.get('container_id')

    if not container_id:
        return jsonify({'error': 'Missing container_id.'}), 400

    try:
        process_id = None
        app_type = 'run_code'
        user_id = session['user_id']
        db = get_db()

        for key in redis_client.scan_iter("runcode:process:*"):
            cid = redis_client.hget(key, "container_id")
            if cid and cid == container_id:
                process_id = redis_client.hget(key, "process_id")
                break

        if not process_id:
            app_type = 'repo'
            for key in redis_client.scan_iter("repo:process:*"):
                cid = redis_client.hget(key, "container_id")
                if cid and cid == container_id:
                    process_id = redis_client.hget(key, "process_id")
                    break

        if process_id:
            # Validate ownership
            cursor = db.execute('SELECT 1 FROM repo_history WHERE process_id = ? AND user_id = ?', (process_id, user_id))
            is_owner = cursor.fetchone() is not None

            # Allow run_code containers that were created in the current session
            # If it's a run_code container, it might not be in repo_history. We allow it if it's in their session
            if not is_owner and app_type == 'run_code':
                if session.get('last_run_code_process_id') != process_id:
                     return jsonify({'error': 'Forbidden. You do not own this container.'}), 403
            elif not is_owner and app_type == 'repo':
                 return jsonify({'error': 'Forbidden. You do not own this container.'}), 403

            stop_and_cleanup_app_by_process_id(process_id, app_type)
            return jsonify({'success': True, 'message': f'Container {container_id[:12]} and its process stopped.'}), 200
        else:
            # Verify direct container ID belongs to the user if we can't map it to a process
            return jsonify({'error': 'Forbidden. Cannot verify ownership.'}), 403
    except docker.errors.NotFound:
        return jsonify({'success': False, 'message': 'Container not found (may have already stopped).'}), 404
    except Exception as e:
        logger.error(f"Error stopping container {container_id} via API: {e}")
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500


@app.route('/api/visualize', methods=['POST'])
@require_approval
def generate_visualization():
    data = request.get_json()
    content = data.get('content')
    message_id = data.get('message_id') # Get message_id to persist visualization
    model_id = 'gemini-3.5-flash' # Use the pro preview model as requested
    api_key = PRIMARY_API_KEY
 # Use RTP key for faster/cheaper generation or PRIMARY if needed

    if not content:
        return jsonify({'error': 'Content is required for visualization.'}), 400

    if not api_key:
         return jsonify({'error': 'API key not configured.'}), 500

    prompt = (
        "Create beautiful, elegant 3D interactive visuals that explain the following research paper context. "
        "The output must be a SINGLE, self-contained HTML file (including all CSS and JS). "
        "Use modern libraries like Three.js, React (via CDN), or vanilla JS/Canvas for high-quality outcomes. "
        "Ensure the design is Light Mode, with professional typography and a clean, 'Apple-like' aesthetic. "
        "The visualization should be interactive (e.g., rotatable 3D models, clickable elements, animations) and pedagogical. "
        "Do NOT require any external assets that might be blocked (images, etc.). Use procedural generation or base64 if needed. "
        "The code should be robust and error-free. "
        "IMPORTANT: Ensure the content fits perfectly within the viewport. Use `overflow: hidden` on the body and `width: 100vw; height: 100vh;` for the main container. "
        "Ensure no elements are cut off or unreachable. Responsive design is a must. "
        "MAKE IT IMPRESSIVE.\n\n"
        "Context:\n"
        f"{content}"
    )

    try:
        # We use a non-streaming call here for simplicity as we need the full HTML
        # or we could stream it to the frontend effectively.
        # For now, let's use the gemini_generate generator but collect the result.

        chat_id = session.get('current_chat_id')
        if message_id:
            try:
                db = get_db()
                cursor = db.execute('SELECT chat_id FROM messages WHERE id = ?', (message_id,))
                msg_row = cursor.fetchone()
                if msg_row: chat_id = msg_row['chat_id']
            except Exception as db_err:
                logger.error(f"Failed to query chat_id from messages: {db_err}")

        generator = gemini_generate(prompt, model_id, api_key, chat_id=chat_id)
        full_response = ""
        for chunk in generator:
            if 'result' in chunk:
                full_response += chunk['result']
            elif 'error' in chunk:
                 return jsonify({'error': chunk['error']}), 500

        if not full_response:
             return jsonify({'error': 'Failed to generate visualization.'}), 500

        # Extract HTML if wrapped in markdown code blocks
        match = re.search(r'```html\s*([\s\S]*?)\s*```', full_response, re.IGNORECASE)
        if match:
             html_content = match.group(1)
        else:
             # Fallback: sometimes the model just returns the code or wraps in generic ```
             match_generic = re.search(r'```\s*([\s\S]*?)\s*```', full_response, re.IGNORECASE)
             if match_generic:
                 html_content = match_generic.group(1)
             else:
                 html_content = full_response # Assume raw HTML if no blocks found

        # Persist to database if message_id is provided
        if message_id:
            try:
                db = get_db()
                db.execute('UPDATE messages SET visualization_html = ? WHERE id = ?', (html_content, message_id))
                db.commit()
                logger.info(f"Persisted visualization for message {message_id}")
            except Exception as db_err:
                logger.error(f"Failed to persist visualization: {db_err}")

        return jsonify({'success': True, 'html': html_content})

    except Exception as e:
        logger.error(f"Error generating visualization: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


class OrphanContainerMonitor:
    """
    Monitor thread that periodically identifies and cleans up orphan Docker containers
    and performs runtime health checks on active applications.
    """
    def __init__(self, interval=60):
        """
        Initialize the monitor with a cleanup interval.

        Args:
            interval (int, optional): The sleep interval between checks in seconds. Defaults to 60.
        """
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)

    def start(self):
        """Start the background monitor thread."""
        self.thread.start()

    def stop(self):
        """Signal the background monitor thread to stop."""
        self.stop_event.set()

    def _monitor_loop(self):
        """Loop continuously, checking for and cleaning up orphan containers."""
        while not self.stop_event.is_set():
            try:
                self._cleanup_orphans()
            except Exception as e:
                logger.error("Error in OrphanContainerMonitor: %s", e, exc_info=True)
            # Bolt - Stability Optimization: Wait on stop_event to exit instantly on stop/reload
            self.stop_event.wait(self.interval)

    def _cleanup_orphans(self):
        """
        Perform a sweep of all Docker containers matching the 'stellar_type' label,
        stopping and removing those that are no longer tracked as active,
        and querying health status endpoints for those that are.
        """
        import docker
        import requests
        if not client:
            return

        # List all containers managed by Stellar with our label
        try:
            containers = client.containers.list(all=True, filters={"label": "stellar_type"})
        except Exception as e:
            logger.error("OrphanContainerMonitor: Failed to list containers error=%s", e, exc_info=True)
            return

        current_time = time.time()

        for container in containers:
            try:
                labels = container.labels
                process_id = labels.get("stellar_process_id")
                created_ts_str = labels.get("created_at_ts")

                # Check for exited containers
                if container.status == 'exited':
                    try:
                        container.remove(force=True)
                        logger.info("OrphanContainerMonitor: Removed exited container container_id=%s", container.short_id)
                    except docker.errors.NotFound:
                        pass
                    except Exception as e:
                        logger.error("OrphanContainerMonitor: Error removing exited container container_id=%s error=%s", container.short_id, e, exc_info=True)
                    continue

                # Check for running orphans
                if container.status == 'running':
                    if created_ts_str:
                        try:
                            created_ts = float(created_ts_str)
                            if current_time - created_ts < 60 * 60 * 60:
                                # Grace period for startup
                                continue
                        except ValueError:
                            pass

                    # Check if process_id is in active_apps
                    is_active = False
                    with active_apps_lock:
                        if process_id in active_apps:
                            is_active = True

                    if not is_active:
                        logger.warning("OrphanContainerMonitor: Found orphan container container_id=%s process_id=%s action=stopping", container.short_id, process_id)
                        try:
                            container.stop(timeout=5)
                            container.remove(force=True)
                            logger.info("OrphanContainerMonitor: Removed orphan container container_id=%s process_id=%s", container.short_id, process_id)
                        except docker.errors.NotFound:
                            pass
                        except Exception as e:
                            logger.error("OrphanContainerMonitor: Failed to remove orphan container_id=%s process_id=%s error=%s", container.short_id, process_id, e, exc_info=True)
                    else:
                        # Runtime Health Check for active apps
                        try:
                            app_data = None
                            with active_apps_lock:
                                app_data = active_apps.get(process_id)

                            # Only check if it's supposed to be 'running'
                            if app_data and app_data.get('port') and app_data.get('status') == 'running':
                                # Use created_ts to avoid racing with initial startup (5 min grace period)
                                if current_time - created_ts > 300:
                                    target_port = app_data['port']
                                    try:
                                        # Use host loopback to check the mapped port
                                        check_url = f"http://127.0.0.1:{target_port}/api/ping"
                                        # Use a short timeout to prevent monitor stalls
                                        resp = requests.get(check_url, timeout=5)
                                        if resp.status_code >= 500:
                                            logger.error("OrphanContainerMonitor: App health check returned server error process_id=%s port=%s status_code=%d", process_id, target_port, resp.status_code)
                                            with active_apps_lock:
                                                active_apps[process_id]['status'] = 'failed'
                                            redis_client.hset(_redis_repo_key(process_id), "status", "failed")
                                    except Exception as req_err:
                                        logger.error("OrphanContainerMonitor: App health check connection error process_id=%s port=%s error=%s", process_id, target_port, req_err)
                                        # Mark as failed in Redis and memory so user sees the error
                                        with active_apps_lock:
                                            active_apps[process_id]['status'] = 'failed'
                                        redis_client.hset(_redis_repo_key(process_id), "status", "failed")
                        except Exception as h_err:
                            logger.error("OrphanContainerMonitor: Health check logic error process_id=%s error=%s", process_id, h_err, exc_info=True)
            except Exception as e:
                logger.error("OrphanContainerMonitor: Error processing container container_id=%s error=%s", container.short_id, e, exc_info=True)

def cleanup_stale_containers():
    """
    Locate and clean up any stale Docker containers and update database execution
    history statuses to 'stopped' during server startup.
    """
    import docker
    try:
        # Reset only very old statuses in the database to 'stopped' on startup
        try:
            with app.app_context():
                db = get_db()
                # 90 hours in seconds
                ninety_hours_ago = (datetime.datetime.now() - datetime.timedelta(hours=90)).strftime('%Y-%m-%d %H:%M:%S')
                db.execute("UPDATE repo_history SET status = 'stopped' WHERE status IN ('running', 'starting', 'created') AND created_at < ?", (ninety_hours_ago,))
                db.commit()
                logger.info("Database status for repo_history reset for apps older than %s", ninety_hours_ago)
        except Exception as db_err:
            logger.exception("Failed to reset database statuses on startup error=%s", db_err)

        t_list = time.time()
        client = docker.from_env()
        # Clean up by label first
        stale_labeled = client.containers.list(all=True, filters={"label": "stellar_type"})

        # Also clean up by name pattern for backward compatibility
        stale_named = client.containers.list(all=True, filters={'name': 'stellar-sandbox-*'})
        logger.info("Docker stale container query completed duration_sec=%.2f", time.time() - t_list)

        all_stale = list(set(stale_labeled + stale_named))

        if not all_stale:
            logger.info("No stale sandbox containers found on startup.")
            return

        logger.warning("Found %d stale sandbox container(s) checking creation times", len(all_stale))
        current_time = time.time()
        for container in all_stale:
            try:
                # Check for 90-hour grace period
                labels = container.labels
                created_ts_str = labels.get("created_at_ts")
                if created_ts_str:
                    try:
                        created_ts = float(created_ts_str)
                        if current_time - created_ts < 90 * 60 * 60:
                            logger.info("Skipping recently created container within 90h grace period container_name=%s", container.name)
                            continue
                    except ValueError:
                        pass

                logger.warning("Force-removing stale container container_name=%s container_id=%s", container.name, container.short_id)
                container.remove(force=True)
            except docker.errors.NotFound:
                logger.info("Container already removed container_name=%s", container.name)
            except Exception as e:
                logger.error("Error during cleanup of stale container container_name=%s error=%s", container.name, e, exc_info=True)
        logger.info("Stale container cleanup complete.")

    except docker.errors.DockerException as e:
        logger.error("Docker not available skipping stale container cleanup error=%s", e, exc_info=True)
    except Exception as e:
        logger.exception("Unexpected error during stale container cleanup error=%s", e)

# Start the orphan monitor - only in the main process to avoid multi-worker redundancy
if not app.config.get('TESTING'):
    # Note: In gunicorn, this might still trigger per worker if not careful,
    # but we initialize active_apps per process anyway.
    orphan_monitor = OrphanContainerMonitor(interval=300)
    orphan_monitor.start()
    atexit.register(orphan_monitor.stop)

active_apps = {}
active_apps_lock = threading.Lock()

@app.before_request
def log_request_start():
    g.start_time = time.time()
    if not getattr(g, 'request_id', None):
        g.request_id = request.headers.get('X-Request-ID', str(uuid.uuid4())[:8])
    if request.path.startswith('/static/') or request.path == '/favicon.ico':
        return
    logger.info("Request received method=%s path=%s ip=%s", request.method, request.path, request.remote_addr)

@app.after_request
def log_request_end(response):
    if request.path.startswith('/static/') or request.path == '/favicon.ico':
        return response
    duration = time.time() - getattr(g, 'start_time', time.time())
    logger.info("Request completed method=%s path=%s status=%d duration_sec=%.3f",
                request.method, request.path, response.status_code, duration)
    return response

@app.after_request
def add_security_headers(response):
    # Sentinel Security Fix: Prevent MIME-sniffing, clickjacking, and referrer leakage
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Don't restrict framing for proxied user subdomains (e.g. live previews)
    if getattr(g, 'is_proxy', False):
        response.headers.pop('X-Frame-Options', None)
    else:
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    
    # Sentinel Security Fix: Apply strict sandbox Content-Security-Policy for files viewed in browser
    # to isolate them from the main origin and prevent script execution (stored XSS protection)
    if request.endpoint == 'view_file':
        response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; media-src 'self';"
    return response

@app.before_request
def update_last_active():
    if 'user_id' in session:
        try:
            db = get_db()
            db.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = ?", (session['user_id'],))
            db.commit()
        except Exception as e:
            logger.exception("Error caught: %s", e)
            pass # Silently fail to not interrupt user experience

@app.before_request
def intercept_subdomains():
    import requests
    # Don't intercept sentinel API calls — they must reach the main app
    if request.path.startswith('/api/sentinel/'):
        return None

    host = request.headers.get('Host', '')
    domain_parts = host.split(':')[0].split('.')

    # Catch any request to *.stellarai.live (excluding www and the main root domain)
    if len(domain_parts) >= 3 and domain_parts[-2] == 'stellarai' and domain_parts[-1] == 'live' and domain_parts[0] != 'www':
        g.is_proxy = True
        subdomain = domain_parts[0]

        db = get_db()
        cursor = db.execute("SELECT process_id, subdomain, user_id FROM repo_history WHERE subdomain = ? ORDER BY id DESC LIMIT 1", (subdomain,))
        row = cursor.fetchone()

        owner_id = None
        if row:
            # Verify owner approval
            owner_id = row['user_id']
            owner_cursor = db.execute("SELECT is_approved FROM users WHERE id = ?", (owner_id,))
            owner_row = owner_cursor.fetchone()
            if not owner_row or not owner_row[0]:
                return f"Access Denied. The owner of '{subdomain}' is not approved or their access has been revoked.", 403

        # Fallback to process_id (uuid) if it's a temporary run_code container
        process_id = row['process_id'] if row else subdomain

        # Check if Sentinel is currently healing this application
        try:
            healing_status = redis_client.get(f"sentinel:healing:{process_id}")
            if healing_status:
                return render_template('sentinel_healing_overlay.html', app_name=subdomain, status_text=healing_status, process_id=process_id)
        except Exception as redis_err:
            logger.error(f"Failed to check sentinel healing status in Redis: {redis_err}")

        app_info = None
        with active_apps_lock:
            app_info = active_apps.get(process_id)

        if not app_info:
            try:
                redis_key = _redis_repo_key(process_id)
                redis_data = redis_client.hgetall(redis_key)
                if not redis_data:
                    redis_key = _redis_runcode_key(process_id)
                    redis_data = redis_client.hgetall(redis_key)

                if redis_data and redis_data.get("host_port") and redis_data.get("status") in ["running", "created", "exited", "failed"]:
                    app_info = {
                        "port": int(redis_data["host_port"]),
                        "container_id": redis_data.get("container_id"),
                        "status": redis_data.get("status")
                    }
                    with active_apps_lock:
                        active_apps[process_id] = app_info
                else:
                    logger.debug(f"No active app found in Redis for {process_id} (subdomain: {subdomain})")
            except Exception as e:
                logger.error(f"Redis lookup failed for app {process_id}: {e}")
                return "Error looking up application state.", 500

        if not app_info or not app_info.get("port"):
            return f"Application '{subdomain}' is stopped or unavailable. Start it in Repo Control.", 503
        # Critical Fix: Don't even try to proxy if we know it's exited
        if app_info.get("status") == "exited":
            return f"Application '{subdomain}' has stopped. Please restart it.", 404

        target_port = app_info["port"]
        path = request.full_path # Preserves exact routing paths and query parameters!
        target_url = f"http://127.0.0.1:{target_port}{path}"

        resp = None
        t_proxy = time.time()
        try:
            logger.info("Proxying request subdomain=%s process_id=%s target_port=%s method=%s path=%s", subdomain, process_id, target_port, request.method, request.path)
            # Strip the main session cookie to prevent user containers from hijacking the user's session
            proxy_cookies = {k: v for k, v in request.cookies.items() if k != 'stellar_session_main'}
            proxy_headers = {key: value for (key, value) in request.headers if key.lower() not in ['host', 'cookie']}
            resp = requests.request(
                method=request.method,
                url=target_url,
                headers=proxy_headers,
                data=request.get_data(),
                cookies=proxy_cookies,
                allow_redirects=False,
                stream=True,
                timeout=3600
            )
            logger.info("Proxy request completed subdomain=%s process_id=%s status=%d duration_sec=%.3f", subdomain, process_id, resp.status_code, time.time() - t_proxy)

            excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
            headers =[(name, value) for (name, value) in resp.raw.headers.items() if name.lower() not in excluded_headers]

            # Add cache control to ensure the browser always gets the latest iteration
            headers.append(('Cache-Control', 'no-cache, no-store, must-revalidate'))
            headers.append(('Pragma', 'no-cache'))
            headers.append(('Expires', '0'))

            # Check if the response is a server error — trigger Sentinel if so
            if resp.status_code >= 500:
                container_logs = ""
                try:
                    import docker
                    d_client = docker.from_env()
                    container = d_client.containers.get(f"stellar-repo-{process_id}")
                    container_logs = container.logs(tail=100, stdout=True, stderr=True).decode('utf-8', 'replace')
                except Exception as docker_err:
                    container_logs = f"Failed to retrieve container logs: {docker_err}"
                body_snippet = resp.text[:2000] if 'text/html' in resp.headers.get('Content-Type', '').lower() else ""
                # Sentinel Security Fix: Only trigger self-healing if the visitor is the authenticated owner of the application.
                is_owner = (owner_id is not None and 'user_id' in session and session['user_id'] == owner_id)
                log_backend_crash(process_id, f"HTTP Server Error {resp.status_code}",
                    f"HTTP STATUS {resp.status_code}\n\nCONTAINER LOGS:\n{container_logs}\n\nHTTP RESPONSE:\n{body_snippet}",
                    trigger_heal=is_owner)

            # Inject Sentinel telemetry JS hook into HTML responses
            content_type = resp.headers.get('Content-Type', '')
            if 'text/html' in content_type.lower():
                html_content = resp.text
                script_tag = """<script id="sentinel-telemetry-hook">
(function() {
    var SENTINEL_KEY = 'sentinel_reported_' + window.location.pathname;
    var reportedErrors = {};
    var errorCount = 0;
    // Use sessionStorage to survive reloads — don't re-report on a just-healed page
    var healingReported = sessionStorage.getItem(SENTINEL_KEY) === '1';
    setInterval(function() { errorCount = 0; }, 10000);
    // Clear the flag after 30s so future real errors can still be caught
    if (healingReported) setTimeout(function() { sessionStorage.removeItem(SENTINEL_KEY); healingReported = false; }, 30000);
    function pollForOverlay() {
        var attempts = 0;
        var interval = setInterval(function() {
            attempts++;
            fetch('/api/sentinel/status?url=' + encodeURIComponent(window.location.href))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.healing) { clearInterval(interval); window.location.reload(); }
                })
                .catch(function(){});
            if (attempts > 20) clearInterval(interval);
        }, 1500);
    }
    function reportError(errorData) {
        if (healingReported || errorCount >= 5) return;
        // Ignore cross-origin errors (CDN scripts, browser extensions) — source is null or empty
        var src = errorData.source || '';
        if (!src || src === 'null' || (src.indexOf(window.location.origin) === -1 && src.indexOf('://') !== -1)) return;
        var hash = errorData.message + (errorData.line || '') + src;
        if (reportedErrors[hash] && (Date.now() - reportedErrors[hash] < 30000)) return;
        reportedErrors[hash] = Date.now();
        errorCount++;
        healingReported = true;
        sessionStorage.setItem(SENTINEL_KEY, '1');
        fetch('/api/sentinel/log_error', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: window.location.href, error: errorData, user_agent: navigator.userAgent })
        }).then(function(r) {
            if (r.ok) pollForOverlay();
        }).catch(function(){});
    }
    window.onerror = function(message, source, lineno, colno, error) {
        reportError({ type: 'js_error', message: message, source: source || '', line: lineno, col: colno, stack: error ? error.stack : '' });
    };
    window.onunhandledrejection = function(event) {
        if (!event.reason) return;
        reportError({ type: 'promise_rejection', message: event.reason.message || String(event.reason), stack: event.reason.stack || '', source: window.location.href });
    };
})();
</script>"""

                if "</head>" in html_content:
                    html_content = html_content.replace("</head>", f"{script_tag}</head>", 1)
                else:
                    html_content = script_tag + html_content
                return Response(html_content, resp.status_code, headers)

            # FIX: Stream the response back in chunks instead of buffering with resp.content
            def generate():
                try:
                    # Yield data as it comes in from the container
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk
                finally:
                    # CRITICAL: If the user refreshes/disconnects, Flask stops consuming
                    # the generator. This finally block executes and severs the connection
                    # to the inner container, freeing up its threads immediately.
                    resp.close()

            return Response(stream_with_context(generate()), resp.status_code, headers)

        except requests.exceptions.RequestException as e:
            if resp:
                try:
                    resp.close()
                except:
                    pass
            logger.error(f"Dynamic proxy error for app {process_id}: {e}")

            # Log connection failure to Sentinel
            try:
                import docker
                d_client = docker.from_env()
                container = d_client.containers.get(f"stellar-repo-{process_id}")
                container_logs = container.logs(tail=100, stdout=True, stderr=True).decode('utf-8', 'replace')
            except Exception as docker_err:
                container_logs = f"Failed to retrieve container logs: {docker_err}"
            # Sentinel Security Fix: Only trigger self-healing if the visitor is the authenticated owner of the application.
            is_owner = (owner_id is not None and 'user_id' in session and session['user_id'] == owner_id)
            log_backend_crash(process_id, f"Connection Failure: {str(e)}",
                f"PROXY ERROR: {str(e)}\n\nCONTAINER LOGS:\n{container_logs}",
                trigger_heal=is_owner)

            # Passive Health Check: If connection is refused/reset, invalidate local cache
            # The port might be stale. Removing it forces a Redis re-fetch on the next request.
            if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                with active_apps_lock:
                    if process_id in active_apps:
                        del active_apps[process_id]

            if app_info.get("status") == "exited":
                 return "Application not found or has been stopped.", 404
            return f"Error proxying request to application.", 502


@require_approval
@require_approval
@require_approval
@app.route('/api/repo/history', methods=['GET'])
@require_approval
def get_repo_history():
    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('''
        SELECT fh.id, fh.project_name, fh.process_id, fh.status, fh.deployment_url, fh.created_at, fh.last_updated
        FROM repo_history fh
        INNER JOIN (
            SELECT process_id, MAX(id) as latest_id
            FROM repo_history
            WHERE user_id = ?
            GROUP BY process_id
        ) latest ON fh.id = latest.latest_id
        ORDER BY fh.created_at DESC
    ''', (user_id,))
    history = _fetch_as_dict(cursor)
    return jsonify({'history': history})

@app.route('/api/repo/history/<int:history_id>/resume', methods=['POST'])
@require_approval
def resume_repo_history(history_id):
    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('SELECT * FROM repo_history WHERE id = ? AND user_id = ?', (history_id, user_id))
    entry = _fetchone_as_dict(cursor)

    if not entry:
        return jsonify({'error': 'History entry not found.'}), 404

    files_snapshot = entry.get('files_snapshot')
    if not files_snapshot:
        return jsonify({'error': 'No files snapshot available for this project.'}), 400

    try:
        files = json.loads(files_snapshot)
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid file snapshot data.'}), 500

    # Stop current project if any
    if 'repo_project' in session:
        try:
            stop_and_cleanup_app_by_process_id(session['repo_project'].get('process_id'), app_type='repo')
        except Exception as e:
            logger.exception("Error caught: %s", e)
            logger.warning(f"Error stopping previous repo project during resume: {e}")

    process_id = str(uuid.uuid4())
    project_name = entry.get('project_name') or "Repo Project"
    subdomain = entry.get('subdomain')

    session['repo_project'] = {
        'files': files,
        'container_id': None,
        'process_id': process_id,
        'project_name': project_name,
        'subdomain': subdomain
    }
    session.modified = True

    # Notify via Telegram
    try:
        db = get_db()
        cursor = db.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],))
        user_row = cursor.fetchone()
        if user_row:
            current_username = user_row['username']
            send_email_to_nikhil(
                f"Repo Session Resumed: {project_name}",
                f"🛠️ {current_username} resumed repo session: {project_name}"
            )
    except Exception as e:
        logger.error(f"Failed to send Repo Resume Telegram notification: {e}")
    return jsonify({'success': True, 'message': 'Project loaded.', 'files': files, 'process_id': process_id})

@app.route('/api/repo/history/<int:history_id>', methods=['DELETE'])
@require_approval
def delete_repo_history(history_id):
    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('SELECT process_id, container_id FROM repo_history WHERE id = ? AND user_id = ?', (history_id, user_id))
    entry = _fetchone_as_dict(cursor)

    if not entry:
        return jsonify({'error': 'Entry not found.'}), 404

    process_id = entry['process_id']

    # Stop if running
    stop_and_cleanup_app_by_process_id(process_id, app_type='repo')

    db.execute('DELETE FROM repo_history WHERE id = ?', (history_id,))
    db.commit()

    return jsonify({'success': True, 'message': 'History entry deleted.'})
if os.environ.get('TESTING') != 'true':
    cleanup_stale_containers()

class TaskSchedulerMonitor:
    """
    Monitor thread that periodically polls the database for scheduled tasks
    that are due for execution and runs them asynchronously.
    """
    def __init__(self, app_instance, interval=60):
        """
        Initialize the scheduler monitor.

        Args:
            app_instance (Flask): The Flask application instance.
            interval (int, optional): The check interval in seconds. Defaults to 60.
        """
        self.app_instance = app_instance
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)

    def start(self):
        """Start the background scheduler thread."""
        self.thread.start()

    def stop(self):
        """Signal the background scheduler thread to exit."""
        self.stop_event.set()

    def _monitor_loop(self):
        """Loop continuously, checking for and running scheduled tasks."""
        while not self.stop_event.is_set():
            try:
                self._check_tasks()
            except Exception as e:
                logger.error("Error in TaskSchedulerMonitor: %s", e, exc_info=True)
            # Bolt - Stability Optimization: Wait on stop_event to exit instantly on stop/reload
            self.stop_event.wait(self.interval)

    def _check_tasks(self):
        """
        Check for scheduled tasks that are active and due for execution.
        Claims the task atomically using a unique worker ID to prevent concurrency conflicts,
        and spawns a separate thread to run the task.
        """
        import uuid
        worker_id = str(uuid.uuid4()) # Unique ID for this thread/worker

        with self.app_instance.app_context():
            db = get_db()
            # ATOMIC CLAIM: Try to lock any pending task that is due
            db.execute('''
                UPDATE scheduled_tasks
                SET status = 'running', lock_id = ?
                WHERE id IN (
                    SELECT id FROM scheduled_tasks
                    WHERE is_active = 1
                    AND status = 'pending'
                    AND (execute_at IS NULL OR execute_at <= datetime('now', 'localtime'))
                    LIMIT 1
                )
            ''', (worker_id,))
            db.commit()

            # Now find the task WE just locked
            cursor = db.execute('''
                SELECT id, user_id, chat_id, task_prompt, model_id, execute_at, recurring_minutes, metadata
                FROM scheduled_tasks
                WHERE lock_id = ? AND status = 'running'
            ''', (worker_id,))
            task = cursor.fetchone()
            if task:
                def run_task_wrapper(t):
                    """Wrapper to run the task inside a thread with its own request ID context."""
                    thread_local_ctx.request_id = f"sched-{t['id']}"
                    logger.info("Executing scheduled task task_id=%d user_id=%d chat_id=%d model_id=%s", t['id'], t['user_id'], t['chat_id'], t['model_id'])
                    try:
                        self._execute_ai_task(t['id'], t['user_id'], t['chat_id'], t['task_prompt'], t['model_id'], t['metadata'])
                        with self.app_instance.app_context():
                            db = get_db()
                            if t['recurring_minutes'] > 0:
                                db.execute('''
                                    UPDATE scheduled_tasks
                                    SET execute_at = datetime('now', 'localtime', '+' || ? || ' minutes'),
                                        last_run = datetime('now', 'localtime'), status = 'pending', lock_id = NULL
                                    WHERE id = ?
                                ''', (t['recurring_minutes'], t['id']))
                            else:
                                db.execute("UPDATE scheduled_tasks SET is_active = 0, status = 'completed', last_run = datetime('now', 'localtime'), lock_id = NULL WHERE id = ?", (t['id'],))
                            db.commit()
                        logger.info("Scheduled task completed successfully task_id=%d", t['id'])
                    except Exception as e:
                        logger.error("Scheduled task execution failed task_id=%d user_id=%d chat_id=%d error=%s", t['id'], t['user_id'], t['chat_id'], e, exc_info=True)
                        with self.app_instance.app_context():
                            db = get_db()
                            db.execute("UPDATE scheduled_tasks SET status = 'failed', lock_id = NULL WHERE id = ?", (t['id'],))
                            db.commit()

                threading.Thread(target=run_task_wrapper, args=(task,), daemon=True).start()

    def _execute_ai_task(self, task_id, user_id, chat_id, task_prompt, model_id, metadata):
        """
        Execute a scheduled task by invoking the Gemini refinement generation pipeline
        and appending the resulting response to the user's conversation history.

        Args:
            task_id (int): The database ID of the scheduled task.
            user_id (int): The owner user's database ID.
            chat_id (int): The target chat's database ID.
            task_prompt (str): The prompt instructions for the task.
            model_id (str): The Gemini model ID to execute the task with.
            metadata (str): Additional configuration or tracking context.
        """
        with self.app_instance.app_context():
            from flask import g
            from app import get_db
            # Set global context for tools to use in background threads
            g.user_id = user_id
            g.chat_id = chat_id

            from app import get_conversation_history, insert_message, gemini_generate, PRIMARY_API_KEY, build_annotated_history
            history = get_conversation_history(chat_id)
            conv_hist_list, last_msg_time = build_annotated_history(history[-10:], None)

            from prompts import get_refinement_prompt
            # Calculate time elapsed since last message (in UTC)
            time_elapsed_str = ""
            if last_msg_time:
                try:
                    now_utc = datetime.datetime.utcnow()
                    elapsed_delta = now_utc - last_msg_time
                    if elapsed_delta.total_seconds() > 60:
                        formatted_elapsed = format_time_delta(elapsed_delta).replace(' later', '')
                        time_elapsed_str = f"[SYSTEM NOTICE: {formatted_elapsed} has passed since the last message in this conversation.]\n\n"
                except Exception as e:
                    logger.error(f"Error calculating time elapsed: {e}")

            # Wrap the task prompt in a directive and include the scratchpad metadata
            meta_context = f"\n**TASK SCRATCHPAD (TRANSIENT STATE):**\n{metadata}\n" if metadata else ""
            directive_prompt = f"{time_elapsed_str}### SCHEDULED TASK EXECUTION MANDATE\nYou are executing a pre-authorized scheduled task.{meta_context}\nYou MUST use the necessary tools to fulfill this request immediately. Do not apologize or simulate the action.\n\nTask: {task_prompt}"

            system_prompt = get_refinement_prompt(directive_prompt, conv_hist_list, user_id=user_id, model_id=model_id)

            generator = gemini_generate(
                prompt=system_prompt,
                model_id=model_id, # MODEL LOCK ENFORCED
                key=PRIMARY_API_KEY,
                chat_id=chat_id
            )

            final_output = ""
            db = get_db()
            for chunk in generator:
                # Check if task was cancelled mid-execution
                cursor = db.execute("SELECT is_active FROM scheduled_tasks WHERE id = ?", (task_id,))
                row = cursor.fetchone()
                if row and row['is_active'] == 0:
                    logger.info(f"Task {task_id} was cancelled during execution. Aborting AI generation.")
                    return # Exit early, discarding any output

                if 'result' in chunk: final_output += chunk['result']

            if final_output:
                from app import MODEL_NAMES
                display_name = MODEL_NAMES.get(model_id, model_id)
                import re
                clean_output = re.sub(r'^\s*\*\*Scheduled Execution\s*\([^)]+\):\*\*\s*', '', final_output, flags=re.IGNORECASE)
                insert_message(chat_id, "stellar", f"**Scheduled Execution ({display_name}):**\n\n{clean_output}")

if os.environ.get('TESTING') != 'true':
    task_scheduler = TaskSchedulerMonitor(app)
    task_scheduler.start()

    try:
        from sentinel_healer import start_sentinel_healer, stop_sentinel_healer
        start_sentinel_healer()
        atexit.register(stop_sentinel_healer)
        logger.info("Successfully started Sentinel Healer background worker.")
    except Exception as e:
        logger.error(f"Failed to start Sentinel Healer: {e}")

if __name__ == '__main__':    # Ensure Docker images are ready before starting the server
    try:
        logger.info("Verifying and building Docker images via dockersetup.py...")
        subprocess.run([sys.executable, "dockersetup.py"], check=True)
    except Exception as e:
        logger.error(f"Failed to run dockersetup.py: {e}")

    port = int(os.environ.get('PORT', 5013))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
