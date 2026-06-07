import smtplib
from email.message import EmailMessage
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
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
from google import genai
import pypandoc
from dotenv import load_dotenv
import webscrapper
from tavily import TavilyClient
import datetime
from google.genai import types
import requests
import docker
import tempfile
import atexit
import shutil
from itertools import cycle
from cryptography.fernet import Fernet
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import redis
import secrets
from prompts import (
    get_refinement_prompt
)

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

client = None
try:
    client = docker.from_env()
    client.ping()
    logging.info("Successfully connected to Docker daemon on startup.")
    try:
        client.networks.get("stellar_isolated")
        logging.info("Found existing 'stellar_isolated' network.")
    except docker.errors.NotFound:
        logging.info("Creating 'stellar_isolated' network with ICC disabled.")
        client.networks.create("stellar_isolated", driver="bridge", options={"com.docker.network.bridge.enable_icc": "false"})
except Exception as e:
    logger.exception("Error caught: %s", e)
    logging.error(f"Could not connect to Docker daemon on startup. Please ensure Docker is running. Code execution will fail. Error: {e}")

from functools import wraps

def require_approval(f):
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
    if not user_id:
        return "stellar_isolated"
    network_name = f"stellar_net_{user_id}"
    try:
        docker_client.networks.get(network_name)
    except docker.errors.NotFound:
        try:
            docker_client.networks.create(network_name, driver="bridge", options={"com.docker.network.bridge.enable_icc": "false"})
        except docker.errors.APIError:
            pass # Ignore if created concurrently
    return network_name

from telegram_bot import TelegramBot

telegram_bot = TelegramBot()

def send_login_notification(username, display_name=None, is_waitlist=False):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    name_str = f"{display_name} ({username})" if display_name else username
    if is_waitlist:
        message_body = f"⏳ New Waitlist Registration\nUser: {name_str}\nTime: {timestamp}"
    else:
        message_body = f"✅ User Login on Stellar\nUser: {name_str}\nTime: {timestamp}"
    telegram_bot.send_message(message_body)

# --- LOGGING AND ENV LOADING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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
SANDBOX_DIR = 'sandbox_runs'
os.makedirs(SANDBOX_DIR, exist_ok=True)
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf','docx','pptx', 'png', 'jpg', 'jpeg', 'gif', 'csv', 'md', 'py', 'js', 'html', 'css', 'json', 'xml', 'log', 'c', 'cpp', 'java', 'rb', 'php', 'go', 'rs', 'swift', 'kt','mp4','mp3'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

app.secret_key = os.getenv("FLASK_SECRET_KEY")

app.config['SESSION_COOKIE_NAME'] = 'stellar_session_main'
app.config['SESSION_PERMANENT'] = True

# Constants for Google OAuth (Update these if necessary)
FIREBASE_PROJECT_ID = "stellarai-live"

@app.route('/login/google', methods=['POST'])
def login_google():
    data = request.get_json()
    token = data.get('id_token')
    
    if not token:
        return jsonify({"success": False, "message": "ID token required."}), 400

    try:
        # Verify the ID token using Google's verification library
        # For Firebase, the audience is the Firebase Project ID
        # and the issuer must be https://securetoken.google.com/<project_id>
        try:
            id_info = id_token.verify_firebase_token(
                token, 
                google_requests.Request(), 
                audience=FIREBASE_PROJECT_ID
            )
        except Exception as ve:
            logger.error(f"Token verification failed: {ve}")
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
            notification_thread = threading.Thread(
                target=send_login_notification,
                args=(email, name, is_waitlist),
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
app.config['SESSION_REDIS'] = redis.StrictRedis(host='localhost', port=6379, db=1)

Session(app)


PRIMARY_API_KEY = os.getenv("PRIMARY_API_KEY")
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
# GLOBAL THREAD-SAFE GEMINI API KEY RATE LIMIT MANAGER
# -------------------------------------------------------------
import time
from threading import Lock

class GlobalKeyManager:
    def __init__(self):
        self.lock = Lock()
        self.blocked_until = {} 
        self.block_reason = {} 
        
    def _get_redis_keys(self, key_val, model_id):
        import hashlib
        key_hash = hashlib.sha256(key_val.encode('utf-8')).hexdigest()
        scope = model_id if model_id is not None else "global"
        return f"stellar:blocked_until:{key_hash}:{scope}", f"stellar:block_reason:{key_hash}:{scope}"

    def block_key(self, key_val, model_id, duration_seconds, reason='RPM'):
        if reason in ('RPD', 'INVALID'):
            model_id = None
            
        with self.lock:
            self.blocked_until[(key_val, model_id)] = time.time() + duration_seconds
            self.block_reason[(key_val, model_id)] = reason
            
        try:
            k_until, k_reason = self._get_redis_keys(key_val, model_id)
            redis_client.setex(k_until, int(duration_seconds), str(time.time() + duration_seconds))
            redis_client.setex(k_reason, int(duration_seconds), reason)
        except Exception as e:
            logger.error(f"Error writing key block to Redis: {e}")
            
    def is_key_blocked(self, key_val, model_id):
        # Try checking Redis first to coordinate between different processes
        try:
            k_until, k_reason = self._get_redis_keys(key_val, model_id)
            blocked_until_val = redis_client.get(k_until)
            if blocked_until_val:
                try:
                    blocked_until_time = float(blocked_until_val)
                    if time.time() < blocked_until_time:
                        reason = redis_client.get(k_reason) or 'RPM'
                        return True, reason
                except ValueError:
                    pass
            
            # Check global block in Redis
            if model_id is not None:
                k_until_g, k_reason_g = self._get_redis_keys(key_val, None)
                blocked_until_val_g = redis_client.get(k_until_g)
                if blocked_until_val_g:
                    try:
                        blocked_until_time_g = float(blocked_until_val_g)
                        if time.time() < blocked_until_time_g:
                            reason = redis_client.get(k_reason_g) or 'RPM'
                            return True, reason
                    except ValueError:
                        pass
        except Exception as e:
            logger.error(f"Error reading key block from Redis: {e}")

        # Fallback to local process memory if Redis is unavailable or hasn't cached it
        with self.lock:
            # Check model-specific block first
            blocked_time = self.blocked_until.get((key_val, model_id), 0)
            if time.time() < blocked_time:
                return True, self.block_reason.get((key_val, model_id), 'RPM')
            
            # Check global model block
            if model_id is not None:
                blocked_time_global = self.blocked_until.get((key_val, None), 0)
                if time.time() < blocked_time_global:
                    return True, self.block_reason.get((key_val, None), 'RPM')
                    
            return False, None

    def get_key_blocks(self, key_val, models):
        blocks = {}
        global_blocked, global_reason = self.is_key_blocked(key_val, None)
        if global_blocked:
            remaining = 0
            try:
                k_until, _ = self._get_redis_keys(key_val, None)
                blocked_until_val = redis_client.get(k_until)
                if blocked_until_val:
                    remaining = max(0.0, float(blocked_until_val) - time.time())
            except Exception:
                pass
            if remaining == 0:
                with self.lock:
                    blocked_time = self.blocked_until.get((key_val, None), 0)
                    remaining = max(0.0, blocked_time - time.time())
            blocks["global"] = {
                "blocked": True,
                "reason": global_reason or 'RPM',
                "remaining_seconds": int(remaining)
            }
        else:
            blocks["global"] = {
                "blocked": False,
                "reason": None,
                "remaining_seconds": 0
            }

        for model in models:
            model_blocked = False
            model_reason = None
            model_remaining = 0.0
            
            try:
                k_until, k_reason = self._get_redis_keys(key_val, model)
                blocked_until_val = redis_client.get(k_until)
                if blocked_until_val:
                    try:
                        blocked_until_time = float(blocked_until_val)
                        if time.time() < blocked_until_time:
                            model_blocked = True
                            model_reason = redis_client.get(k_reason) or 'RPM'
                            model_remaining = max(0.0, blocked_until_time - time.time())
                    except ValueError:
                        pass
            except Exception:
                pass
                
            if not model_blocked:
                with self.lock:
                    blocked_time = self.blocked_until.get((key_val, model), 0)
                    if time.time() < blocked_time:
                        model_blocked = True
                        model_reason = self.block_reason.get((key_val, model), 'RPM')
                        model_remaining = max(0.0, blocked_time - time.time())
            
            effective_blocked, effective_reason = self.is_key_blocked(key_val, model)
            if effective_blocked:
                if model_blocked:
                    blocks[model] = {
                        "blocked": True,
                        "reason": model_reason,
                        "remaining_seconds": int(model_remaining),
                        "type": "model_specific"
                    }
                else:
                    blocks[model] = {
                        "blocked": True,
                        "reason": global_reason,
                        "remaining_seconds": int(blocks["global"]["remaining_seconds"]),
                        "type": "global"
                    }
            else:
                blocks[model] = {
                    "blocked": False,
                    "reason": None,
                    "remaining_seconds": 0,
                    "type": None
                }
        return blocks

KEY_MANAGER = GlobalKeyManager()
ACTIVE_CHATS_CANCEL_EVENTS = {}

def get_seconds_until_pacific_midnight():
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
        tomorrow_pacific = datetime.datetime(now_pacific.year, now_pacific.month, now_pacific.day) + datetime.timedelta(days=1)
        
        seconds_until_midnight = (tomorrow_pacific - now_pacific).total_seconds()
        return max(int(seconds_until_midnight), 60)
    except Exception as e:
        logger.error(f"Error calculating Pacific midnight offset: {e}")
        return 14400 # Fallback to 4 hours if datetime calculations fail

def parse_quota_block_duration(error_msg):
    err_lower = error_msg.lower()
    if ('requestsperday' in err_lower or 'requests per day' in err_lower or 
        'daily' in err_lower or 'perday' in err_lower or 'projectpermodel-freetier' in err_lower or
        'exceeded your current quota' in err_lower or 'billing details' in err_lower or 'quota/rate limits' in err_lower):
        # Daily limit / Quota exhaustion: Block until the next Pacific Midnight reset time
        duration = get_seconds_until_pacific_midnight()
        return duration, 'RPD'
    elif ('overloaded' in err_lower or '503' in err_lower or 'service unavailable' in err_lower or 'service_unavailable' in err_lower):
        # Model overloaded / 503: Block key for 30 seconds to let Google cool down
        return 30, 'OVERLOAD'
    elif ('500' in err_lower or 'internal error' in err_lower or 'internal_error' in err_lower):
        # Internal error / 500: Block key for 10 seconds
        return 10, 'INTERNAL'
    # Minute limit / TPM / RPM: Block for 60 seconds
    return 60, 'RPM'
# -------------------------------------------------------------


def get_fallback_chain(start_model):
    chain = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemma-4-31b-it"]
    if start_model in chain:
        idx = chain.index(start_model)
        return chain[idx:]
    return [start_model, "gemma-4-31b-it"]

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
# (PRIMARY_API_KEY already assigned in aggressive loading block)

backup_env_pattern = re.compile(r'^BACKUP_API_KEY_(\d+)$')

# 2. Extract into a sorted dictionary to maintain numerical order
backup_vars = {
    int(match.group(1)): os.environ[key]
    for key in os.environ
    if (match := backup_env_pattern.match(key))
}

# 3. Final list of functional backup keys (automatically scales)
BACKUP_API_KEYS = [backup_vars[i] for i in sorted(backup_vars.keys())]

tavily_backup_env_pattern = re.compile(r'^TAVILY_BACKUP_API_KEY_(\d+)$')
tavily_backup_vars = {
    int(match.group(1)): os.environ[key]
    for key in os.environ
    if (match := tavily_backup_env_pattern.match(key))
}
TAVILY_BACKUP_API_KEYS = [tavily_backup_vars[i] for i in sorted(tavily_backup_vars.keys())]

DATABASE_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stellar_local.db')

def _fetch_as_dict(cursor):
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def get_or_create_encryption_key():
    key_path = Path(script_dir / 'encryption.key')
    if key_path.is_file():
        with open(key_path, 'rb') as key_file:
            key = key_file.read()
    else:
        key = Fernet.generate_key()
        with open(key_path, 'wb') as key_file:
            key_file.write(key)
    return key

ENCRYPTION_KEY = get_or_create_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)


def _fetchone_as_dict(cursor):
    row = cursor.fetchone()
    if row:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
    return None

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE_NAME)
        g.db.row_factory = sqlite3.Row
        # Enable WAL mode and set timeout for concurrency
        g.db.execute("PRAGMA journal_mode=WAL;")
        g.db.execute("PRAGMA busy_timeout=5000;")
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def initialize_database():
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
                print("Added 'visualization_html' column to 'messages' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'visualization_html' column: {e}")

        # Migration: Add hidden column if it doesn't exist
        if 'hidden' not in columns:
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN hidden BOOLEAN DEFAULT 0")
                print("Added 'hidden' column to 'messages' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'hidden' column: {e}")

        # Migration: Add attached_files column for Native Gemini File URIs
        if 'attached_files' not in columns:
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN attached_files TEXT")
                print("Added 'attached_files' column to 'messages' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'attached_files' column: {e}")

        # Add user_logs_prefs table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_logs_prefs'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE user_logs_prefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, -- user email or 'global'
                log_entry TEXT NOT NULL,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )''')
            print("Created 'user_logs_prefs' table.")

        cursor.execute("PRAGMA table_info(users)")
        users_columns = [info[1] for info in cursor.fetchall()]
        if 'display_name' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
                print("Added 'display_name' column to 'users' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'display_name' column: {e}")

        if 'last_active' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN last_active DATETIME")
                print("Added 'last_active' column to 'users' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'last_active' column: {e}")

        if 'pfp_url' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN pfp_url TEXT")
                print("Added 'pfp_url' column to 'users' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'pfp_url' column: {e}")

        if 'designation' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN designation TEXT")
                print("Added 'designation' column to 'users' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'designation' column: {e}")

        if 'source' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN source TEXT")
                print("Added 'source' column to 'users' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'source' column: {e}")

        if 'use_case' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN use_case TEXT")
                print("Added 'use_case' column to 'users' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'use_case' column: {e}")

        if 'waitlist_form_submitted' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN waitlist_form_submitted BOOLEAN DEFAULT 0")
                print("Added 'waitlist_form_submitted' column to 'users' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'waitlist_form_submitted' column: {e}")

        cursor.execute("PRAGMA table_info(chats)")
        chats_columns = [info[1] for info in cursor.fetchall()]
        if 'token_count' not in chats_columns:
            try:
                cursor.execute("ALTER TABLE chats ADD COLUMN token_count INTEGER DEFAULT 0")
                print("Added 'token_count' column to 'chats' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'token_count' column: {e}")

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
                print("Added 'subdomain' column to 'repo_history' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'subdomain' column: {e}")

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
                print("Added 'hidden' column to 'tool_calls' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'hidden' column to tool_calls: {e}")

        cursor.execute("PRAGMA table_info(chats)")
        chats_columns = [info[1] for info in cursor.fetchall()]
        if 'is_temp' not in chats_columns:
            try:
                cursor.execute("ALTER TABLE chats ADD COLUMN is_temp BOOLEAN DEFAULT 0")
                print("Added 'is_temp' column to 'chats' table.")
            except Exception as e:
                logger.exception("Error caught: %s", e)
                print(f"Error adding 'is_temp' column: {e}")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'")
        if cursor.fetchone() is None:
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
                    print("Added 'metadata' column to 'scheduled_tasks' table.")
                except Exception as e:
                    logger.exception("Error caught: %s", e)
                    print(f"Error adding 'metadata' column to scheduled_tasks: {e}")

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
            print("Created 'agent_feedback' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='talents'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE talents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                talent_name TEXT UNIQUE NOT NULL,
                mandate_text TEXT NOT NULL
            )''')
            print("Created 'talents' table.")

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
            print("Created 'push_subscriptions' table.")

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
                    print(f"Migrated {migrated} PWA push subscriptions from SQLite to Redis.")
                    cursor.execute("DELETE FROM push_subscriptions")
                    db.commit()
        except Exception as e:
            print(f"Error migrating push subscriptions to Redis: {e}")

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
            print("Created 'sentinel_app_errors' table.")

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
            print("Created 'sentinel_app_patches' table.")

        # Add performance indexes for foreign key lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_chat_id ON tool_calls(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_repo_history_user_id ON repo_history(user_id)")

        db.commit()

initialize_database()

def get_current_session_id():
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

            if message_type == "user" and user_query_for_name and not hidden:
                num_messages_in_chat = db.execute('SELECT COUNT(*) FROM messages WHERE chat_id = ?', (chat_id,)).fetchone()[0]
                if num_messages_in_chat == 1 or (num_messages_in_chat -1) % 10 == 0:
                    def thread_target(app_instance, target_chat_id, target_query):
                        with app_instance.app_context():
                            generate_chat_name(target_chat_id, target_query)
                    
                    threading.Thread(target=thread_target, args=(current_app._get_current_object(), chat_id, user_query_for_name), daemon=True).start()

            # Trigger Token Count update in background
            def token_update_thread(app_instance, target_chat_id):
                with app_instance.app_context():
                    try:
                        count_chat_tokens(target_chat_id)
                    except Exception as e:
                        logger.error(f"Error in token_update_thread: {e}")

            threading.Thread(target=token_update_thread, args=(current_app._get_current_object(), chat_id), daemon=True).start()

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
                logger.error(f"Failed to broadcast new message: {e}")

            return last_id
        except sqlite3.OperationalError as e:
            logger.error(f"Database error in insert_message (Attempt {attempt + 1}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries - 1:
                time.sleep(retry_delay_seconds)
            else:
                return None
        except Exception as e:
            logger.error(f"Unexpected error in insert_message: {e}", exc_info=True)
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
    with app.app_context():
        db = get_db()
        try:
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
                    r = chat.send_message(prompt)
                    
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
                token_count_response = client.models.count_tokens(
                    model="gemini-3.1-flash-lite", contents=history_for_tokens
                )
                t_count = token_count_response.total_tokens
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
                g_file = client.files.upload(file=filepath)
                
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

    return jsonify({
        'status': f"Saved {len(successful_uploads)} file(s) locally.",
        'uploaded_files': successful_uploads
    }), 200

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(' ', '_')
    sanitized = re.sub(r'[^\w\-\.]+', '', filename)
    return sanitized[:100] if len(sanitized) > 100 else sanitized

def is_safe_hostname(hostname):
    """Helper to resolve hostname and check if all associated IPs are safe for SSRF protection."""
    if not hostname:
        return False, "Invalid hostname"
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for family, kind, proto, canonname, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if (ip.is_private or ip.is_loopback or ip.is_link_local or 
                    ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                    return False, f"Access to internal networks is forbidden: {ip_str}"
            except ValueError:
                continue
    except socket.gaierror:
        return False, "Failed to resolve hostname"
    return True, "Safe"

def scrape_url(url: str) -> str:
    if not url or not url.startswith(('http://', 'https://')):
        return f"Error scraping {url}: Invalid URL format"
    
    from urllib.parse import urlparse
    
    try:
        parsed = urlparse(url)
        safe, msg = is_safe_hostname(parsed.hostname)
        if not safe:
            logger.warning(f"Blocked scraping SSRF attempt: {msg} via {url}")
            return f"Error scraping {url}: {msg}"

        apron=webscrapper.scrape_url(url)
        print(apron)
        return apron
    except Exception as e:
        logger.exception("Error caught: %s", e)
        return f"Error scraping {url}: {str(e)}"

stop_sequence="8919018818"

def is_output_cut_off(text: str, key: str) -> bool:
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
        client = genai.Client(api_key=key, http_options={'api_version': 'v1beta'})
        chat = client.chats.create(model='gemini-3.1-flash-lite', config={'tools': []})
        r = chat.send_message(check_prompt)
        
        if r.candidates and r.candidates[0].content and r.candidates[0].content.parts:
            response_text = r.candidates[0].content.parts[0].text.strip().upper()
            if "NO" in response_text:
                return True
            else:
                return False
        else:
            return False
    except Exception as e:
        logger.exception("Error caught: %s", e)
        return False


def gemini_generate(prompt: str, model_id: str, key: str, attempts: int = 3, backoff_factor: float = 1.5, model_display_name=None, username=None, chat_id=None, disabled_tools=None, gemini_files_data=None, cancel_event=None):
    from flask import g
    g.model_id = model_id # Set ground-truth model for tools
    display_name = model_display_name or MODEL_NAMES.get(model_id)
    logger.info(f"Initiating gemini_generate with model: {model_id} ({display_name})")

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
    
    # Filter out globally rate-limited or quota-exhausted keys to avoid time/resource waste
    active_keys = []
    blocked_info = []
    for k in keys_to_try:
        is_blocked, reason = KEY_MANAGER.is_key_blocked(k, model_id)
        if not is_blocked:
            active_keys.append(k)
        else:
            blocked_info.append((k, reason))
            
    if not active_keys:
        # Fallback: if ALL keys are blocked, try them all to avoid complete lockout
        active_keys = keys_to_try
    else:
        if blocked_info:
            logger.info(f"Skipped {len(blocked_info)} globally blocked/exhausted API key(s) to protect from redundant 429 hits.")
            
    keys_to_try = active_keys
    

    current_key_index = 0

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
                    r = chat.send_message(message_to_send)
                    consecutive_network_errors = 0 # Reset on success
                except Exception as loop_e:
                    logger.exception("Error caught: %s", loop_e)
                    error_string = str(loop_e).lower()
                    is_quota = any(x in error_string for x in ['429', '403', 'permission_denied', 'resource_exhausted', 'quota', 'rate limit'])
                    is_network = any(x in error_string for x in ['500', '503', 'connection', 'timeout', 'deadline'])
                    
                    if is_quota and (current_key_index + 1) < len(keys_to_try):
                        logger.warning(f"Quota exceeded for key index {current_key_index}. Switching to backup key...")
                        block_duration, block_reason = parse_quota_block_duration(error_string)
                        block_scope = None if ('403' in error_string or 'permission_denied' in error_string or 'invalid' in error_string) else model_id
                        KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                        logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to {block_reason} error in inner loop.")
                        current_key_index += 1
                        current_key = keys_to_try[current_key_index]
                        
                        
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
                        if consecutive_network_errors == 3 and (current_key_index + 1) < len(keys_to_try):
                             logger.warning(f"Persistent network issues with key {current_key_index}. Switching to backup key...")
                             block_duration, block_reason = parse_quota_block_duration(error_string)
                             block_scope = model_id
                             KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                             logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to persistent network issues in inner loop.")
                             current_key_index += 1
                             current_key = keys_to_try[current_key_index]
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
                                            import sqlite3
                                            db_temp = sqlite3.connect(DATABASE_NAME)
                                            db_temp.row_factory = sqlite3.Row
                                            row = db_temp.execute('SELECT user_id FROM chats WHERE id = ?', (chat_id,)).fetchone()
                                            if row:
                                                p_user_id = row['user_id']
                                            db_temp.close()
                                        
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
                                    g_state = {k: getattr(g, k) for k in ['user_id', 'username', 'chat_id', 'session_id', 'model_id'] if hasattr(g, k)}
                                    
                                    def _run_tool_with_context(**kwargs):
                                        with app_obj.app_context():
                                            for k, v in g_state.items():
                                                setattr(g, k, v)
                                            return func_to_call(**kwargs)

                                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                                    future = executor.submit(_run_tool_with_context, **args_dict)
                                    try:
                                        res = future.result(timeout=timeout_val)
                                    except concurrent.futures.TimeoutError:
                                        res = f"Error: Tool '{func_name}' was stopped because it exceeded the timeout of {timeout_val} seconds that the agent set for that tool."
                                    finally:
                                        executor.shutdown(wait=False)
                            
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
                                # Get the ID of the record we JUST inserted
                                last_tool_id = "unknown"
                                try:
                                    db = get_db()
                                    cursor = db.execute('SELECT id FROM tool_calls WHERE chat_id = ? ORDER BY id DESC LIMIT 1', (chat_id,))
                                    row = cursor.fetchone()
                                    if row: last_tool_id = row[0]
                                except: pass
                                
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
                 if attempt < attempts or (current_key_index + 1) < len(keys_to_try):
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
            
            is_quota_error = ('429' in error_string or '403' in error_string or 'permission_denied' in error_string or 'resource_exhausted' in error_string or 'quota' in error_string or 'rate limit' in error_string)
            is_transient_error = ('overloaded' in error_string or '503' in error_string or 'service unavailable' in error_string or '500' in error_string or 'internal error' in error_string or 'internal_error' in error_string)
            
            if is_quota_error or is_transient_error:
                 is_blockable_error = True
                 block_duration, block_reason = parse_quota_block_duration(error_string)
                 block_scope = None if ('403' in error_string or 'permission_denied' in error_string or 'invalid' in error_string) else model_id
                 KEY_MANAGER.block_key(current_key, block_scope, block_duration, block_reason)
                 logger.warning(f"Globally blocked API key (Hash: {hash(current_key)}) for {block_duration}s for model {block_scope} due to {block_reason} error in generation loop.")

            if is_blockable_error and (current_key_index + 1) < len(keys_to_try):
                if is_quota_error:
                    yield {'status': f'Quota exceeded. Switching to backup key...'}
                else:
                    yield {'status': f'Google API encountered transient error ({block_reason}). Switching to backup key...'}
                current_key_index += 1
            elif is_blockable_error:
                if is_quota_error:
                    yield {'status': f'Quota exceeded on all keys. Cannot proceed.'}
                else:
                    yield {'status': f'Google API transient errors on all keys. Cannot proceed.'}
                break

            if attempt < attempts:
                 yield {'status': f"Encountered error, retrying..."}
                 if not is_blockable_error:
                    current_key_index = (current_key_index + 1) % len(keys_to_try) if keys_to_try else 0
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
                logger.exception("Error caught: %s", e)
                pass
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except OSError:
                    pass
            return None
    except Exception as e:
        logger.exception("Error caught: %s", e)
        return None
    return None

GRACE_PERIOD_SECONDS = 30

def _redis_repo_key(pid): 
    return f"repo:process:{pid}"

def _redis_runcode_key(pid):
    return f"runcode:process:{pid}"

def _get_process_key_prefix(process_id, app_type='repo'):
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
    logs_buffer = []

    user_id = None
    with app_obj.app_context():
        db = get_db()
        cursor = db.execute('SELECT user_id FROM repo_history WHERE process_id = ?', (process_id,))
        row = cursor.fetchone()
        if row: user_id = row['user_id']
    
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
                    logger.exception("Error caught.")
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
                            logger.exception("Error caught.")
                            break
                    
                    # Find mount path to update files
                    for mount in container.attrs.get('Mounts', []):
                        if mount['Destination'] == '/app':
                            temp_dir_path = mount['Source']
                            break
                else:
                    _put_event({'type': 'log', 'content': f'Dependencies changed. Rebuilding container ({old_container.short_id})...'})
                    old_container.stop(timeout=10)
                    old_container.remove(force=True)
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.exception("Error caught: %s", e)
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

            _put_event({'type': 'container_id', 'id': container.id})
            _put_event({'type': 'log', 'content': f'Sandbox container ({container.short_id}) created.'})

            update_history(status='created', container_id=container.id)
        else:
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
                
                _put_event({'type': 'log', 'content': '✅ Dependencies installed successfully.'})
            except Exception as pip_err:
                logger.error(f"Pip install error for {process_id}: {pip_err}")
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
                logger.exception("Error caught.")
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
                    logger.exception("Error caught: %s", exec_err)
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
                 except: pass

        if not public_url_found:
            _put_event({'type': 'error', 'content': 'Failed to get public URL. Container may have crashed.'})
            update_history(status='failed', final_logs="\n".join(logs_buffer))
            try: redis_client.hset(redis_key, mapping={"status": "failed"})
            except: pass
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
        except: pass

    finally:
        # Check current status before marking as stopped
        current_status = 'starting'
        try:
            val = redis_client.hget(redis_key, "status")
            if val: current_status = val
        except: pass

        if current_status != 'failed':
            update_history(status='stopped', final_logs="\n".join(logs_buffer))
        
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

        def _delayed_cleanup(pid, r_key, delay=GRACE_PERIOD_SECONDS):
            time.sleep(delay)
            with active_apps_lock:
                active_apps.pop(pid, None)
            try:
                redis_client.delete(r_key)
            except Exception:
                logger.exception("Failed to delete redis key for %s", pid)

        cleanup_thread = threading.Thread(target=_delayed_cleanup, args=(process_id, redis_key,), daemon=True)
        cleanup_thread.start()

def stop_and_cleanup_app_by_process_id(process_id, app_type='repo'):
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
            container = client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
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
        redis_client.setex(f"stream_started:{query_id}", 3600 * 24, "1")
        
        cancel_event = threading.Event()
        if chat_id:
            old_event = ACTIVE_CHATS_CANCEL_EVENTS.get(chat_id)
            if old_event:
                logger.info(f"Dynamic interrupt/cancellation requested for chat_id: {chat_id}. Terminating old thread.")
                old_event.set()
            ACTIVE_CHATS_CANCEL_EVENTS[chat_id] = cancel_event

        def generator_task(cancel_event=None):
            from flask import g
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
                                 row = db_temp.execute('SELECT user_id FROM chats WHERE id = ?', (chat_id,)).fetchone()
                                 if row:
                                     p_user_id = row['user_id']
                                 db_temp.close()
                                 
                             if p_user_id:
                                 # Limit notification body preview
                                 preview_body = refined_query_result or "Task execution completed successfully."
                                 # Strip markdown for clean preview
                                 import re as _re
                                 preview_body = _re.sub(r'[*#`_\-\[\]]', '', preview_body)
                                 if len(preview_body) > 120:
                                     preview_body = preview_body[:117].strip() + "..."
                                 send_push_notification(
                                     user_id=p_user_id,
                                     title="Stellar: Task Completed",
                                     body=preview_body,
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

    redis_client.setex(f"stop_flag:{query_id}", 3600, "1")
    if chat_id:
        redis_client.delete(f"chat_active_query:{chat_id}")
        cancel_event = ACTIVE_CHATS_CANCEL_EVENTS.get(chat_id)
        if cancel_event:
            logger.info(f"Stop button clicked: Signalling thread termination for chat_id: {chat_id}")
            cancel_event.set()
        
    logging.info(f"Stop flag set in Redis for query_id: {query_id}")
    return jsonify({'success': True, 'message': 'Stop signal received.'})

def check_and_log_stop(query_id, stage=""):
    if redis_client.exists(f"stop_flag:{query_id}"):
        logging.info(f"Stop signal detected for query_id: {query_id} at stage: {stage}")
        return True
    return False

def stream_consumer(query_id):
    """Consumer for replaying historical events and subscribing to live events."""
    pubsub = redis_client.pubsub()
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
    for message in pubsub.listen():
        if message['type'] == 'message':
            data = message['data']
            if isinstance(data, bytes):
                data = data.decode('utf-8')
            if data == "__STREAM_END__":
                yield f"data: {json.dumps({'status': 'Stream ended.', 'error': True, 'stopped': True})}\n\n"
                break
            yield data
    pubsub.close()

def background_thread_runner(app_obj, query_id, chat_id, cancel_event, task_func, *args):
    """Wrapper that runs generation streams in the background to decouple from HTTP requests."""
    def run():
        with app_obj.app_context():
            try:
                for chunk in task_func(cancel_event, *args):
                    redis_client.rpush(f"stream_history:{query_id}", chunk)
                    redis_client.publish(f"stream:{query_id}", chunk)
            except Exception as e:
                logger.error(f"Stream background task error for {query_id}: {e}", exc_info=True)
                err_str = f"data: {json.dumps({'status': f'Internal Background Error: {str(e)}', 'error': True})}\n\n"
                redis_client.rpush(f"stream_history:{query_id}", err_str)
                redis_client.publish(f"stream:{query_id}", err_str)
            finally:
                if chat_id in ACTIVE_CHATS_CANCEL_EVENTS and ACTIVE_CHATS_CANCEL_EVENTS[chat_id] == cancel_event:
                    ACTIVE_CHATS_CANCEL_EVENTS.pop(chat_id, None)
                redis_client.rpush(f"stream_history:{query_id}", "__STREAM_END__")
                redis_client.publish(f"stream:{query_id}", "__STREAM_END__")
                redis_client.delete(f"chat_active_query:{chat_id}")
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
        logger.info(f"Attempting to delete message {message_id} for user {user_id}")
        # Verify ownership by checking the chat the message belongs to
        cursor = db.execute('''
            SELECT m.id, c.user_id FROM messages m
            JOIN chats c ON m.chat_id = c.id
            WHERE m.id = ? AND c.user_id = ?
        ''', (message_id, user_id))
        
        row = cursor.fetchone()
        if not row:
            logger.warning(f"Deletion failed: Message {message_id} not found or unauthorized for user {user_id}")
            return jsonify({'error': 'Message not found or unauthorized.'}), 403

        logger.info(f"Ownership verified for message {message_id}. Proceeding with deletion.")
        db.execute('DELETE FROM messages WHERE id = ?', (message_id,))
        db.commit()

        logger.info(f"User {user_id} successfully deleted message {message_id}.")
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

        logging.info(f"User {user_id} deleted {deleted_count} message(s) in chat {chat_id} after message {message_id}.")
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
            except: pass
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
    from urllib.parse import urlparse
    
    image_url = request.args.get('url')
    if not image_url or not image_url.startswith(('http://', 'https://')):
        return "Invalid URL", 400
        
    try:
        # 1. SSRF Protection: Prevent access to internal/private networks
        parsed = urlparse(image_url)
        safe, msg = is_safe_hostname(parsed.hostname)
        if not safe:
            logger.warning(f"Blocked SSRF attempt: {msg} via {image_url}")
            return msg, 403

        # 2. Fetch the image with a strict timeout and prevent redirects (SSRF Protection)
        resp = None
        try:
            resp = requests.get(image_url, stream=True, timeout=15, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                resp.close()
                return "Redirects are not allowed for security reasons", 400
            resp.raise_for_status()
            
            # 3. MIME Type Validation: Ensure it's actually an image, not a malicious script/HTML
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
    except socket.gaierror:
        return "Failed to resolve hostname", 400
    except Exception as e:
        logger.error(f"Image proxy unexpected error for {image_url}: {e}")
        return "Internal server error", 500

# INTENTIONALLY UNPROTECTED: This route omits @require_approval to allow users to easily share generated files and outputs via direct links.
@app.route('/download/<path:filename>')
def download_file(filename):
    if '..' in filename or filename.startswith('/'):
        return "Invalid path", 400
    directory = os.path.abspath(os.path.join(os.path.dirname(__file__), "outputs"))
    file_path = os.path.join(directory, filename)
    if not os.path.abspath(file_path).startswith(directory):
         return "Access denied", 403
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({'status': 'Failed: File not found'}), 404
    
    # Use dirname and basename for send_from_directory to correctly serve sub-paths
    subdir = os.path.dirname(filename)
    basename = os.path.basename(filename)
    return send_from_directory(os.path.join(directory, subdir), basename, as_attachment=True)

# INTENTIONALLY UNPROTECTED: This route omits @require_approval to allow users to easily share generated files and outputs via direct links.
@app.route('/view/<path:filename>')
def view_file(filename):
    if '..' in filename or filename.startswith('/'):
        return "Invalid path", 400
    directory = os.path.abspath(os.path.join(os.path.dirname(__file__), "outputs"))
    file_path = os.path.join(directory, filename)
    if not os.path.abspath(file_path).startswith(directory):
         return "Access denied", 403
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
         return "File not found", 404
         
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

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        logger.info(f"SUCCESS: Approval email sent successfully to {recipient_email}.")
    except Exception as e:
        logger.error(f"FAILURE sending approval email: {str(e)}")

def send_revocation_email(recipient_email, display_name):
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

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        logger.info(f"SUCCESS: Revocation email sent successfully to {recipient_email}.")
    except Exception as e:
        logger.error(f"FAILURE sending revocation email: {str(e)}")

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
            
    response_data = []
    for item in keys_to_report:
        key_val = item['value']
        masked = key_val[:8] + "..." + key_val[-4:] if len(key_val) > 12 else key_val
        blocks = KEY_MANAGER.get_key_blocks(key_val, list(MODEL_NAMES.keys()))
        
        response_data.append({
            'label': item['label'],
            'masked': masked,
            'blocks': blocks
        })
        
    return jsonify(response_data), 200

@app.route('/api/admin/waitlist', methods=['GET'])
def get_admin_waitlist():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    db = get_db()
    # Calculate last_active, num_chats, num_projects, and total_tokens_approx
    query = """
        SELECT
            u.id, u.username, u.display_name, u.role, u.is_approved, u.created_at, u.last_active,
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

        recipient = user['username'] if '@' in user['username'] else None
        if recipient:
            if new_status and not user['is_approved']:
                threading.Thread(target=send_approval_email, args=(recipient, user['username']), daemon=True).start()
            elif not new_status and user['is_approved']:
                threading.Thread(target=send_revocation_email, args=(recipient, user['username']), daemon=True).start()

        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception("Error caught: %s", e)
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
        
        # If user_email is not provided, try to use username if it looks like an email
        recipient = user_email or (user['username'] if '@' in user['username'] else None)
        
        if recipient:
            threading.Thread(target=send_approval_email, args=(recipient, user['username']), daemon=True).start()
            
        return jsonify({'success': True, 'message': f"User {user['username']} approved."}), 200
    except sqlite3.Error as e:
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
         
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['display_name'] = user['display_name']
    session['role'] = user['role']
    session['is_approved'] = bool(user['is_approved'])
    session.pop('current_chat_id', None)
    
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
        logger.warning(f"Could not delete Redis session on logout: {e}")
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
_SSH_CODE_LENGTH = 6
_SSH_CODE_TTL = 300  # 5 minutes
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
                <div class="code-value" id="codeValue">------</div>
                <button class="copy-btn" id="copyBtn" onclick="copyCode()" style="display: inline-flex; align-items: center; justify-content: center; gap: 8px;">
                    <svg class="copy-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                    <span>Copy Code</span>
                </button>
                <div class="timer">Expires in <span id="countdown">5:00</span></div>
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
    startTimer(300);
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
            with open('templates/waitlist.html', 'r') as f:
                content = f.read()
            response = make_response(content)
            response.headers['Content-Type'] = 'text/html'
            return response
            
    return _SSH_AUTH_PAGE_HTML, 200, {'Content-Type': 'text/html'}

@app.route('/api/ssh/generate-code', methods=['POST'])
@require_approval
def ssh_generate_code():
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

    # Format as XXX-XXX for display
    display_code = f'{code[:3]}-{code[3:]}'
    return jsonify({'code': display_code}), 200

@app.route('/api/ssh/verify-code', methods=['POST'])
def ssh_verify_code():
    # Verify shared secret
    gateway_secret = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')
    data = request.get_json(silent=True)
    if not data or data.get('secret') != gateway_secret:
        return jsonify({'valid': False, 'error': 'Unauthorized'}), 403

    # Rate limit failed attempts per IP
    client_ip = request.remote_addr or 'unknown'
    fail_key = f'ssh_verify_fail:{client_ip}'
    fail_count = redis_client.get(fail_key)
    if fail_count and int(fail_count) >= _SSH_VERIFY_FAIL_LIMIT:
        return jsonify({'valid': False, 'error': 'Too many failed attempts. Try again later.'}), 429

    raw_code = data.get('code', '').upper().replace('-', '').replace(' ', '')
    if not raw_code or len(raw_code) != _SSH_CODE_LENGTH:
        return jsonify({'valid': False}), 200

    code_key = f'ssh_auth_code:{raw_code}'
    code_data = redis_client.get(code_key)
    if not code_data:
        # Track failed attempt
        pipe = redis_client.pipeline()
        pipe.incr(fail_key)
        pipe.expire(fail_key, _SSH_VERIFY_FAIL_WINDOW)
        pipe.execute()
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

    return jsonify({
        'valid': True,
        'user_id': parsed.get('user_id'),
        'username': parsed.get('username'),
        'display_name': parsed.get('display_name')
    }), 200

# ==================== End SSH Authentication Routes =======================

@app.route('/check_auth', methods=['GET'])
def check_auth_status():
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
    user_id = session['user_id']
    
    def event_stream():
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
    user_id = session['user_id']
    db = get_db()
    try:
        cursor = db.execute('''
            SELECT c.id, c.name, COALESCE(MAX(m.timestamp), c.created_at) as last_active 
            FROM chats c 
            LEFT JOIN messages m ON c.id = m.chat_id 
            WHERE c.user_id = ? AND c.is_temp = 0
            GROUP BY c.id 
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
    user_id = session['user_id']
    db = get_db()
    try:
        cursor = db.execute('INSERT INTO chats (user_id, name) VALUES (?, ?)', (user_id, 'New Chat'))
        db.commit()
        new_chat_id = cursor.lastrowid
        


        session['current_chat_id'] = new_chat_id
        session.modified = True
        
        return jsonify({'success': True, 'chat_id': new_chat_id, 'name': 'New Chat'}), 201
    except sqlite3.Error as e:
        logger.error(f"Database error in create_new_chat: {e}", exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error in create_new_chat: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/chats/new_temp', methods=['POST'])
@require_approval
def create_temp_chat():
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
        
        return jsonify({'success': True, 'chat_id': new_chat_id}), 201
    except Exception as e:
        logger.error(f"Error in create_temp_chat: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/set_active_chat', methods=['POST'])
@require_approval
def set_active_chat():
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
    
    return jsonify({'success': True, 'message': f'Active chat set to {chat_id}'})
@app.route('/api/chats/<int:chat_id>/delete', methods=['DELETE'])
@require_approval
def delete_chat_route(chat_id):
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
            except: pass
            redis_client.delete(f"chat_active_query:{chat_id}")
        
        db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
        db.execute('DELETE FROM tool_calls WHERE chat_id = ?', (chat_id,))
        db.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
        db.commit()
        
        if session.get('current_chat_id') == chat_id:
            session.pop('current_chat_id', None)
            session.modified = True

        return jsonify({'success': True, 'message': 'Chat deleted successfully.'}), 200
    except sqlite3.Error as e:
        logger.error(f"Database error in delete_chat_route: {e}", exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error in delete_chat_route: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/chats/<int:chat_id>/name', methods=['POST'])
@require_approval
def update_chat_name_route(chat_id):
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
                token_count_response = client.models.count_tokens(
                    model="gemini-3.1-flash-lite", contents=contents
                )
                t_count = token_count_response.total_tokens
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
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    try:
        # Use a short timeout and don't verify SSL if we expect local dev certs (but regular requests is fine here)
        response = requests.get(url, timeout=3, allow_redirects=True)
        return jsonify({'status': response.status_code}), 200
    except Exception as e:
        logger.exception("Error caught: %s", e)
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
            telegram_bot.send_message(msg)
            
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
def send_push_notification(user_id, title, body, url=None):
    """Sends a Web Push notification to all active devices registered by the user in Redis."""
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
            logger.info(f"Cleaned up {len(expired_endpoints)} expired push subscriptions from Redis.")
        except Exception as e:
            logger.error(f"Error cleaning up expired push subscriptions from Redis: {e}")
            
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
            logger.info(f"Sentinel: Queued healing task for app {process_id}, error_id={error_id}")
        else:
            logger.info(f"Sentinel: Logged error for app {process_id} (error_id={error_id}) but skipped healing (non-owner visitor).")
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

        # Ownership is validated implicitly: we resolved owner_id from the DB
        # via the subdomain in the reported URL. The session cookie is NOT
        # available here because this request comes from a cross-subdomain iframe
        # (SameSite=Lax blocks cookie sending across subdomains), so checking
        # session.get('user_id') would always return None and disable healing.
        is_owner = owner_id is not None

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
            for message in pubsub.listen():
                if message['type'] == 'message':
                    data = message['data'].decode('utf-8') if isinstance(message['data'], bytes) else message['data']
                    yield f"data: {data}\n\n"
                    try:
                        if json.loads(data).get('event') in ['healed', 'failed']:
                            break
                    except Exception:
                        pass
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
            session['current_chat_id'] = int(chat_id)
        except ValueError:
            pass

    def serve_no_cache(filename):
        with open(filename, 'r') as f:
            content = f.read()
        response = make_response(content)
        response.headers['Content-Type'] = 'text/html'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    if 'user_id' not in session:
        return serve_no_cache('templates/login.html')

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
@app.route('/api/chats/search_messages', methods=['GET'])
@require_approval
def search_messages_route():
    user_id = session['user_id']
    search_term = request.args.get('search_term', '').strip()

    if not search_term:
        return jsonify({'results': {}}), 200

    db = get_db()
    try:
        cursor = db.execute('''
            SELECT
                T1.id AS chat_id,
                T1.name AS chat_name,
                T2.id AS message_id,
                T2.message_content,
                T2.message_type
            FROM chats AS T1
            LEFT JOIN messages AS T2 ON T1.id = T2.chat_id
            WHERE T1.user_id = ? AND T1.is_temp = 0 AND (
                T1.name LIKE ? OR
                T2.message_content LIKE ?
            )
            ORDER BY T1.created_at DESC, T2.timestamp ASC
        ''', (user_id, f'%{search_term}%', f'%{search_term}%'))

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

            for line_bytes in container.logs(stream=True, follow=True):
                cleaned_line = line_bytes.decode('utf-8', 'replace').strip()
                if cleaned_line:
                    yield f"data: {json.dumps({'type': 'log', 'content': cleaned_line})}\n\n"
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
        logging.info(f"Successfully saved/updated API keys for user {user_id}.")
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
            except: pass

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
    def __init__(self, interval=60):
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            try:
                self._cleanup_orphans()
            except Exception as e:
                logger.error(f"Error in OrphanContainerMonitor: {e}")
            time.sleep(self.interval)

    def _cleanup_orphans(self):
        if not client:
            return

        # List all containers managed by Stellar with our label
        try:
            containers = client.containers.list(all=True, filters={"label": "stellar_type"})
        except Exception as e:
            logger.error(f"OrphanContainerMonitor: Failed to list containers: {e}")
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
                        logger.info(f"OrphanContainerMonitor: Removed exited container {container.short_id}")
                    except docker.errors.NotFound:
                        pass
                    except Exception as e:
                        logger.error(f"OrphanContainerMonitor: Error removing exited container {container.short_id}: {e}")
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
                        logger.warning(f"OrphanContainerMonitor: Found orphan container {container.short_id} (process {process_id}). Stopping...")
                        try:
                            container.stop(timeout=5)
                            container.remove(force=True)
                            logger.info(f"OrphanContainerMonitor: Removed orphan {container.short_id}")
                        except docker.errors.NotFound:
                            pass
                        except Exception as e:
                            logger.error(f"Failed to remove orphan {container.short_id}: {e}")
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
                                            logger.error(f"OrphanContainerMonitor: App {process_id} health check returned {resp.status_code}")
                                            with active_apps_lock:
                                                active_apps[process_id]['status'] = 'failed'
                                            redis_client.hset(_redis_repo_key(process_id), "status", "failed")
                                    except Exception as req_err:
                                        logger.error(f"OrphanContainerMonitor: App {process_id} health check failed (Connection Error): {req_err}")
                                        # Mark as failed in Redis and memory so user sees the error
                                        with active_apps_lock:
                                            active_apps[process_id]['status'] = 'failed'
                                        redis_client.hset(_redis_repo_key(process_id), "status", "failed")
                        except Exception as h_err:
                            logger.error(f"OrphanContainerMonitor: Health check logic error: {h_err}")
            except Exception as e:
                logger.error(f"OrphanContainerMonitor: Error processing container {container.short_id}: {e}")

def cleanup_stale_containers():
    try:
        # Reset only very old statuses in the database to 'stopped' on startup
        try:
            with app.app_context():
                db = get_db()
                # 90 hours in seconds
                ninety_hours_ago = (datetime.datetime.now() - datetime.timedelta(hours=90)).strftime('%Y-%m-%d %H:%M:%S')
                db.execute("UPDATE repo_history SET status = 'stopped' WHERE status IN ('running', 'starting', 'created') AND created_at < ?", (ninety_hours_ago,))
                db.commit()
                logging.info(f"Database status for repo_history reset for apps older than {ninety_hours_ago}.")
        except Exception as db_err:
            logger.exception("Error caught: %s", db_err)
            logging.error(f"Failed to reset database statuses: {db_err}")

        client = docker.from_env()
        # Clean up by label first
        stale_labeled = client.containers.list(all=True, filters={"label": "stellar_type"})

        # Also clean up by name pattern for backward compatibility
        stale_named = client.containers.list(all=True, filters={'name': 'stellar-sandbox-*'})
        
        all_stale = list(set(stale_labeled + stale_named))

        if not all_stale:
            logging.info("No stale sandbox containers found on startup.")
            return

        logging.warning(f"Found {len(all_stale)} stale sandbox container(s). Checking creation times...")
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
                            logging.info(f"Skipping recently created orphan (within 90h): {container.name}")
                            continue
                    except ValueError:
                        pass

                logging.warning(f"Force-removing stale container: {container.name} ({container.short_id})")
                container.remove(force=True) 
            except docker.errors.NotFound:
                logging.info(f"Container {container.name} was already removed.")
            except Exception as e:
                logger.error(f"Error during cleanup of container {container.name}: {e}")
        logging.info("Stale container cleanup complete.")

    except docker.errors.DockerException as e:
        logging.error(f"Docker is not available. Skipping stale container cleanup. Error: {e}")
    except Exception as e:
        logger.exception("Error caught: %s", e)
        logging.error(f"An unexpected error occurred during stale container cleanup: {e}")

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
    # Don't intercept sentinel API calls — they must reach the main app
    if request.path.startswith('/api/sentinel/'):
        return None

    host = request.headers.get('Host', '')
    domain_parts = host.split(':')[0].split('.')

    # Catch any request to *.stellarai.live (excluding www and the main root domain)
    if len(domain_parts) >= 3 and domain_parts[-2] == 'stellarai' and domain_parts[-1] == 'live' and domain_parts[0] != 'www':
        subdomain = domain_parts[0]

        db = get_db()
        cursor = db.execute("SELECT process_id, subdomain, user_id FROM repo_history WHERE subdomain = ? ORDER BY id DESC LIMIT 1", (subdomain,))
        row = cursor.fetchone()

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
        try:
            logger.debug(f"Proxying request for {subdomain} ({process_id}) to port {target_port}")
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
                log_backend_crash(process_id, f"HTTP Server Error {resp.status_code}",
                    f"HTTP STATUS {resp.status_code}\n\nCONTAINER LOGS:\n{container_logs}\n\nHTTP RESPONSE:\n{body_snippet}")

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
            log_backend_crash(process_id, f"Connection Failure: {str(e)}",
                f"PROXY ERROR: {str(e)}\n\nCONTAINER LOGS:\n{container_logs}")

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
            telegram_bot.send_message(f"🛠️ {current_username} resumed repo session: {project_name}")
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
    def __init__(self, app_instance, interval=60):
        self.app_instance = app_instance
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            try:
                self._check_tasks()
            except Exception as e:
                logger.error(f"Error in TaskSchedulerMonitor: {e}")
            time.sleep(self.interval)

    def _check_tasks(self):
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
                    except Exception as e:
                        logger.error(f"Task Execution Failed (ID {t['id']}): {e}")
                        with self.app_instance.app_context():
                            db = get_db()
                            db.execute("UPDATE scheduled_tasks SET status = 'failed', lock_id = NULL WHERE id = ?", (t['id'],))
                            db.commit()

                threading.Thread(target=run_task_wrapper, args=(task,), daemon=True).start()

    def _execute_ai_task(self, task_id, user_id, chat_id, task_prompt, model_id, metadata):
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
