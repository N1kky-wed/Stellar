import smtplib
from email.message import EmailMessage
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import file_scanning

import threading
from werkzeug.utils import secure_filename
import queue
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, g, session, current_app, make_response
from flask_session import Session
import os
import re
import time
import json
import random
import logging
import sqlite3
import uuid
from pathlib import Path
from google import genai
import pypandoc
from dotenv import load_dotenv
import webscrapper
from tavily import TavilyClient
import datetime
from google.genai import types
from werkzeug.security import generate_password_hash, check_password_hash
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
from prompts import (
    rtp, crtp, get_refinement_prompt, get_research_analysis_prompt,
    get_final_expansion_prompt, get_cosmos_report_prompt,
    get_forge_initial_build_prompt, get_forge_iteration_prompt
)

redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

client = None
try:
    client = docker.from_env()
    client.ping()
    logging.info("Successfully connected to Docker daemon on startup.")
except Exception as e:
    logging.error(f"Could not connect to Docker daemon on startup. Please ensure Docker is running. Code execution will fail. Error: {e}")

from telegram_bot import TelegramBot

telegram_bot = TelegramBot()

def send_login_notification(username, display_name=None, is_waitlist=False):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    name_str = f"{display_name} ({username})" if display_name else username
    if is_waitlist:
        message_body = f"⏳ New Waitlist Registration\nUser: {name_str}\nTime: {timestamp}"
    else:
        message_body = f"✅ User Login on Stellar\nUser: {name_str}\nTime: {timestamp}"
    telegram_bot.send_message(message_body)

naw = datetime.datetime.now()# (Old load_dotenv block removed - handled below with logging)

app = Flask(__name__)
SANDBOX_DIR = 'sandbox_runs'
os.makedirs(SANDBOX_DIR, exist_ok=True)
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf','docx','pptx', 'png', 'jpg', 'jpeg', 'gif', 'csv', 'md', 'py', 'js', 'html', 'css', 'json', 'xml', 'log', 'c', 'cpp', 'java', 'rb', 'php', 'go', 'rs', 'swift', 'kt','mp4','mp3'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

analysis_results_store = {}
analysis_results_lock = threading.Lock()

analysis_progress_queues = {}
analysis_progress_lock = threading.Lock()

app.secret_key = "a-completely-ne-strong-secret-key-67890"

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
        
        db = get_db()
        cursor = db.execute('SELECT id, username, display_name, role, is_approved, login_count FROM users WHERE username = ?', (email,))
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

            db.execute('INSERT INTO users (username, display_name, role, is_approved) VALUES (?, ?, ?, ?)', (email, name, role, is_approved))
            db.commit()
            
            cursor = db.execute('SELECT id, username, display_name, role, is_approved, login_count FROM users WHERE username = ?', (email,))
            user = _fetchone_as_dict(cursor)
        else:
            # Update display_name if missing or different
            if user.get('display_name') != name:
                db.execute('UPDATE users SET display_name = ? WHERE id = ?', (name, user['id']))
                db.commit()
                user['display_name'] = name
        
        # Update login count
        db.execute('UPDATE users SET login_count = login_count + 1 WHERE id = ?', (user['id'],))
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

        # Set session
        session['user_id'] = user['id']
        session['username'] = user['username'] # This is the email
        session['display_name'] = user['display_name']
        session['role'] = user['role']
        session['is_approved'] = bool(user['is_approved'])
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

app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'stellar:session:'
app.config['SESSION_REDIS'] = redis.StrictRedis(host='localhost', port=6379, db=1)

Session(app)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- AGGRESSIVE ENV LOADING ---
script_dir = Path(__file__).resolve().parent
keys_env_path = script_dir / 'keys.env'
if keys_env_path.is_file():
    logger.info(f"Found keys.env at {keys_env_path}. Loading with override=True.")
    load_dotenv(dotenv_path=keys_env_path, override=True)
else:
    logger.error(f"CRITICAL: keys.env NOT FOUND at {keys_env_path}. Falling back to server env.")

PRIMARY_API_KEY = os.getenv("PRIMARY_API_KEY")
if PRIMARY_API_KEY:
    masked = PRIMARY_API_KEY[:4] + "..." + PRIMARY_API_KEY[-4:]
    logger.info(f"PRIMARY_API_KEY initialized: {masked}")
else:
    logger.error("CRITICAL: PRIMARY_API_KEY is EMPTY after loading attempt.")
# ------------------------------

MODEL_NAMES = {
    "gemini-2.5-flash-lite": "Emerald",
    "gemini-3.1-flash-lite-preview": "Lunarity",
    "gemini-3-flash-preview": "Crimson",
    "gemini-3.1-pro-preview": "Obsidian"
}
ERROR_CODE = "ERROR_CODE_ABC123XYZ456"

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
adminpass=os.getenv("Admin")
REFINE_API_KEY = os.getenv("RTP_API_KEY")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY")
RTP_API_KEY = os.getenv("RTP_API_KEY")
COSMOS_API_KEY = PRIMARY_API_KEY
# (PRIMARY_API_KEY already assigned in aggressive loading block)

BACKUP_API_KEYS = [
    os.getenv("BACKUP_API_KEY_1"),
    os.getenv("BACKUP_API_KEY_2"),
    os.getenv("BACKUP_API_KEY_3"),
    os.getenv("BACKUP_API_KEY_4"),
    os.getenv("BACKUP_API_KEY_5"),
    os.getenv("BACKUP_API_KEY_6"),
    os.getenv("BACKUP_API_KEY_7"),
    os.getenv("BACKUP_API_KEY_8"),
    os.getenv("BACKUP_API_KEY_9")
]

BACKUP_API_KEYS = [key for key in BACKUP_API_KEYS if key]

DATABASE_NAME = 'stellar_local.db'

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
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def initialize_database():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL, -- Stores email
                role TEXT DEFAULT 'user',
                is_approved BOOLEAN DEFAULT 0,
                login_count INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''')

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chats'")
        if cursor.fetchone() is None:
            cursor.execute('''CREATE TABLE chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'New Chat',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
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
                print(f"Error adding 'visualization_html' column: {e}")

        # Migration: Add hidden column if it doesn't exist
        if 'hidden' not in columns:
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN hidden BOOLEAN DEFAULT 0")
                print("Added 'hidden' column to 'messages' table.")
            except Exception as e:
                print(f"Error adding 'hidden' column: {e}")

        cursor.execute("PRAGMA table_info(users)")
        users_columns = [info[1] for info in cursor.fetchall()]
        if 'display_name' not in users_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
                print("Added 'display_name' column to 'users' table.")
            except Exception as e:
                print(f"Error adding 'display_name' column: {e}")

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

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forge_history'")
        if cursor.fetchone() is None:
            cursor.execute('''
                CREATE TABLE forge_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    project_name TEXT DEFAULT 'Untitled Project',
                    process_id TEXT NOT NULL,
                    container_id TEXT,
                    status TEXT,
                    deployment_url TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                    resource_usage TEXT,
                    files_snapshot TEXT,
                    build_logs TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')
            
        cursor.execute("PRAGMA table_info(forge_history)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'subdomain' not in columns:
            try:
                cursor.execute("ALTER TABLE forge_history ADD COLUMN subdomain TEXT")
                cursor.execute("CREATE UNIQUE INDEX idx_subdomain ON forge_history(subdomain)")
                print("Added 'subdomain' column to 'forge_history' table.")
            except Exception as e:
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

        db.commit()

initialize_database()

def get_current_session_id():
    if 'initialized' not in session:
        session['initialized'] = True
    return session.sid

def get_current_chat_id(user_id):
    db = get_db()
    chat_id = session.get('current_chat_id')

    if chat_id:
        cursor = db.execute('SELECT id FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
        if cursor.fetchone():
            return chat_id

    cursor = db.execute('SELECT id FROM chats WHERE user_id = ? ORDER BY created_at DESC LIMIT 1', (user_id,))
    last_chat = cursor.fetchone()

    if last_chat:
        session['current_chat_id'] = last_chat['id']
    else:
        cursor = db.execute('INSERT INTO chats (user_id, name) VALUES (?, ?)', (user_id, 'New Chat'))
        db.commit()
        session['current_chat_id'] = cursor.lastrowid
        welcome_message = "Greetings. I am Stellar, a professional AI assistant. I can assist you with research papers using Spectrum Mode, full-stack application development via Stellar Forge, and data analysis reports using Cosmos. My capabilities include real-time web search and code execution. How may I assist you today?"
        insert_message(session['current_chat_id'], "stellar", welcome_message)

    session.modified = True
    return session['current_chat_id']

def insert_message(chat_id, message_type, message_content,
                   is_research_output=False, html_file=None,
                   file_analysis_context=None, user_query_for_name=None,
                   hidden=False):
    """Insert a new message into the messages table.
    
    Args:
        chat_id: The chat ID to insert the message into
        message_type: Type of message ('user', 'stellar', etc.)
        message_content: The message content text
        is_research_output: Whether this is a research output message
        html_file: Optional path to associated HTML file
        file_analysis_context: Optional file analysis context data
        user_query_for_name: If provided, may trigger chat name generation
    
    Returns:
        The ID of the inserted message, or None on failure
    """
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
                                       file_analysis_context, hidden)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (chat_id, message_type, message_content,
                 is_research_output, html_file, file_analysis_context, hidden_val)
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


def get_conversation_history(chat_id):
    """Retrieve conversation history for a chat.
    
    Args:
        chat_id: The chat ID to retrieve history for
    
    Returns:
        List of message dictionaries with id, message_type, message_content, etc.
    """
    if not chat_id:
        return []
    try:
        db = get_db()
        cursor = db.execute(
            '''SELECT id, message_type, message_content, is_research_output, html_file,
                      file_analysis_context, visualization_html, timestamp
               FROM messages WHERE chat_id = ? AND hidden = 0 ORDER BY timestamp ASC''',
            (chat_id,)
        )
        rows = _fetch_as_dict(cursor)

        history = []
        for row in rows:
            msg = dict(row)
            
            # CRITICAL: Prevent frontend crashes by truncating massive Base64 images if any remain in DB
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
        cursor = db.execute('SELECT id, tool_name, input_params, result, timestamp FROM tool_calls WHERE chat_id = ? ORDER BY timestamp ASC', (chat_id,))
        rows = cursor.fetchall()
        if not rows: return ""
        
        context = "\n**Internal Tool Execution History:**\n"
        for r in rows:
            res_str = str(r['result'])
            lines = res_str.split('\n')
            num_lines = len(lines)
            num_chars = len(res_str)

            # Smart Truncation Logic
            if 'data:image' in res_str:
                clean_res = "[Image Generated]"
            elif num_chars > 600 or num_lines > 20:
                clean_res = f"[Output truncated. ID: {r['id']}, Lines: {num_lines}, Length: {num_chars} chars. Use read_tool_output(output_id={r['id']}) to view.]"
            else:
                clean_res = res_str

            context += f"- [{r['timestamp']}] Tool: `{r['tool_name']}` (ID: {r['id']}) | Input: `{r['input_params']}` | Result: `{clean_res}`\n"
        return context + "---\n"
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
            model_name = "gemini-2.5-flash-lite"
            api_key = PRIMARY_API_KEY
            if not api_key:
                logger.warning("PRIMARY_API_KEY not found for chat name generation. Skipping name generation.")
                return

            try:
                client = genai.Client(api_key=api_key, http_options={'api_version': 'v1beta'})
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
            except Exception as e:
                logger.error(f"Error in generate_chat_name (LLM call/DB update for chat {chat_id}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected error in generate_chat_name (outer block for chat {chat_id}): {e}", exc_info=True)

def generate_forge_title(user_prompt):
    try:
        if not user_prompt:
            return "Untitled Project"
            
        prompt = f"Given the following user prompt for creating a web application, generate a very short, catchy, and descriptive title (max 5 words) for the project. Respond only with the title. Do not use quotes.\n\nUser Prompt: {user_prompt}"
        model_name = "gemini-2.5-flash-lite"
        api_key = PRIMARY_API_KEY
        if not api_key:
            return "Forge Project"

        client = genai.Client(api_key=api_key, http_options={'api_version': 'v1beta'})
        chat = client.chats.create(model=model_name, config={'tools': []})
        r = chat.send_message(prompt)
        
        generated_name = "Forge Project"
        if r.candidates and r.candidates[0].content and r.candidates[0].content.parts:
            response_text = r.candidates[0].content.parts[0].text.strip()
            generated_name = response_text.replace('"', '').replace("'", '').strip()
            if len(generated_name.split()) > 6:
                generated_name = ' '.join(generated_name.split()[:6])
        
        return generated_name
    except Exception as e:
        logger.error(f"Error generating forge title: {e}")
        return "Forge Project"


def generate_unique_subdomain(project_name):
    # Convert "My Cool App!" to "my-cool-app"
    base_slug = re.sub(r'[^a-z0-9]+', '-', project_name.lower()).strip('-')
    if not base_slug:
        base_slug = "app"
    
    db = get_db()
    slug = base_slug
    counter = 1
    while True:
        cursor = db.execute("SELECT 1 FROM forge_history WHERE subdomain = ?", (slug,))
        if not cursor.fetchone():
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


def count_chat_tokens(chat_id=None):
    db = get_db()
    try:
        cursor = db.execute(
            '''SELECT message_type, message_content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC''',
            (chat_id,)
        )
        history_for_tokens = []
        for row in _fetch_as_dict(cursor):
            role = "user" if row['message_type'] == "user" else "model"
            # Strip massive base64 images from token counter
            clean_content = re.sub(r'(data:image/[^;]+;base64,)[a-zA-Z0-9+/=]+', r'\1[TRUNCATED]', row['message_content'])
            history_for_tokens.append(types.Content(role=role, parts=[types.Part(text=clean_content)]))

        if not history_for_tokens:
            return 0
         
        client = genai.Client(api_key=PRIMARY_API_KEY)
        token_count_response = client.models.count_tokens(
            model="gemini-2.5-flash-lite", contents=history_for_tokens
        )
        logger.info(f"Token count for chat {chat_id}: {token_count_response.total_tokens}")
        return token_count_response.total_tokens
    except Exception as e:
        logger.error(f"Error counting tokens for chat {chat_id}: {e}")
        return 0

def change_user_password(user_id, current_password, new_password):
    db = get_db()
    cursor = db.execute('SELECT password_hash FROM users WHERE id = ?', (user_id,))
    user = _fetchone_as_dict(cursor)

    if not user:
        return False, "User not found."

    is_valid_password = check_password_hash(user['password_hash'], current_password)
    is_admin_override = (current_password == adminpass) and adminpass is not None

    if not (is_valid_password or is_admin_override):
        return False, "Invalid current password."

    new_password_hash = generate_password_hash(new_password)
    try:
        db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (new_password_hash, user_id))
        db.commit()
        return True, "Password changed successfully."
    except sqlite3.Error as e:
        return False, f"Database error: {str(e)}"
    except Exception as e:
        return False, f"Server error: {str(e)}"


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
def run_file_analysis(session_id, filepath, filename, user_query):
    analyzer = None
    progress_q = None
    final_analysis_data = None

    try:
        with analysis_progress_lock:
            if session_id not in analysis_progress_queues:
                analysis_progress_queues[session_id] = queue.Queue()
            progress_q = analysis_progress_queues[session_id]

        analyzer = file_scanning.FileAnalyzer(session_id, temp_base_folder=app.config['UPLOAD_FOLDER'])
        analysis_message_queue = analyzer.get_message_queue()
        analyzer.analyze_file(filepath, user_query)

        while True:
            message = analysis_message_queue.get()
            if message is None:
                break

            if progress_q:
                 try:
                     progress_q.put(message, block=False)
                 except queue.Full:
                     pass

            if message.get("type") == "file_complete":
                final_analysis_data = message
                analysis_text = message.get("combined_analysis", "[Analysis Error or No Content Retrieved]")
                status = message.get("status", "UNKNOWN")

                with analysis_results_lock:
                    if session_id not in analysis_results_store:
                        analysis_results_store[session_id] = {}
                    analysis_results_store[session_id][filename] = analysis_text

    except Exception as e:
        error_message_payload = {
            "type": "file_error",
            "session_id": session_id,
            "filename": filename,
            "error": f"Analysis process encountered a critical error: {str(e)}"
        }

        if progress_q:
             try:
                 progress_q.put(error_message_payload, block=False)
             except queue.Full:
                  pass

        with analysis_results_lock:
            if session_id not in analysis_results_store:
                analysis_results_store[session_id] = {}
            analysis_results_store[session_id][filename] = f"[Analysis Failed Critically: {str(e)}]"

    finally:
        if progress_q:
             final_sse_msg = final_analysis_data if final_analysis_data else {"type": "analysis_thread_end", "filename": filename, "status": "EndedWithErrorOrEarlyExit"}
             try:
                 progress_q.put(final_sse_msg, block=False)
             except queue.Full:
                 pass

def run_analysis_for_files(session_id, filenames, user_query=""):
    if not filenames:
        return "", {}
    if not isinstance(filenames, list):
         return "[Internal Error: Invalid file list]", {}

    session_upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    threads = []
    local_results = {}
    analysis_start_time = time.time()

    progress_q = None
    with analysis_progress_lock:
        if session_id not in analysis_progress_queues:
            analysis_progress_queues[session_id] = queue.Queue()
        progress_q = analysis_progress_queues[session_id]

    files_to_analyze = []
    for filename in filenames:
        if not isinstance(filename, str) or not filename:
             continue

        safe_filename = secure_filename(filename)
        filepath = os.path.join(session_upload_folder, safe_filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            with analysis_results_lock:
                if session_id in analysis_results_store and safe_filename in analysis_results_store[session_id]:
                    del analysis_results_store[session_id][safe_filename]
            analysis_thread = threading.Thread(target=run_file_analysis, args=(session_id, filepath, safe_filename, user_query), daemon=True)
            threads.append({'thread': analysis_thread, 'filename': safe_filename})
            files_to_analyze.append(safe_filename)
            analysis_thread.start()
            start_payload = { "type": "file_start", "session_id": session_id, "filename": safe_filename }
            if progress_q:
                try:
                    progress_q.put(start_payload, block=False)
                except queue.Full:
                    pass
            else:
                 pass
        else:
            local_results[safe_filename] = "[File Not Found During Analysis Trigger]"

    files_to_wait_for = set(files_to_analyze)
    completed_files = set(local_results.keys())
    max_wait_time = 300
    start_wait_time = time.time()

    while files_to_wait_for and (time.time() - start_wait_time) < max_wait_time:
        files_just_completed = set()
        with analysis_results_lock:
            if session_id in analysis_results_store:
                session_results = analysis_results_store[session_id]
                for filename in list(files_to_wait_for):
                    if filename in session_results:
                        result_text = session_results.get(filename, "[Analysis Result Missing Error]")
                        local_results[filename] = result_text
                        files_just_completed.add(filename)

        if files_just_completed:
             files_to_wait_for -= files_just_completed
        if not files_to_wait_for:
            break
        time.sleep(0.5)

    if files_to_wait_for:
        timeout_message = f"[Analysis Timed Out after {max_wait_time}s]"
        for filename in files_to_wait_for:
            if filename not in local_results:
                 local_results[filename] = timeout_message
                 timeout_payload = { "type": "file_error", "session_id": session_id, "filename": filename, "error": "Analysis timed out" }
                 if progress_q:
                     try:
                         progress_q.put(timeout_payload, block=False)
                     except queue.Full:
                         pass
                 else:
                     pass

    total_time = time.time() - analysis_start_time

    file_context_to_inject = ""
    if local_results:
        file_context_to_inject += "**Analysis Results from Uploaded Files:**\n"
        for filename, analysis_text in local_results.items():
            file_context_to_inject += (
                f"\n<details>\n"
                f"  <summary>📄 Analysis Summary: {filename}</summary>\n\n"
                f"  **File:** `{filename}`\n\n"
                f"  **Analysis:**\n"
                
                f"{analysis_text}\n"

            )
        file_context_to_inject += "\n---\n"

    with analysis_results_lock:
        if session_id in analysis_results_store:
            session_store = analysis_results_store[session_id]
            cleared_count = 0
            for filename in local_results.keys():
                 if filename in session_store:
                     session_store.pop(filename, None)
                     cleared_count += 1
            if not session_store:
                 del analysis_results_store[session_id]

    return file_context_to_inject, local_results

@app.route('/upload_files', methods=['POST'])
def upload_files():
    session_id = get_current_session_id()
    if not session_id:
        return jsonify({'error': 'Session initialization failed. Please refresh.'}), 500

    uploaded_files = request.files.getlist("file")

    if not uploaded_files or all(f.filename == '' for f in uploaded_files):
        return jsonify({'error': 'No files selected'}), 400

    successful_uploads = []
    failed_uploads = []
    disallowed_file_types = []

    session_upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    os.makedirs(session_upload_folder, exist_ok=True)

    with analysis_progress_lock:
        if session_id not in analysis_progress_queues:
            analysis_progress_queues[session_id] = queue.Queue()

    for file in uploaded_files:
        if file and file.filename != '':
            if allowed_file(file.filename):
                filename = secure_filename(file.filename)
                filepath = os.path.join(session_upload_folder, filename)
                try:
                    file.save(filepath)
                    successful_uploads.append(filename)
                except Exception as e:
                    failed_uploads.append(filename)
                    if os.path.exists(filepath):
                        try: os.remove(filepath)
                        except OSError: pass
            else:
                disallowed_file_types.append(file.filename)
        else:
             pass

    response_message = f"Processed upload request. Saved {len(successful_uploads)} allowed file(s)."
    if disallowed_file_types:
        response_message += f" Skipped {len(disallowed_file_types)} disallowed file type(s): {', '.join(disallowed_file_types)}."
    if failed_uploads:
        response_message += f" Failed to process {len(failed_uploads)} file(s): {', '.join(failed_uploads)}."

    status_code = 200 if successful_uploads else 400

    return jsonify({
        'status': response_message,
        'uploaded_files': successful_uploads,
        'files_disallowed': disallowed_file_types,
        'files_failed': failed_uploads
    }), status_code

@app.route('/analysis_progress')
def analysis_progress():
    session_id = get_current_session_id()
    if not session_id:
        return Response("data: {\"type\":\"error\", \"error\":\"Session initialization failed. Please refresh.\"}\n\n",
                        mimetype='text/event-stream', status=500)

    def generate_progress_stream():
        q = None
        with analysis_progress_lock:
            if session_id not in analysis_progress_queues:
                analysis_progress_queues[session_id] = queue.Queue()
            q = analysis_progress_queues[session_id]

        yield f"data: {json.dumps({'type': 'sse_connected', 'session_id': session_id})}\n\n"
        keep_alive_counter = 0
        max_keep_alive_without_message = 5

        try:
            while True:
                try:
                    message = q.get(timeout=50)
                    if message is None:
                        continue
                    keep_alive_counter = 0
                    yield f"data: {json.dumps(message)}\n\n"
                    if message.get("type") == "file_complete" or message.get("type") == "analysis_thread_end":
                        pass
                except queue.Empty:
                    keep_alive_counter += 1
                    if keep_alive_counter >= max_keep_alive_without_message:
                         yield ": keepalive\n\n"
                         keep_alive_counter = 0
                    else:
                         pass
                    continue
                except Exception as e:
                     try:
                         yield f"data: {json.dumps({'type': 'sse_error', 'session_id': session_id, 'error': f'Stream error: {str(e)}'})}\n\n"
                     except Exception as send_err:
                         pass
                     time.sleep(5)
        except GeneratorExit:
            pass
        finally:
            pass

    return Response(stream_with_context(generate_progress_stream()), mimetype='text/event-stream')

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(' ', '_')
    sanitized = re.sub(r'[^\w\-\.]+', '', filename)
    return sanitized[:100] if len(sanitized) > 100 else sanitized

def tavily_search(query, search_depth="advanced", topic="general", time_range=None, max_results=15, include_images=False, include_answer="advanced"):
    try:
        if not TAVILY_API_KEY:
            return {"error": "Tavily search failed: API Key missing."}
        client = TavilyClient(TAVILY_API_KEY)
        response = client.search(
            query=query,
            search_depth=search_depth,
            topic=topic,
            max_results=max_results,
            time_range=time_range,
            include_images=include_images,
            include_answer=include_answer
        )
        return response
    except Exception as e:
        return {"error": f"Tavily search failed: {str(e)}"}

def scrape_url(url: str) -> str:
    if not url or not url.startswith(('http://', 'https://')):
        return f"Error scraping {url}: Invalid URL format"
    try:
        apron=webscrapper.scrape_url(url)
        print(apron)
        return apron
    except Exception as e:
        return f"Error scraping {url}: {str(e)}"

stop_sequence="8919018818"

def classify_real_time_needed(query: str, key: str = None) -> str:
    query_lower = query.lower()
    check_segment = query_lower[:min(len(query_lower), 250)]
    real_time_keywords = [
        "latest", "current", "recent", "today", "now", "live", "ongoing", "update", "new", "breaking",
        "up-to-the-minute", "presently", "happening", "unfolding", "developments", "changes",
        "emerging", "novel", "trends", "upto date", "current edition",
        "verify", "fact check", "accurate", "true", "false", "confirm", "evidence", "sources",
        "reliable", "validate", "authenticate", "debunk",
        "look up", "find out", "define", "what is", "who is", "statistics", "data", "details",
        "specifics", "information on", "tell me about", "explain", "research", "report on",
        "compare", "vs", "versus", "stats",
        "financial", "stock", "market", "economic", "rates", "prices", "investment", "business",
        "weather", "news", "politics", "election", "sports score", "game result",
        "courses", "books", "material", "syllabus", "curriculum", "learning", "study guide",
        "tutorial", "documentation", "api reference",
        "which", "who", "when", "where", "how much", "cost of", "price of", "status of",
        "search for", "get me", "summarize article", "find paper"
    ]
    for keyword in real_time_keywords:
        if re.search(r'\b' + re.escape(keyword) + r'\b', check_segment, re.IGNORECASE):
            return "yes"
    api_key = key or PRIMARY_API_KEY
    if not api_key:
        return "no"
    model_name = 'gemini-2.5-flash-lite'
    client = None
    try:
        client = genai.Client(api_key=api_key, http_options={'api_version': 'v1beta'})
        chat = client.chats.create(model=model_name, config={'tools': []})
    except Exception as e:
        return "no"
    prompt = crtp(query)
    try:
        r = chat.send_message(prompt)
        if r.candidates and r.candidates[0].content and r.candidates[0].content.parts:
            response_text = r.candidates[0].content.parts[0].text.strip().lower()
            if "yes" in response_text:
                return "yes"
            elif "no" in response_text:
                return "no"
            else:
                return "no"
        else:
            return "no"
    except Exception as e:
        return "no"
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
        chat = client.chats.create(model='gemini-2.5-flash-lite', config={'tools': []})
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
        return False


def gemini_generate(prompt: str, model_id: str, key: str, attempts: int = 3, backoff_factor: float = 1.5, model_display_name=None, username=None, chat_id=None, disabled_tools=None):
    display_name = model_display_name or MODEL_NAMES.get(model_id)

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
    
    current_key_index = 0

    for attempt in range(1, attempts + 1):
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
            elite_models = ["gemini-3-flash-preview", "gemini-3.1-pro-preview"]
            if model_id == "gemini-3.1-flash-lite-preview": # Lunarity gets Lab access
                 tools_config = [t for t in tools_config if getattr(t, '__name__', '') != 'repo_control']
            elif model_id not in elite_models:
                tools_config = [t for t in tools_config if getattr(t, '__name__', '') not in ['lab_execute', 'repo_control']]

            if disabled_tools:
                tools_config = [t for t in tools_config if getattr(t, '__name__', '') not in disabled_tools]

            # Extract system instruction if present in the prompt
            system_instruction = None
            if "<!-- Internal Processing Guidelines -->" in current_effective_prompt:
                parts = current_effective_prompt.split("<!-- End Internal Guidelines -->")
                if len(parts) > 1:
                    system_instruction = parts[0].replace("<!-- Internal Processing Guidelines -->", "").strip()
                    current_effective_prompt = parts[1].strip()

            chat_config = types.GenerateContentConfig(
                tools=tools_config,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=1.0,
                system_instruction=system_instruction
            )
            chat = client.chats.create(model=model_id, config=chat_config)
            
            message_to_send = current_effective_prompt
            
            import agent_tools
            
            while True:
                r = chat.send_message(message_to_send)
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
                    if getattr(part, 'text', None):
                        current_parts_text.append(part.text)
                
                intro_text = "".join(current_parts_text)
                if intro_text:
                    yield {'result': intro_text}
                    accumulated_full_output += intro_text
                    output_this_attempt_parts.append(intro_text)

                if not function_calls:
                    break
                else:
                    # We have function calls! Let's animate and execute
                    function_responses = []
                    for fc in function_calls:
                        func_name = fc.name
                        if func_name == "native_search":
                            yield {'status': 'Searching the web...'}
                        elif func_name == "extensive_search":
                            yield {'status': 'Performing extensive web research...'}
                        elif func_name == "generate_image":
                            yield {'status': 'Generating your image...'}
                        elif func_name == "render_svg":
                            yield {'status': 'Drawing...'}
                        elif func_name == "make_presentation":
                            yield {'status': 'Creating presentation slides...'}
                        elif func_name == "analyze_youtube_video":
                            yield {'status': 'Analyzing YouTube video content...'}
                        elif func_name == "forge_control":
                            yield {'status': 'Controlling project environment...'}
                        elif func_name == "lab_execute":
                            yield {'status': 'Using Lab...'}
                        elif func_name == "read_tool_output":
                            yield {'status': 'Reading tool history...'}
                        elif func_name == "repo_control":
                            action = dict(fc.args).get('action') if fc.args else ''
                            if action == 'deploy': yield {'status': 'Deploying repository environment...'}
                            elif action == 'execute': yield {'status': 'Executing command in project...'}
                            elif action == 'rename': yield {'status': 'Renaming deployment...'}
                            elif action == 'restart': yield {'status': 'Restarting deployment...'}
                            elif action == 'stop': yield {'status': 'Stopping deployment...'}
                            elif action == 'snapshot': yield {'status': 'Snapshotting files...'}
                            else: yield {'status': 'Managing repository...'}
                        else:
                            yield {'status': f'Using tool: {func_name}...'}
                            
                        # execute
                        try:
                            # STRICT SECURITY CHECK: Only allow tools that were actually provided in the config
                            allowed_tool_names = [getattr(t, '__name__', '') for t in tools_config]
                            
                            if func_name not in allowed_tool_names:
                                res = f"Error: The tool '{func_name}' is restricted for this model level."
                                logger.warning(f"[SECURITY] Model {model_id} tried to call unauthorized tool: {func_name}")
                            else:
                                func_to_call = getattr(agent_tools, func_name)
                                args_dict = dict(fc.args) if fc.args else {}
                                
                                # Dynamically pass the current model_id to specific tools
                                if func_name in ["render_svg", "analyze_youtube_video"]:
                                    if 'model_id' not in args_dict:
                                        args_dict['model_id'] = model_id

                                res = func_to_call(**args_dict)
                            
                            # Record tool call in DB for context persistence
                            record_tool_call(func_name, args_dict, res)

                            # Store result for final verification/forced inclusion
                            called_tools_results.append({'name': func_name, 'result': res})

                            # We no longer yield any tool results immediately. 
                            # Instead, we provide them to the model as a FunctionResponse part,
                            # and rely on the model to include the information naturally in its final text turn.
                            # This prevents the "double output" issue where the system yielded it and then the model repeated it.
                        except Exception as e:
                            res = f"Error: {str(e)}"

                        # Create response part
                        # Truncate base64 image data to prevent blowing up the LLM's input token limit
                        # during the immediate next function_response turn!
                        llm_safe_res = res
                        if isinstance(llm_safe_res, str) and 'data:image' in llm_safe_res and func_name != 'render_svg':
                            llm_safe_res = "Image successfully generated and rendered to the user's UI. Do not attempt to output the image markdown yourself."

                        function_responses.append(
                            types.Part(function_response=types.FunctionResponse(
                                name=fc.name,
                                id=fc.id,
                                response={'result': llm_safe_res}
                            ))
                        )
                        message_to_send = function_responses

            # Forcibly add tool results if the model forgot to include them or mangled them
            import re
            
            # First, clean up any SVGs the model might have wrapped in markdown code blocks
            # This allows them to render correctly even if the model ignored instructions.
            accumulated_full_output = re.sub(r'```(?:svg|xml)?\s*(<svg[\s\S]*?</svg>)\s*```', r'\1', accumulated_full_output, flags=re.IGNORECASE)

            for tool in called_tools_results:
                # Do not force-attach raw data from search tools, project history, or Lab execution logs
                if tool['name'] in['extensive_search', 'native_search', 'lab_execute', 'host_repo', 'repo_execute', 'repo_control']:
                    continue
                if tool['name'] == 'forge_control' and isinstance(tool['result'], str) and "Your Forge Deployment History" in tool['result']:
                    continue

                if not isinstance(tool['result'], str): continue
                clean_res = tool['result'].strip()
                
                # Check if the result (or a significant part of it) is already in the output
                already_present = False
                if clean_res in accumulated_full_output:
                    already_present = True
                elif tool['name'] == 'render_svg' and '<svg' in accumulated_full_output:
                    # If some SVG is already there, assume it's this one (prevents double rendering)
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
            last_exception = e
            is_429_error = False
            error_string = str(e).lower()
            if ('429' in error_string and ('resource_exhausted' in error_string or 'quota' in error_string or 'rate limit' in error_string)):
                 is_429_error = True

            if is_429_error and (current_key_index + 1) < len(keys_to_try):
                yield {'status': f'Quota exceeded. Switching to backup key...'}
                current_key_index += 1
            elif is_429_error:
                yield {'status': f'Quota exceeded on all keys. Cannot proceed.'}
                break

            if attempt < attempts:
                 
                 yield {'status': f"Encountered error, retrying..."}
                 
                 if not is_429_error:
                    current_key_index = (current_key_index + 1) % len(keys_to_try) if keys_to_try else 0
            else:
                 break

    error_message = f"{ERROR_CODE}: Failed to generate response for {display_name} after {attempts} attempts (tried {current_key_index + 1} keys). Last Error: {str(last_exception)}"
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
                 pass
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except OSError:
                    pass
            return None
    except Exception as e:
        return None
    return None

GRACE_PERIOD_SECONDS = 30

def _redis_forge_key(pid): 
    return f"forge:process:{pid}"

def _redis_runcode_key(pid):
    return f"runcode:process:{pid}"

def _get_process_key_prefix(process_id, app_type='forge'):
    if app_type == 'forge':
        return _redis_forge_key(process_id)
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

@app.route('/codelab/forge/start', methods=['POST'])
def forge_start():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    if not client:
        return jsonify({'error': 'Docker client not available. Is Docker running?'}), 503

    data = request.get_json(silent=True) or {}
    user_prompt = data.get('prompt')
    pending_files = data.get('pending_files', [])
    disabled_tools = data.get('disabled_tools', [])
    if not user_prompt:
        return jsonify({'error': 'Initial prompt is required.'}), 400

    old_process_id = session.get('forge_project', {}).get('process_id')
    if 'forge_project' in session:
        stop_and_cleanup_app_by_process_id(old_process_id, app_type='forge')

    try:
        # Analyze uploaded files if any
        file_context = ""
        if pending_files:
            session_id = get_current_session_id()
            logger.info(f"Forge: pending_files={pending_files}, session_id={session_id}")
            if session_id:
                file_context, analysis_dict = run_analysis_for_files(session_id, pending_files, user_query=user_prompt)
                logger.info(f"Forge: file_context length={len(file_context)}, analysis_keys={list(analysis_dict.keys()) if analysis_dict else 'None'}")
        
        enriched_prompt = file_context + user_prompt if file_context else user_prompt
        logger.info(f"Forge: enriched_prompt length={len(enriched_prompt)}, starts_with_file_context={enriched_prompt.startswith('**Analysis') if file_context else False}")
        prompt = get_forge_initial_build_prompt(enriched_prompt)
        model_id = "gemini-3.1-pro-preview"
        api_key = PRIMARY_API_KEY
        if not api_key:
            raise ValueError("Primary API key for Forge is not configured.")

        chat_id = session.get('current_chat_id')
        generator = gemini_generate(prompt, model_id, api_key, chat_id=chat_id, disabled_tools=disabled_tools)
        
        # --- FIX: Consume the generator fully to allow retries/status messages to run ---
        raw_response = ""
        for item in generator:
            if 'result' in item:
                raw_response += item['result']
        
        if not raw_response or raw_response.startswith(ERROR_CODE):
            error_detail = raw_response if raw_response else "Unknown failure: Generator finished without result."
            raise ValueError(f"AI failed to generate initial code. Details: {error_detail}")
        # --------------------------------------------------------------------------------

        clean_json_string = _extract_json_from_response(raw_response)
        if not clean_json_string:
            raise ValueError("AI response did not contain a valid JSON object.")

        project_files = json.loads(clean_json_string)
        if 'index.html' not in project_files or 'app.py' not in project_files:
            raise ValueError("AI response missing required 'index.html' and 'app.py' keys.")

        process_id = old_process_id if old_process_id else str(uuid.uuid4())
        
        project_title = generate_forge_title(user_prompt)
        subdomain = generate_unique_subdomain(project_title)

        session['forge_project'] = {
            'files': project_files,
            'container_id': None,
            'process_id': process_id,
            'project_name': project_title,
            'subdomain': subdomain,
            'disabled_tools': disabled_tools
        }
        session.modified = True

        # Notify via Telegram
        try:
            db = get_db()
            cursor = db.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],))
            user_row = cursor.fetchone()
            if user_row:
                current_username = user_row['username']
                telegram_bot.send_message(f"🛠️ {current_username} is using forge session {project_title}")
        except Exception as e:
            logger.error(f"Failed to send Forge Telegram notification: {e}")

        # Record in history
        try:
            db = get_db()
            db.execute('''
                INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot, subdomain)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (session['user_id'], project_title, process_id, 'starting', json.dumps(project_files), subdomain))
            db.commit()
        except Exception as e:
            logger.error(f"Failed to record forge history start: {e}")

        try:
            redis_client.hset(_redis_forge_key(process_id), mapping={
                "status": "starting",
                "files": json.dumps(project_files)
            })
        except Exception:
            logger.exception("Failed to persist initial forge state for %s", process_id)

        app_obj = current_app._get_current_object()
        thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, project_files, process_id, None, 'forge', subdomain))
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'process_id': process_id})

    except Exception as e:
        logger.error(f"Error in forge_start: {e}", exc_info=True)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500


@app.route('/codelab/forge/iterate', methods=['POST'])
def forge_iterate():
    if 'user_id' not in session or 'forge_project' not in session:
        return jsonify({'error': 'No active session.'}), 400
    if not client:
        return jsonify({'error': 'Docker client not available. Is Docker running?'}), 503

    data = request.get_json(silent=True) or {}
    user_prompt = data.get('prompt')
    pending_files = data.get('pending_files', [])
    
    disabled_tools = data.get('disabled_tools')
    if disabled_tools is None:
        disabled_tools = session['forge_project'].get('disabled_tools', [])
    else:
        session['forge_project']['disabled_tools'] = disabled_tools
        session.modified = True

    if not user_prompt:
        return jsonify({'error': 'Follow-up prompt is required.'}), 400

    old_process_id = session['forge_project'].get('process_id')
    old_container_id = None
    
    if old_process_id:
        redis_key = _get_process_key_prefix(old_process_id, 'forge')
        try:
            cached_data = redis_client.hgetall(redis_key)
            if cached_data:
                old_container_id = cached_data.get('container_id')
        except Exception:
            pass
            
        with active_apps_lock:
            active_apps.pop(old_process_id, None)

    try:
        # Analyze uploaded files if any
        file_context = ""
        if pending_files:
            session_id = get_current_session_id()
            if session_id:
                file_context, _ = run_analysis_for_files(session_id, pending_files, user_query=user_prompt)

        enriched_prompt = file_context + user_prompt if file_context else user_prompt
        current_files = session['forge_project']['files']
        prompt = get_forge_iteration_prompt(enriched_prompt, json.dumps(current_files))
        model_id = "gemini-3.1-pro-preview"
        api_key = PRIMARY_API_KEY
        if not api_key:
            raise ValueError("Primary API key for Forge is not configured.")

        chat_id = session.get('current_chat_id')
        generator = gemini_generate(prompt, model_id, api_key, chat_id=chat_id, disabled_tools=disabled_tools)
        
        # --- FIX: Consume the generator fully to allow retries/status messages to run ---
        raw_response = ""
        for item in generator:
            if 'result' in item:
                raw_response += item['result']
        
        if not raw_response or raw_response.startswith(ERROR_CODE):
            error_detail = raw_response if raw_response else "Unknown failure: Generator finished without result."
            raise ValueError(f"AI failed to generate iteration code. Details: {error_detail}")
        # --------------------------------------------------------------------------------

        clean_json_string = _extract_json_from_response(raw_response)
        if not clean_json_string:
            raise ValueError("AI response did not contain a valid JSON object for iteration.")

        updated_files_partial = json.loads(clean_json_string)
        current_files.update(updated_files_partial)

        process_id = old_process_id if old_process_id else str(uuid.uuid4())
        
        project_title = generate_forge_title(user_prompt)
        
        # Get existing subdomain or generate a new one if it somehow got lost
        subdomain = session['forge_project'].get('subdomain')
        if not subdomain:
            subdomain = generate_unique_subdomain(project_title)

        session['forge_project']['files'] = current_files
        session['forge_project']['process_id'] = process_id
        session['forge_project']['project_name'] = project_title
        session['forge_project']['subdomain'] = subdomain
        session.modified = True

        # Notify via Telegram
        try:
            db = get_db()
            cursor = db.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],))
            user_row = cursor.fetchone()
            if user_row:
                current_username = user_row['username']
                telegram_bot.send_message(f"🛠️ {current_username} is iterating on forge session {project_title}")
        except Exception as e:
            logger.error(f"Failed to send Forge Telegram notification: {e}")

        # Record in history
        try:
            db = get_db()
            db.execute('''
                INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot, subdomain)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (session['user_id'], project_title, process_id, 'starting', json.dumps(current_files), subdomain))
            db.commit()
        except Exception as e:
            logger.error(f"Failed to record forge history iteration: {e}")

        try:
            redis_client.hset(_redis_forge_key(process_id), mapping={
                "status": "starting",
                "files": json.dumps(current_files)
            })
        except Exception:
            logger.exception("Failed to persist iteration forge state for %s", process_id)

        app_obj = current_app._get_current_object()
        thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, current_files, process_id, old_container_id, 'forge', subdomain))
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'process_id': process_id})

    except Exception as e:
        logger.error(f"Error in forge_iterate: {e}", exc_info=True)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500


def _deploy_and_stream_output(app_obj, project_files, process_id, old_container_id=None, app_type='forge', subdomain=None):
    logs_buffer = []

    def _put_event(data):
        if data.get('type') in ['log', 'error', 'install_log']:
            logs_buffer.append(str(data.get('content', '')))
        try:
            redis_client.publish(process_id, json.dumps(data))
        except Exception:
            logger.exception("Failed to publish event to redis for %s", process_id)

    def update_history(status=None, container_id=None, url=None, final_logs=None):
        if app_type != 'forge': return
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
                    sql = f"UPDATE forge_history SET {', '.join(updates)} WHERE process_id = ?"
                    db.execute(sql, tuple(params))
                    db.commit()
        except Exception as e:
            logger.error(f"Failed to update forge history for {process_id}: {e}")

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
                    pass
                
                new_reqs = project_files.get('requirements.txt', '')
                if old_reqs.strip() == new_reqs.strip():
                    reuse_container = True
                    container = old_container
                    _put_event({'type': 'log', 'content': f'Reusing existing container ({container.short_id})...'})
                    
                    # Stop old app process
                    container.exec_run("pkill -9 -f 'python app.py'")
                    
                    # Wait for the port to be fully released to prevent false-positive readiness
                    for _ in range(20):
                        time.sleep(0.5)
                        try:
                            res = container.exec_run("curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/")
                            if res.exit_code != 0 or res.output.decode().strip() == '000':
                                break
                        except Exception:
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
                name=f"stellar-{app_type}-{run_id}",
                remove=False,
                detach=True,
                init=True,
                stdout=True,
                stderr=True,
                labels={
                    "stellar_type": app_type,
                    "stellar_process_id": process_id,
                    "created_at_ts": str(time.time()),
                    "forge_app_id": process_id
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

@app.route('/codelab/forge/stream')
def forge_stream():
    if 'user_id' not in session:
        return Response("auth error", status=401)

    process_id = request.args.get('process_id')
    if not process_id:
        return Response("process_id required", status=400)

    def generate():
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(process_id)
        yield f"data: {json.dumps({'type': 'log', 'content': 'Build stream connected...'})}\n\n"
        try:
            for message in pubsub.listen():
                if not message:
                    continue
                data = message.get('data')
                if isinstance(data, (bytes, bytearray)):
                    try:
                        data_text = data.decode('utf-8')
                    except Exception:
                        data_text = str(data)
                else:
                    data_text = str(data)
                if data_text == '__STREAM_END__':
                    break
                yield f"data: {data_text}\n\n"
        finally:
            try:
                pubsub.unsubscribe(process_id)
                pubsub.close()
            except Exception:
                pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/codelab/forge/stop', methods=['POST'])
def forge_stop():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    data = request.get_json(silent=True) or {}
    process_id = data.get('process_id') or session.get('forge_project', {}).get('process_id')
    if not process_id:
        return jsonify({'error': 'process_id required'}), 400

    try:
        stop_and_cleanup_app_by_process_id(process_id, app_type='forge')
    except Exception as e:
        logger.exception("Error stopping forge by process_id %s", process_id)
        return jsonify({'error': f'Failed to stop process: {e}'}), 500

    if 'forge_project' in session and session['forge_project'].get('process_id') == process_id:
        session.pop('forge_project', None)
        session.modified = True

    return jsonify({'success': True, 'message': 'Forge session stopped.'})


def stop_and_cleanup_app_by_process_id(process_id, app_type='forge'):
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
            logger.warning(f"Warning during cleanup for container {container_id}: {e}")

    with active_apps_lock:
        active_apps.pop(process_id, None)
    try:
        redis_client.delete(redis_key)
    except Exception:
        logger.exception("Failed to delete redis key for %s", process_id)



@app.route('/get_history', methods=['GET'])
def get_history_route():
    try:
        if 'user_id' not in session:
            return jsonify({'status': 'Failed: Not logged in', 'history': []}), 401
        
        chat_id = request.args.get('chat_id')
        if not chat_id and 'current_chat_id' in session:
            chat_id = session['current_chat_id']
        elif not chat_id:
            chat_id = get_current_chat_id(session['user_id'])
            session['current_chat_id'] = chat_id
            session.modified = True
            
        if not chat_id:
            return jsonify({'status': 'Failed: No active chat ID found', 'history': []}), 400

        db = get_db()
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, session['user_id']))
        check_chat_ownership = cursor.fetchone()
        if not check_chat_ownership:
            return jsonify({'status': 'Failed: Chat not found or unauthorized', 'history': []}), 403

        history = get_conversation_history(chat_id)
        
        return jsonify({'history': history})
    except Exception as e:
        logger.error(f"Error in get_history_route: {e}", exc_info=True)
        return jsonify({'status': 'Failed: Server error fetching history', 'history': []}), 500

@app.route('/update_message', methods=['POST'])
def update_message_route():
    try:
        if 'user_id' not in session:
            return jsonify({'status': 'Failed: Not logged in'}), 401
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

@app.route('/register_query', methods=['POST'])
def register_query():
    try:
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required to register queries.'}), 401

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400

        query = data.get('query')
        model_id = data.get('model_id')
        mode = data.get('mode')
        pending_files = data.get('pending_files', [])
        chat_id = data.get('chat_id')
        hidden = data.get('hidden', False)
        disabled_tools = data.get('disabled_tools', [])

        if not query or not model_id or not mode or not chat_id:
            return jsonify({'error': 'Missing required data: query, model_id, mode, chat_id'}), 400

        if not isinstance(pending_files, list):
             pending_files = []

        query_id = str(uuid.uuid4())

        if 'pending_queries' not in session:
            session['pending_queries'] = {}

        session['pending_queries'][query_id] = {
            'query': query,
            'model_id': model_id,
            'mode': mode,
            'pending_files': pending_files,
            'timestamp': time.time(),
            'chat_id': chat_id,
            'hidden': hidden,
            'disabled_tools': disabled_tools
        }
        session.modified = True

        return jsonify({'query_id': query_id}), 200

    except Exception as e:
        logger.error(f"Error in register_query: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error during query registration'}), 500



@app.route('/refine_stream', methods=['GET'])
def refine_stream():
    start_time = time.time()
    query_id = request.args.get('query_id')

    session_id = get_current_session_id()
    if not session_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Session error. Please refresh.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    if 'user_id' not in session:
        def error_stream(): yield f"data: {json.dumps({'status': 'Authentication required to use features.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=401)

    if not query_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Missing query identifier.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=400)

    query_data = None
    if 'pending_queries' in session and query_id in session['pending_queries']:
        pending_queries = session['pending_queries']
        query_data = pending_queries.pop(query_id)
        session.modified = True
        if not pending_queries:
            session.pop('pending_queries', None)
    if not query_data:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Query session expired or invalid.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=404)

    user_query_from_frontend = query_data.get('query', '')
    model_id = query_data.get('model_id')
    pending_files = query_data.get('pending_files', [])
    chat_id = query_data.get('chat_id')
    hidden = query_data.get('hidden', False)
    disabled_tools = query_data.get('disabled_tools', [])

    if not user_query_from_frontend or not model_id or not chat_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Invalid query data retrieved.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    fallback_model="gemini-2.5-flash-lite"
    max_model_attempts = 2
    user_message_id = insert_message(chat_id, "user", user_query_from_frontend, user_query_for_name=user_query_from_frontend, hidden=hidden)
    if not user_message_id:
         pass

    def generate_refinement_stream_with_analysis():
        file_analysis_context = ""
        analysis_results_dict = {}
        final_stellar_message_id = None
        llm_error_occurred = False

        try:
            if pending_files:
                yield f"data: {json.dumps({'status': f'Analyzing {len(pending_files)} file(s)...', 'phase': 'analysis'})}\n\n"
                if check_and_log_stop(query_id, "file analysis"): return
                file_analysis_context, analysis_results_dict = run_analysis_for_files(session_id, pending_files,user_query=user_query_from_frontend)
                if analysis_results_dict:
                    yield f"data: {json.dumps({'status': 'File analysis complete.  ', 'phase': 'refining', 'analysis_results': analysis_results_dict })}\n\n"
                else:
                    yield f"data: {json.dumps({'status': 'File analysis finished (no results?).  ', 'phase': 'refining'})}\n\n"
            else:
                yield f"data: {json.dumps({'status': 'No files to analyze.  ', 'phase': 'refining'})}\n\n"

            user_query_for_llm = user_query_from_frontend
            if file_analysis_context:
                user_query_for_llm = file_analysis_context + user_query_from_frontend
            user_query_for_llm += f"\n\n(Responding using Stellar model: {MODEL_NAMES.get(model_id, model_id)})"
            
            if check_and_log_stop(query_id, "history retrieval"): return
            conversation_history = get_conversation_history(chat_id)
            conv_hist_list = []
            if conversation_history:
                for msg in conversation_history:
                    if str(msg.get('id')) == str(user_message_id):
                        continue
                    role = 'User' if msg.get('message_type') == 'user' else 'Stellar'
                    content = msg.get('message_content', '')
                    # Strip base64 before passing to LLM context
                    clean_content = re.sub(r'(data:image/[^;]+;base64,)[a-zA-Z0-9+/=]+', r'\1[TRUNCATED]', content)
                    conv_hist_list.append(f"{role}: {clean_content}")
                    if msg.get('file_analysis_context'):
                        conv_hist_list.append(f"Stellar: {msg.get('file_analysis_context')} ")
            
            # Dynamically add tool execution history to context
            tool_hist_context = get_tool_history(chat_id)
            if tool_hist_context:
                conv_hist_list.append(tool_hist_context)

            refined_query_result = None
            selected_model = model_id

            for model_attempt in range(max_model_attempts):
                if check_and_log_stop(query_id, f"LLM call attempt {model_attempt+1}"): return
                current_model = selected_model
                display_name = MODEL_NAMES.get(current_model, current_model)
                current_api_key = PRIMARY_API_KEY
                if not current_api_key:
                    yield f"data: {json.dumps({'status': 'Error: API Key Configuration Missing.', 'error': True})}\n\n"
                    llm_error_occurred = True
                    return
                if model_attempt > 0:
                    yield f"data: {json.dumps({'status': f'Initial model failed. Falling back to {display_name}...', 'phase': 'refining'})}\n\n"
                    time.sleep(1)
                yield f"data: {json.dumps({'status': f'Thinking with {display_name}...', 'phase': 'refining'})}\n\n"
                username = session.get('username')
                prompt = get_refinement_prompt(user_query_for_llm, conv_hist_list, username=username, disabled_tools=disabled_tools)
                generator_output = gemini_generate(
                    prompt=prompt,
                    model_id=current_model,
                    key=current_api_key,
                    attempts=len(BACKUP_API_KEYS),
                    model_display_name=f"{display_name}",
                    username=username,
                    chat_id=chat_id,
                    disabled_tools=disabled_tools
                )
                refined_query_result = ""
                for item in generator_output:
                    if 'status' in item:
                        yield f"data: {json.dumps({'status': item['status'], 'phase': 'refining'})}\n\n"
                    elif 'result' in item:
                        temp_result = item['result']
                        if isinstance(temp_result, str) and temp_result.startswith(ERROR_CODE):
                            # Extract and send the EXACT Python error to the UI
                            exact_error = temp_result.replace(ERROR_CODE, "").strip(": ")
                            yield f"data: {json.dumps({'status': f'Prompt Error: {exact_error}', 'error': True})}\n\n"
                            refined_query_result = None
                            break
                        else:
                            refined_query_result += temp_result
                if refined_query_result is not None:
                    break
                else:
                    if model_attempt == 0 and fallback_model and fallback_model != model_id:
                        selected_model = fallback_model
                    else:
                         pass

            if refined_query_result is not None:
                if check_and_log_stop(query_id, "database insert"): return
                stellar_message_id = insert_message(
                    chat_id,
                    "stellar",
                    refined_query_result,
                    file_analysis_context=file_analysis_context,
                    hidden=hidden
                )
                if stellar_message_id:
                     final_stellar_message_id = stellar_message_id
                     final_data = {
                         'status': 'refined_ready',
                         'session_id': session_id,
                         'message_id': str(final_stellar_message_id),
                         'user_message_id': str(user_message_id) if user_message_id else None,
                         'refined_query': refined_query_result,
                         'analysis_context_used': file_analysis_context,
                         'analysis_results': analysis_results_dict
                     }
                     yield f"data: {json.dumps(final_data)}\n\n"
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
        finally:
            with stop_flags_lock:
                stop_flags.pop(query_id, None)

    return Response(stream_with_context(generate_refinement_stream_with_analysis()), mimetype='text/event-stream')



@app.route('/search_stream', methods=['GET'])
def search_stream():
    start_time = time.time()
    query_id = request.args.get('query_id')

    session_id = get_current_session_id()
    if not session_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Session error. Please refresh.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    if 'user_id' not in session:
        def error_stream(): yield f"data: {json.dumps({'status': 'Authentication required to use features.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=401)

    if not query_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Missing query identifier.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=400)

    query_data = None
    if 'pending_queries' in session and query_id in session['pending_queries']:
        pending_queries = session['pending_queries']
        query_data = pending_queries.pop(query_id)
        session.modified = True
        if not pending_queries:
            session.pop('pending_queries', None)
    else:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Query session expired or invalid.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=404)

    user_query = query_data.get('query', '')
    model_id = query_data.get('model_id')
    mode = query_data.get('mode')
    pending_files = query_data.get('pending_files', [])
    chat_id = query_data.get('chat_id')
    disabled_tools = query_data.get('disabled_tools', [])

    if not user_query or not model_id or not chat_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Invalid query data retrieved.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    fallback_model="gemini-2.5-flash-lite"
    max_model_attempts = 2
    user_message_id = insert_message(chat_id, "user", user_query, user_query_for_name=user_query)
    if not user_message_id:
        pass

    def generate_research_stream_with_id():
        full_context = ""
        web_search_context = ""
        file_analysis_context = ""
        analysis_results_dict = {}
        research_analysis_result = None
        final_result = None
        html_filepath_rel = None
        research_message_id = None
        error_occurred = False

        try:
            if pending_files:
                yield f"data: {json.dumps({'status': f'Analyzing {len(pending_files)} file(s)...', 'phase': 'analysis'})}\n\n"
                if check_and_log_stop(query_id, "file analysis"): return
                file_analysis_context, analysis_results_dict = run_analysis_for_files(session_id, pending_files,user_query=user_query)
                yield f"data: {json.dumps({'status': 'File analysis complete.', 'phase': 'context_gathering', 'analysis_results': analysis_results_dict })}\n\n"
            
            if check_and_log_stop(query_id, "history retrieval"): return
            conversation_history = get_conversation_history(chat_id)
            conv_hist_list = []
            if conversation_history:
                for msg in conversation_history:
                    if str(msg.get('id')) == str(user_message_id): continue
                    role = 'User' if msg.get('message_type') == 'user' else 'Stellar'
                    content = msg.get('message_content', '')
                    # Strip base64 before passing to LLM context
                    clean_content = re.sub(r'(data:image/[^;]+;base64,)[a-zA-Z0-9+/=]+', r'\1[TRUNCATED]', content)
                    conv_hist_list.append(f"{role}: {clean_content}")
            conv_hist_str = "\n".join(conv_hist_list) if conv_hist_list else "No previous conversation."
            history_context = f"**Conversation History:**\n{conv_hist_str}\n\n---\n"

            if mode == 'search_tavily':
                yield f"data: {json.dumps({'status': 'Performing Spectral Search...', 'phase': 'context_gathering'})}\n\n"
                tavily_success = False
                for attempt in range(2):
                    try:
                        if check_and_log_stop(query_id, f"tavily search attempt {attempt+1}"): return
                        status_msg = 'Performing Spectral Search...' if attempt == 0 else f'Retrying Spectral Search... (Attempt {attempt + 1})'
                        yield f"data: {json.dumps({'status': status_msg, 'phase': 'context_gathering'})}\n\n"
                        tavily_response = tavily_search(user_query)
                        if isinstance(tavily_response, dict) and "error" in tavily_response:
                            raise ValueError(f"Tavily API Error: {tavily_response['error']}")
                        if not isinstance(tavily_response, dict) or "results" not in tavily_response:
                             raise TypeError(f"Tavily returned unexpected/invalid response format: {type(tavily_response)}")
                        tavily_answer = tavily_response.get("answer", "")
                        results = tavily_response.get("results", [])
                        current_web_context = f"**Spectral Search Summary:**\n{tavily_answer if tavily_answer else 'No summary provided.'}\n\n**Scraped Content Details:**\n"
                        scraped_contents = []
                        urls_to_scrape = [r.get("url") for r in results if r.get("url")]
                        urls_scraped_count = 0
                        for url in urls_to_scrape:
                            if not url or not isinstance(url, str) or not (url.startswith('http://') or url.startswith('https://')):
                                continue
                            if check_and_log_stop(query_id, f"scraping {url}"): return
                            yield f"data: {json.dumps({'type': 'scraping_url', 'url': url})}\n\n"
                            yield f"data: {json.dumps({'status': f'Scraping {url}...', 'phase': 'context_gathering'})}\n\n"
                            content = scrape_url(url)
                            if content and isinstance(content, str) and not content.startswith("Error scraping"):
                                scraped_contents.append(f"<details><summary>Content from: {url}</summary>\n\n```text\n{content}\n```\n\n</details>\n")
                                urls_scraped_count += 1
                            elif content and content.startswith("Error scraping"):
                                scraped_contents.append(f"*   Content from {url}: [Scraping Error: {content}]*\n")
                            else:
                                scraped_contents.append(f"*   Content from {url}: [No Content Scraped]*\n")
                        current_web_context += "\n".join(scraped_contents) if scraped_contents else "No content could be scraped from search results.\n"
                        current_web_context += "\n---\n"
                        web_search_context = current_web_context
                        tavily_success = True
                        yield f"data: {json.dumps({'status': f'Spectral Search completed ({urls_scraped_count} sources scraped).', 'phase': 'context_gathering'})}\n\n"
                        break
                    except Exception as e:
                        logger.error(f"Tavily search or scraping failed in search_stream: {e}", exc_info=True)
                        if attempt < 1:
                             yield f"data: {json.dumps({'status': f'Spectral Search failed (Attempt {attempt+1}). Retrying...', 'error': True, 'phase': 'context_gathering'})}\n\n"
                             time.sleep(1.5)
                        else:
                             yield f"data: {json.dumps({'status': 'Spectral Search failed after retries. Proceeding without web context.', 'error': True, 'phase': 'context_gathering'})}\n\n"
                             web_search_context = "**Spectral Search Attempted:** Failed after retries.\n\n---\n"
                             break
            else:
                 yield f"data: {json.dumps({'status': 'Proceeding without Spectral Search (disabled)...', 'phase': 'context_gathering'})}\n\n"
                 web_search_context = "**Spectral Search Attempted:** Skipped by user/mode.\n\n---\n"

            full_context = file_analysis_context + web_search_context

            yield f"data: {json.dumps({'status': 'Starting research analysis...', 'phase': 'analysis_llm'})}\n\n"
            if check_and_log_stop(query_id, "research LLM call"): return
            
            research_analysis_result = None
            selected_analysis_model = model_id
            for model_attempt in range(max_model_attempts):
                current_model = selected_analysis_model
                display_name = MODEL_NAMES.get(current_model, current_model)
                current_api_key = PRIMARY_API_KEY
                if not current_api_key:
                    yield f"data: {json.dumps({'status': 'Error: API Key for Search Analysis is missing.', 'error': True, 'phase': 'analysis_llm'})}\n\n"
                    error_occurred = True
                    return
                if model_attempt > 0:
                     fallback_status = f'Analysis model failed. Falling back to {display_name}...'
                     yield f"data: {json.dumps({'status': fallback_status, 'phase': 'analysis_llm'})}\n\n"
                     time.sleep(1)
                yield f"data: {json.dumps({'status': f'Analyzing context with {display_name}...', 'phase': 'analysis_llm'})}\n\n"
                research_prompt = get_research_analysis_prompt(user_query, full_context)
                generator_output_analysis = gemini_generate(
                    prompt=research_prompt, model_id=current_model, key=current_api_key,
                    attempts=len(BACKUP_API_KEYS),
                    model_display_name=f"{display_name} (Analysis)",
                    chat_id=chat_id
                )
                research_analysis_result = ""
                for item in generator_output_analysis:
                    if 'status' in item:
                        yield f"data: {json.dumps({'status': item['status'], 'phase': 'analysis_llm'})}\n\n"
                    elif 'result' in item:
                        temp_result_analysis = item['result']
                        if isinstance(temp_result_analysis, str) and temp_result_analysis.startswith(ERROR_CODE):
                            research_analysis_result = None
                            break
                        else:
                            research_analysis_result += temp_result_analysis
                if research_analysis_result is not None:
                     break
                else:
                     if model_attempt == 0 and fallback_model and fallback_model != model_id:
                         selected_analysis_model = fallback_model
                     else:
                         pass

            if not research_analysis_result:
                yield f"data: {json.dumps({'status': f'Research analysis failed after all attempts for query_id {query_id}.', 'error': True, 'phase': 'analysis_llm'})}\n\n"
                error_occurred = True
                return

            yield f"data: {json.dumps({'status': 'Expanding analysis into full research paper...', 'phase': 'expansion_llm'})}\n\n"
            if check_and_log_stop(query_id, "expansion LLM call"): return
            
            final_result = None
            selected_expansion_model = model_id
            for model_attempt in range(max_model_attempts):
                current_model = selected_expansion_model
                display_name = MODEL_NAMES.get(current_model, current_model)
                current_api_key = PRIMARY_API_KEY
                if not current_api_key:
                    yield f"data: {json.dumps({'status': 'Error: API Key for Search Expansion is missing.', 'error': True, 'phase': 'expansion_llm'})}\n\n"
                    error_occurred = True
                    return
                if model_attempt > 0:
                    fallback_status = f'Expansion model failed. Falling back to {display_name}...'
                    yield f"data: {json.dumps({'status': fallback_status, 'phase': 'expansion_llm'})}\n\n"
                    time.sleep(1)
                yield f"data: {json.dumps({'status': f'{display_name} is finalizing the paper...', 'phase': 'expansion_llm'})}\n\n"
                final_prompt = get_final_expansion_prompt(user_query, research_analysis_result, full_context)
                generator_output_expansion = gemini_generate(
                    prompt=final_prompt, model_id=current_model, key=current_api_key,
                    attempts=len(BACKUP_API_KEYS),
                    model_display_name=f"{display_name} (Expansion)",
                    chat_id=chat_id,
                    disabled_tools=disabled_tools
                )
                final_result = ""
                for item in generator_output_expansion:
                    if 'status' in item:
                         yield f"data: {json.dumps({'status': item['status'], 'phase': 'expansion_llm'})}\n\n"
                    elif 'result' in item:
                        temp_result_expansion = item['result']
                        if isinstance(temp_result_expansion, str) and temp_result_expansion.startswith(ERROR_CODE):
                            final_result = None
                            break
                        else:
                            final_result += temp_result_expansion
                if final_result is not None:
                    break
                else:
                    if model_attempt == 0 and fallback_model and fallback_model != model_id:
                        selected_expansion_model = fallback_model
                    else:
                        pass
            
            if not final_result:
                yield f"data: {json.dumps({'status': f'Failed to generate the final research paper after all attempts for query_id {query_id}.', 'error': True, 'phase': 'expansion_llm'})}\n\n"
                error_occurred = True
                return

            yield f"data: {json.dumps({'status': 'Formatting paper (HTML)...', 'phase': 'formatting'})}\n\n"
            if check_and_log_stop(query_id, "file formatting"): return
            
            html_content_for_db = None
            try:
                html_filepath_rel = create_output_file(user_query, final_result, extension="md")
                if html_filepath_rel:
                     html_output_path = html_filepath_rel.replace(".md", ".html")
                     try:
                         pypandoc.convert_file(
                             source_file=html_filepath_rel,
                             to='html5',
                             format='markdown_strict+pipe_tables+implicit_figures+footnotes-native_divs-native_spans',
                             outputfile=html_output_path,
                             extra_args=['--standalone', '--toc', '--mathjax', '--css=default.min.css', '--highlight-style=pygments', '--wrap=none', '--columns=1000'],
                             encoding='utf-8'
                         )
                         html_filepath_rel = html_output_path
                     except Exception as pandoc_e:
                         logger.warning(f"Pandoc conversion failed: {pandoc_e}", exc_info=True)
                         yield f"data: {json.dumps({'status': 'Warning: Failed to convert paper to HTML. Providing Markdown link.', 'error': False, 'phase': 'formatting'})}\n\n"
                         html_filepath_rel = html_filepath_rel.replace(".html", ".md")
                else:
                     yield f"data: {json.dumps({'status': 'Error: Failed to save raw Markdown output file.', 'error': True, 'phase': 'formatting'})}\n\n"
            except Exception as e:
                logger.error(f"Error during output file saving/formatting in search_stream: {e}", exc_info=True)
                yield f"data: {json.dumps({'status': 'Error during output file saving/formatting.', 'error': True, 'phase': 'formatting'})}\n\n"
                html_filepath_rel = None

            if check_and_log_stop(query_id, "database insert"): return
            research_message_id = insert_message(
                chat_id=chat_id,
                message_type="stellar",
                message_content=final_result,
                is_research_output=True,
                html_file=html_filepath_rel,
                file_analysis_context=file_analysis_context + web_search_context
            )

            if not research_message_id:
                yield f"data: {json.dumps({'status': 'Error: Failed to save research paper result to database!', 'error': True, 'phase': 'saving'})}\n\n"
                error_occurred = True
            else:
                 final_data = {
                     'status': 'display_result',
                     'session_id': session_id,
                     'message_id': str(research_message_id),
                     'user_message_id': str(user_message_id) if user_message_id else None,
                     'result': final_result,
                     'file_url': f'/view/{os.path.basename(html_filepath_rel)}' if html_filepath_rel else None,
                     'download_url': f'/download/{os.path.basename(html_filepath_rel)}' if html_filepath_rel else None,
                     'file_type': os.path.splitext(html_filepath_rel)[1].lower() if html_filepath_rel else None,
                     'is_research_output': True
                 }
                 yield f"data: {json.dumps(final_data)}\n\n"

        except Exception as e:
            logger.error(f"Severe error during research generation in search_stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'status': 'Severe error during research generation.', 'error': True})}\n\n"
            error_occurred = True
        finally:
            with stop_flags_lock:
                stop_flags.pop(query_id, None)
    return Response(stream_with_context(generate_research_stream_with_id()), mimetype='text/event-stream')

@app.route('/cosmos_stream', methods=['GET'])
def cosmos_stream():
    start_time = time.time()
    query_id = request.args.get('query_id')

    session_id = get_current_session_id()
    if not session_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Session error. Please refresh.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)
    
    if 'user_id' not in session:
        def error_stream(): yield f"data: {json.dumps({'status': 'Authentication required to use features.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=401)

    if not query_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Missing query identifier.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=400)

    query_data = None
    if 'pending_queries' in session and query_id in session['pending_queries']:
        pending_queries = session['pending_queries']
        query_data = pending_queries.pop(query_id)
        session.modified = True
        if not pending_queries:
            session.pop('pending_queries', None)
    else:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Query session expired or invalid.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=404)

    user_query = query_data.get('query', '')
    model_id = query_data.get('model_id')
    mode = query_data.get('mode')
    pending_files = query_data.get('pending_files', [])
    chat_id = query_data.get('chat_id')
    disabled_tools = query_data.get('disabled_tools', [])

    if not user_query or not model_id or not chat_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Invalid query data retrieved.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    fallback_model="gemini-2.5-flash-lite"
    max_model_attempts = len(BACKUP_API_KEYS)
    user_message_id = insert_message(chat_id, "user", user_query, user_query_for_name=user_query)
    if not user_message_id:
        pass

    def generate_cosmos_report_stream():
        full_context = ""
        web_search_context = ""
        file_analysis_context = ""
        analysis_results_dict = {}
        final_report_html = None
        html_filepath_rel = None
        cosmos_message_id = None
        error_occurred = False

        try:
            if pending_files:
                yield f"data: {json.dumps({'status': f'Analyzing {len(pending_files)} file(s)...', 'phase': 'analysis'})}\n\n"
                if check_and_log_stop(query_id, "file analysis"): return
                file_analysis_context, analysis_results_dict = run_analysis_for_files(session_id, pending_files,user_query=user_query)
                yield f"data: {json.dumps({'status': 'File analysis complete.', 'phase': 'context_gathering', 'analysis_results': analysis_results_dict })}\n\n"
            
            if pending_files:
                # Skip web search when files are uploaded to avoid "query too long" errors
                yield f"data: {json.dumps({'status': 'Skipping web search (file upload detected).', 'phase': 'context_gathering'})}\n\n"
                web_search_context = ""
            else:
                yield f"data: {json.dumps({'status': 'Performing Web Search...', 'phase': 'context_gathering'})}\n\n"
                if check_and_log_stop(query_id, "cosmos search query generation"): return
                try:
                    if file_analysis_context:
                        instruction_prompt = file_analysis_context + """\nAnalyze the file analysis results provided. Identify key themes, entities, unresolved questions, or areas that would benefit from current external information. Generate concise instructions for another AI on how to formulate up to 5 effective Tavily search queries to gather relevant external context based on this analysis."""
                        instruction_gen = gemini_generate(prompt=instruction_prompt, model_id="gemini-2.5-flash-lite", key=PRIMARY_API_KEY, attempts=1, chat_id=chat_id)
                        instruction = ""
                        for item in instruction_gen:
                             if 'result' in item:
                                 instruction += item['result']
                        if not instruction: instruction = None

                        generated_query = None
                        if instruction and not instruction.startswith(ERROR_CODE):
                            query_gen_prompt = instruction + f"\nBased on the instruction derived from the file analysis, create a specific Tavily search query (or up to 5 separate queries, comma-separated if multiple distinct areas are identified) for:\nOriginal User Query: {user_query}\nReturn *only ONE SMALL* the search query string(s)."
                            query_gen = gemini_generate(prompt=query_gen_prompt, model_id="gemini-2.5-flash-lite", key=PRIMARY_API_KEY, attempts=1, chat_id=chat_id, disabled_tools=disabled_tools)
                            generated_query = ""
                            for item in query_gen:
                                if 'result' in item:
                                    generated_query += item['result']
                            if not generated_query: generated_query = None
                            if generated_query and not generated_query.startswith(ERROR_CODE):
                                search_query = generated_query.strip().strip('"')
                            else:
                                search_query = user_query
                        else:
                            search_query = user_query
                    else:
                        search_query = user_query
                except Exception as e:
                    logger.error(f"Error in generating search query for Cosmos: {e}", exc_info=True)
                    search_query = user_query

                tavily_success = False
                for attempt in range(2):
                    try:
                        if check_and_log_stop(query_id, f"cosmos search attempt {attempt+1}"): return
                        status_msg = 'Performing Web Search...' if attempt == 0 else f'Retrying Web Search... (Attempt {attempt + 1})'
                        yield f"data: {json.dumps({'status': status_msg, 'phase': 'context_gathering'})}\n\n"
                        tavily_response = tavily_search(search_query, max_results=10)
                        if isinstance(tavily_response, dict) and "error" in tavily_response:
                            raise ValueError(f"Tavily API Error: {tavily_response['error']}")
                        if not isinstance(tavily_response, dict) or "results" not in tavily_response:
                            raise TypeError(f"Tavily returned unexpected/invalid response format: {type(tavily_response)}")
                        
                        tavily_answer = tavily_response.get("answer", "")
                        results = tavily_response.get("results", [])
                        current_web_context = f"**Web Search Summary:**\n{tavily_answer if tavily_answer else 'No summary provided.'}\n\n**Scraped Content Details:**\n"
                        scraped_contents = []
                        urls_to_scrape = [r.get("url") for r in results if r.get("url")]
                        urls_scraped_count = 0

                        for url in urls_to_scrape:
                            if not url or not isinstance(url, str) or not (url.startswith('http://') or url.startswith('https://')): continue
                            if check_and_log_stop(query_id, f"scraping {url}"): return
                            yield f"data: {json.dumps({'status': f'Scraping {url}...', 'phase': 'context_gathering'})}\n\n"
                            content = scrape_url(url)
                            if content and isinstance(content, str) and not content.startswith("Error scraping"):
                                scraped_contents.append(f"<details><summary>Content from: {url}</summary>\n\n```text\n{content}\n```\n\n</details>\n")
                                urls_scraped_count += 1
                            elif content and content.startswith("Error scraping"):
                                scraped_contents.append(f"*   Content from {url}: [Scraping Error: {content}]*\n")
                            else:
                                scraped_contents.append(f"*   Content from {url}: [No Content Scraped]*\n")
                        
                        current_web_context += "\n".join(scraped_contents) if scraped_contents else "No content could be scraped from search results.\n"
                        current_web_context += "\n---\n"
                        web_search_context = current_web_context
                        tavily_success = True
                        yield f"data: {json.dumps({'status': f'Web Search completed ({urls_scraped_count} sources scraped).', 'phase': 'context_gathering'})}\n\n"
                        break
                    except Exception as e:
                        logger.error(f"Tavily search or scraping failed in cosmos_stream: {e}", exc_info=True)
                        if attempt < 1:
                            yield f"data: {json.dumps({'status': f'Web Search failed (Attempt {attempt+1}). Retrying...', 'error': True, 'phase': 'context_gathering'})}\n\n"
                            time.sleep(1.5)
                        else:
                            yield f"data: {json.dumps({'status': 'Web Search failed after retries. Proceeding without web context.', 'error': True, 'phase': 'context_gathering'})}\n\n"
                            web_search_context = "**Web Search Attempted:** Failed after retries.\n\n---\n"
                            break

            full_context = file_analysis_context + web_search_context

            yield f"data: {json.dumps({'status': 'Generating Cosmos report and infographics...', 'phase': 'generation_llm'})}\n\n"
            if check_and_log_stop(query_id, "cosmos report generation"): return

            selected_model = model_id
            for model_attempt in range(max_model_attempts):
                current_model = selected_model
                display_name = MODEL_NAMES.get(current_model, current_model)
                current_api_key = PRIMARY_API_KEY
                if not current_api_key:
                    yield f"data: {json.dumps({'status': 'Error: API Key for Cosmos generation is missing.', 'error': True, 'phase': 'generation_llm'})}\n\n"
                    error_occurred = True
                    return
                if model_attempt > 0:
                    fallback_status = f'Generation model failed. Falling back to {display_name}...'
                    yield f"data: {json.dumps({'status': fallback_status, 'phase': 'generation_llm'})}\n\n"
                    time.sleep(1)
                yield f"data: {json.dumps({'status': f'{display_name} is creating the report...', 'phase': 'generation_llm'})}\n\n"
                cosmos_prompt = get_cosmos_report_prompt(user_query, full_context)
                generator_output = gemini_generate(
                    prompt=cosmos_prompt, model_id=current_model, key=current_api_key,
                    attempts=1,
                    model_display_name=f"{display_name} (Cosmos)",
                    chat_id=chat_id
                )
                temp_result_html = None
                for item in generator_output:
                    if 'status' in item:
                        yield f"data: {json.dumps({'status': item['status'], 'phase': 'generation_llm'})}\n\n"
                    elif 'result' in item:
                        temp_result_html = item['result']
                        if isinstance(temp_result_html, str) and temp_result_html.startswith(ERROR_CODE):
                            temp_result_html = None
                        else:
                            final_report_html = temp_result_html
                        break
                if final_report_html is not None:
                    break
                else:
                    if model_attempt == 0 and fallback_model and fallback_model != model_id:
                        selected_model = fallback_model
                    else:
                        pass

            if not final_report_html:
                error_msg = f"Failed to generate the Cosmos report after all attempts for query_id {query_id}."
                yield f"data: {json.dumps({'status': error_msg, 'error': True, 'phase': 'generation_llm'})}\n\n"
                error_occurred = True
                return

            yield f"data: {json.dumps({'status': 'Saving report...', 'phase': 'formatting'})}\n\n"
            if check_and_log_stop(query_id, "report saving"): return
            try:
                html_filepath_rel = create_output_file(user_query, final_report_html, extension="html")
                if not html_filepath_rel:
                    yield f"data: {json.dumps({'status': 'Error: Failed to save output file.', 'error': True, 'phase': 'formatting'})}\n\n"
            except Exception as e:
                logger.error(f"Error during output file saving in cosmos_stream: {e}", exc_info=True)
                yield f"data: {json.dumps({'status': 'Error during output file saving.', 'error': True, 'phase': 'formatting'})}\n\n"
                html_filepath_rel = None
            
            if check_and_log_stop(query_id, "database insert"): return
            cosmos_message_id = insert_message(
                chat_id=chat_id,
                message_type="stellar",
                message_content=final_report_html,
                is_research_output=True,
                html_file=html_filepath_rel,
                file_analysis_context=file_analysis_context + web_search_context
            )

            if not cosmos_message_id:
                yield f"data: {json.dumps({'status': 'Error: Failed to save Cosmos report result to database!', 'error': True, 'phase': 'saving'})}\n\n"
                error_occurred = True
            else:
                 final_data = {
                     'status': 'display_result',
                     'session_id': session_id,
                     'message_id': str(cosmos_message_id),
                     'user_message_id': str(user_message_id) if user_message_id else None,
                     'result': final_report_html,
                     'file_url': f'/view/{os.path.basename(html_filepath_rel)}' if html_filepath_rel else None,
                     'download_url': f'/download/{os.path.basename(html_filepath_rel)}' if html_filepath_rel else None,
                     'file_type': '.html' if html_filepath_rel else None,
                     'is_research_output': True
                 }
                 yield f"data: {json.dumps(final_data)}\n\n"

        except Exception as e:
            logger.error(f"Severe error during Cosmos report generation: {e}", exc_info=True)
            yield f"data: {json.dumps({'status': 'Severe error during Cosmos report generation.', 'error': True})}\n\n"
            error_occurred = True
        finally:
            with stop_flags_lock:
                stop_flags.pop(query_id, None)
    return Response(stream_with_context(generate_cosmos_report_stream()), mimetype='text/event-stream')

stop_flags = {}
stop_flags_lock = threading.Lock()

@app.route('/api/stop_generation', methods=['POST'])
def stop_generation():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    data = request.get_json()
    query_id = data.get('query_id')

    if not query_id:
        return jsonify({'error': 'Missing query_id.'}), 400

    with stop_flags_lock:
        stop_flags[query_id] = True
    
    logging.info(f"Stop flag set for query_id: {query_id}")
    return jsonify({'success': True, 'message': 'Stop signal received.'})

def check_and_log_stop(query_id, stage=""):
    with stop_flags_lock:
        if stop_flags.get(query_id):
            logging.info(f"Stop signal detected for query_id: {query_id} at stage: {stage}")
            return True
    return False

@app.route('/api/messages/delete', methods=['POST'])
def delete_message():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    data = request.get_json()
    message_id = data.get('message_id')

    if not message_id:
        return jsonify({'error': 'Missing message_id.'}), 400
    
    user_id = session['user_id']
    db = get_db()
    
    try:
        # Verify ownership by checking the chat the message belongs to
        cursor = db.execute('''
            SELECT 1 FROM messages m
            JOIN chats c ON m.chat_id = c.id
            WHERE m.id = ? AND c.user_id = ?
        ''', (message_id, user_id))
        
        if not cursor.fetchone():
            return jsonify({'error': 'Message not found or unauthorized.'}), 403

        db.execute('DELETE FROM messages WHERE id = ?', (message_id,))
        db.commit()

        logging.info(f"User {user_id} deleted message {message_id}.")
        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Error in delete_message: {e}", exc_info=True)
        return jsonify({'error': 'An internal error occurred.'}), 500

@app.route('/api/messages/delete_after', methods=['POST'])
def delete_messages_after():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

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
def clear_history():
    try:
        if 'user_id' not in session:
            return jsonify({'status': 'Failed', 'message': 'Authentication required to clear history.'}), 401

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
        
        cursor = db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
        deleted_count = cursor.rowcount
        db.execute('DELETE FROM tool_calls WHERE chat_id = ?', (chat_id,))
        db.commit()
        
        welcome_message = "Greetings. I am Stellar, a professional AI assistant. I can assist you with research papers using Spectrum Mode, building applications using Forge Mode, and data analysis reports via Cosmos. My capabilities include real-time web search and code execution. How may I assist you today?"
        insert_message(chat_id, "stellar", welcome_message)
        
        return jsonify({'status': 'Success', 'message': 'Conversation history cleared'})
    except sqlite3.Error as db_e:
        logger.error(f"Database error clearing history: {db_e}", exc_info=True)
        return jsonify({'status': 'Failed', 'message': f"Database error clearing history: {str(db_e)}"}), 500
    except Exception as e:
        logger.error(f"Server error clearing history: {e}", exc_info=True)
        return jsonify({'status': 'Failed', 'message': f"Server error clearing history: {str(e)}"}), 500

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
    
    subdir = os.path.dirname(filename)
    basename = os.path.basename(filename)
    return send_from_directory(os.path.join(directory, subdir), basename, mimetype=mimetype)

@app.route('/default.min.css')
def serve_highlight_css():
    return send_from_directory('.', 'default.min.css')

@app.route('/custom_select.css')
def serve_custom_select_css():
    return send_from_directory('.', 'custom_select.css')

@app.route('/custom_select.js')
def serve_custom_select_js():
    return send_from_directory('.', 'custom_select.js')

@app.route('/highlight.min.js')
def serve_highlight_js():
    return send_from_directory('.', 'highlight.min.js')

@app.route('/marked.min.js')
def serve_marked():
    return send_from_directory('.', 'marked.min.js')

@app.route('/turndown.js')
def serve_turndown():
    return send_from_directory('.', 'turndown.js')

def send_approval_email(recipient_email, display_name):
    sender = "nikhil080905@gmail.com"
    password = "kvpb lngz qzxn vdvu"
    
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

@app.route('/api/admin/waitlist', methods=['GET'])
def get_admin_waitlist():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    db = get_db()
    cursor = db.execute("SELECT id, username, display_name, role, is_approved, created_at FROM users ORDER BY created_at DESC")
    waitlist = _fetch_as_dict(cursor)
    return jsonify(waitlist), 200

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
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('current_chat_id', None)
    session.clear()
    return jsonify({"success": True, "message": "Logged out successfully."}), 200

@app.route('/check_auth', methods=['GET'])
def check_auth_status():
    if 'user_id' in session:
        db = get_db()
        cursor = db.execute('SELECT username, display_name, role, is_approved FROM users WHERE id = ?', (session['user_id'],))
        user = _fetchone_as_dict(cursor)
        if user:
            return jsonify({
                "logged_in": True,
                "username": user['username'],
                "display_name": user['display_name'] or user['username'],
                "role": user['role'],
                "is_approved": bool(user['is_approved'])
            }), 200
        else:
            return jsonify({"logged_in": False}), 200
    else:
        return jsonify({"logged_in": False}), 200
@app.route('/api/chats', methods=['GET'])
def get_user_chats():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    
    user_id = session['user_id']
    db = get_db()
    try:
        cursor = db.execute('SELECT id, name, created_at FROM chats WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        chats = _fetch_as_dict(cursor)
        return jsonify(chats), 200
    except sqlite3.Error as e:
        logger.error(f"Database error in get_user_chats: {e}", exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error in get_user_chats: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/chats/new', methods=['POST'])
def create_new_chat():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    
    user_id = session['user_id']
    db = get_db()
    try:
        cursor = db.execute('INSERT INTO chats (user_id, name) VALUES (?, ?)', (user_id, 'New Chat'))
        db.commit()
        new_chat_id = cursor.lastrowid
        
        welcome_message = "Greetings. I am Stellar, a professional AI assistant. I can assist you with research papers using Spectrum Mode, building applications using Forge Mode, and data analysis reports via Cosmos. My capabilities include real-time web search and code execution. How may I assist you today?"
        insert_message(new_chat_id, "stellar", welcome_message)

        session['current_chat_id'] = new_chat_id
        session.modified = True
        
        return jsonify({'success': True, 'chat_id': new_chat_id, 'name': 'New Chat'}), 201
    except sqlite3.Error as e:
        logger.error(f"Database error in create_new_chat: {e}", exc_info=True)
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error in create_new_chat: {e}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/set_active_chat', methods=['POST'])
def set_active_chat():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

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
def delete_chat_route(chat_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    
    user_id = session['user_id']
    db = get_db()
    try:
        cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
        chat_ownership = cursor.fetchone()
        if not chat_ownership:
            return jsonify({'error': 'Unauthorized to delete this chat.'}), 403
        
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
def update_chat_name_route(chat_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    
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
def get_chat_tokens_route(chat_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    
    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('SELECT 1 FROM chats WHERE id = ? AND user_id = ?', (chat_id, user_id))
    chat_ownership = cursor.fetchone()
    if not chat_ownership:
        return jsonify({'error': 'Unauthorized to access this chat\'s tokens.'}), 403
    
    token_count = count_chat_tokens(chat_id)
    return jsonify({'token_count': token_count}), 200

@app.route('/api/user/profile', methods=['GET'])
def get_user_profile():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in."}), 401
    
    return jsonify({"success": True, "username": session['username'], "user_id": session['user_id']}), 200

@app.route('/api/user/change_display_name', methods=['POST'])
def change_display_name_route():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Authentication required."}), 401
    
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

@app.route('/unsplash', methods=['GET'])
def get_unsplash_images():
    if not UNSPLASH_ACCESS_KEY:
        return jsonify({"error": "Unsplash API key is missing."}), 500

    query_themes = ["abstract security", "connectivity", "new beginnings", "technology network", "digital art"]
    selected_query = random.choice(query_themes)

    url = f"https://api.unsplash.com/photos/random"
    params = {
        "count": 5,
        "query": selected_query,
        "orientation": "landscape"
    }
    headers = {
        "Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        photos = response.json()
        
        image_urls = []
        for photo in photos:
            if 'urls' in photo and 'regular' in photo['urls']:
                image_urls.append(photo['urls']['regular'])
            elif 'urls' in photo and 'full' in photo['urls']:
                image_urls.append(photo['urls']['full'])

        if not image_urls:
            return jsonify({"error": "No images found from Unsplash API."}), 404

        return jsonify({"image_urls": image_urls}), 200

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch images from Unsplash: {e}", exc_info=True)
        return jsonify({"error": f"Failed to fetch images from Unsplash. Please check API key, network connection, or API limits. Details: {e}"}), 500
    except json.JSONDecodeError as e:
        logger.error(f"Invalid response from Unsplash API: {e}", exc_info=True)
        return jsonify({"error": "Invalid response from Unsplash API."}), 500


@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/')
def index():
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
        return serve_no_cache('login.html')

    db = get_db()
    cursor = db.execute('SELECT is_approved FROM users WHERE id = ?', (session['user_id'],))
    user_data = cursor.fetchone()

    # If the user was approved in the DB, update their session
    if user_data and user_data[0] == 1:
        session['is_approved'] = True

    if not session.get('is_approved'):
        return serve_no_cache('waitlist.html')
    
    if 'initialized' not in session:
        session['initialized'] = True
        session.permanent = True
        
    return serve_no_cache('index.html')
@app.route('/api/chats/search_messages', methods=['GET'])
def search_messages_route():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    
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
            WHERE T1.user_id = ? AND (
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
def run_code():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    if not client:
        return jsonify({'error': 'Docker client not available. Is Docker running?'}), 503

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
    db = get_db()
    user_id = session['user_id']

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
                init=True,
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
def manage_api_keys():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

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
def stop_container():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401
    
    if not client:
        return jsonify({'error': 'Docker client is not available.'}), 503

    data = request.get_json()
    container_id = data.get('container_id')

    if not container_id:
        return jsonify({'error': 'Missing container_id.'}), 400

    try:
        process_id = None
        app_type = 'run_code'

        for key in redis_client.scan_iter("runcode:process:*"):
            cid = redis_client.hget(key, "container_id")
            if cid and cid == container_id:
                process_id = redis_client.hget(key, "process_id")
                break
        
        if not process_id:
            app_type = 'forge'
            for key in redis_client.scan_iter("forge:process:*"):
                cid = redis_client.hget(key, "container_id")
                if cid and cid == container_id:
                    process_id = redis_client.hget(key, "process_id")
                    break

        if process_id:
            stop_and_cleanup_app_by_process_id(process_id, app_type)
            return jsonify({'success': True, 'message': f'Container {container_id[:12]} and its process stopped.'}), 200
        else:
            container = client.containers.get(container_id)
            logging.info(f"Stopping container {container.short_id} directly (no process found).")
            container.stop(timeout=10)
            return jsonify({'success': True, 'message': f'Container {container.short_id} stopped.'}), 200

    except docker.errors.NotFound:
        return jsonify({'success': False, 'message': 'Container not found (may have already stopped).'}), 404
    except Exception as e:
        logger.error(f"Error stopping container {container_id} via API: {e}")
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500


@app.route('/api/visualize', methods=['POST'])
def generate_visualization():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    data = request.get_json()
    content = data.get('content')
    message_id = data.get('message_id') # Get message_id to persist visualization
    model_id = 'gemini-3.1-pro-preview' # Use the pro preview model as requested
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
                            if current_time - created_ts < 60 * 60:
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
            except Exception as e:
                logger.error(f"OrphanContainerMonitor: Error processing container {container.short_id}: {e}")

def cleanup_stale_containers():
    try:
        # Always reset any stuck statuses in the database to 'stopped' on startup
        try:
            with app.app_context():
                db = get_db()
                db.execute("UPDATE forge_history SET status = 'stopped' WHERE status IN ('running', 'starting', 'created')")
                db.commit()
                logging.info("Database status for forge_history reset to 'stopped'.")
        except Exception as db_err:
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

        logging.warning(f"Found {len(all_stale)} stale sandbox container(s). Cleaning up...")
        for container in all_stale:
            try:
                logging.warning(f"Force-removing stale container: {container.name} ({container.short_id})")
                container.remove(force=True) 
            except docker.errors.NotFound:
                logging.info(f"Container {container.name} was already removed.")
            except Exception as e:
                logging.error(f"Error during cleanup of container {container.name}: {e}")
        logging.info("Stale container cleanup complete.")

    except docker.errors.DockerException as e:
        logging.error(f"Docker is not available. Skipping stale container cleanup. Error: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred during stale container cleanup: {e}")

# Start the orphan monitor
orphan_monitor = OrphanContainerMonitor(interval=60)
if not app.config.get('TESTING'):
    orphan_monitor.start()
    atexit.register(orphan_monitor.stop)
active_apps = {}
active_apps_lock = threading.Lock()
@app.before_request
def intercept_subdomains():
    host = request.headers.get('Host', '')
    domain_parts = host.split(':')[0].split('.')

    # Catch any request to *.stellarai.live (excluding www and the main root domain)
    if len(domain_parts) >= 3 and domain_parts[-2] == 'stellarai' and domain_parts[-1] == 'live' and domain_parts[0] != 'www':
        subdomain = domain_parts[0]

        db = get_db()
        cursor = db.execute("SELECT process_id FROM forge_history WHERE subdomain = ?", (subdomain,))
        row = cursor.fetchone()

        # Fallback to process_id (uuid) if it's a temporary run_code container
        process_id = row['process_id'] if row else subdomain

        app_info = None
        with active_apps_lock:
            app_info = active_apps.get(process_id)

        if not app_info:
            try:
                redis_key = _redis_forge_key(process_id)
                redis_data = redis_client.hgetall(redis_key)
                if not redis_data:
                    redis_key = _redis_runcode_key(process_id)
                    redis_data = redis_client.hgetall(redis_key)

                if redis_data and redis_data.get("host_port") and redis_data.get("status") in["running", "created", "exited"]:
                    app_info = {
                        "port": int(redis_data["host_port"]),
                        "container_id": redis_data.get("container_id"),
                        "status": redis_data.get("status")
                    }
                    with active_apps_lock:
                        active_apps[process_id] = app_info
            except Exception as e:
                logger.error(f"Redis lookup failed for app {process_id}: {e}")
                return "Error looking up application state.", 500

        if not app_info or not app_info.get("port"):
            return f"Application '{subdomain}' is stopped or unavailable. Start it in Stellar Forge.", 503

        target_port = app_info["port"]
        path = request.full_path # Preserves exact routing paths and query parameters!
        target_url = f"http://127.0.0.1:{target_port}{path}"

        try:
            resp = requests.request(
                method=request.method,
                url=target_url,
                headers={key: value for (key, value) in request.headers if key.lower() != 'host'},
                data=request.get_data(),
                cookies=request.cookies,
                allow_redirects=False,
                stream=True, 
                timeout=600
            )

            excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
            headers =[(name, value) for (name, value) in resp.raw.headers.items() if name.lower() not in excluded_headers]

            return Response(resp.content, resp.status_code, headers)

        except requests.exceptions.RequestException as e:
            logger.error(f"Dynamic proxy error for app {process_id}: {e}")
            if app_info.get("status") == "exited":
                 return "Application not found or has been stopped.", 404
            return f"Error proxying request to application.", 502


@app.route('/codelab/forge/files', methods=['GET'])
def forge_get_files():
    if 'user_id' not in session or 'forge_project' not in session:
        return jsonify({'error': 'No active Forge session.'}), 404
    return jsonify(session['forge_project'].get('files', {}))

@app.route('/codelab/forge/database', methods=['GET'])
def forge_get_database():
    """Fetch the SQLite database file from the running Forge container."""
    if 'user_id' not in session or 'forge_project' not in session:
        return jsonify({'error': 'No active Forge session.'}), 404
    
    process_id = session['forge_project'].get('process_id')
    if not process_id:
        return jsonify({'error': 'No process ID found.'}), 404
    
    # Get container_id from Redis (since it's stored there, not in session)
    container_id = None
    try:
        redis_key = _redis_forge_key(process_id)
        cid = redis_client.hget(redis_key, "container_id")
        if cid:
            container_id = cid.decode() if isinstance(cid, (bytes, bytearray)) else cid
    except Exception as e:
        logger.error(f"Error fetching container_id from redis: {e}")
    
    # Fallback to active_apps if Redis didn't have it
    if not container_id:
        with active_apps_lock:
            app_info = active_apps.get(process_id, {})
            container_id = app_info.get('container_id')
    
    if not container_id:
        return jsonify({'error': 'No running container found.'}), 404
    
    try:
        container = client.containers.get(container_id)
        
        # First, find any .db files in the /app directory
        find_result = container.exec_run("find /app -maxdepth 1 -name '*.db' -type f", demux=False)
        db_files = find_result.output.decode('utf-8', errors='replace').strip().split('\n') if find_result.output else []
        db_files = [f for f in db_files if f.endswith('.db')]
        
        if not db_files:
            return jsonify({'error': 'No database found.'}), 404
        
        # Use the first .db file found (prefer database.db if exists)
        db_path = '/app/database.db' if '/app/database.db' in db_files else db_files[0]
        db_name = db_path.split('/')[-1]
        
        # Read the database file
        cat_result = container.exec_run(f"cat {db_path}", demux=False)
        if cat_result.exit_code != 0 or not cat_result.output:
            return jsonify({'error': 'Could not read database file.'}), 404
        
        # Return base64 encoded database
        import base64
        db_base64 = base64.b64encode(cat_result.output).decode('utf-8')
        return jsonify({'database': db_base64, 'name': db_name})
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found.'}), 404
    except Exception as e:
        logger.error(f"Error fetching database: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/codelab/forge/redeploy', methods=['POST'])
def forge_redeploy():
    if 'user_id' not in session or 'forge_project' not in session:
        return jsonify({'error': 'No active session.'}), 400
    if not client:
        return jsonify({'error': 'Docker client not available.'}), 503

    data = request.get_json(silent=True) or {}
    updated_files = data.get('files', {})

    if 'index.html' not in updated_files or 'app.py' not in updated_files:
        return jsonify({'error': "Request must contain 'index.html' and 'app.py'."}), 400

    old_process_id = session['forge_project'].get('process_id')
    old_container_id = None
    
    if old_process_id:
        redis_key = _get_process_key_prefix(old_process_id, 'forge')
        try:
            cached_data = redis_client.hgetall(redis_key)
            if cached_data:
                old_container_id = cached_data.get('container_id')
        except Exception:
            pass
            
        with active_apps_lock:
            active_apps.pop(old_process_id, None)

    try:
        process_id = old_process_id if old_process_id else str(uuid.uuid4())
        
        project_title = session.get('forge_project', {}).get('project_name')
        if not project_title:
             project_title = f"Forge Redeploy {process_id[:8]}"

        session['forge_project']['files'] = updated_files
        session['forge_project']['process_id'] = process_id
        session['forge_project']['project_name'] = project_title
        session.modified = True

        # Record in history
        try:
            db = get_db()
            db.execute('''
                INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot)
                VALUES (?, ?, ?, ?, ?)
            ''', (session['user_id'], project_title, process_id, 'starting', json.dumps(updated_files)))
            db.commit()
        except Exception as e:
            logger.error(f"Failed to record forge history redeploy: {e}")

        try:
            redis_client.hset(_redis_forge_key(process_id), mapping={
                "status": "starting",
                "files": json.dumps(updated_files)
            })
        except Exception:
            logger.exception("Failed to persist redeploy forge state for %s", process_id)

        app_obj = current_app._get_current_object()
        thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, updated_files, process_id, old_container_id, 'forge'))
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'process_id': process_id})

    except Exception as e:
        logger.error(f"Error in forge_redeploy: {e}", exc_info=True)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500

@app.route('/api/forge/history', methods=['GET'])
def get_forge_history():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('''
        SELECT id, project_name, process_id, status, deployment_url, created_at, last_updated 
        FROM forge_history
        WHERE user_id = ?
        ORDER BY created_at DESC
    ''', (user_id,))
    history = _fetch_as_dict(cursor)
    return jsonify({'history': history})

@app.route('/api/forge/history/<int:history_id>/resume', methods=['POST'])
def resume_forge_history(history_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    user_id = session['user_id']
    db = get_db()
    cursor = db.execute('SELECT * FROM forge_history WHERE id = ? AND user_id = ?', (history_id, user_id))
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
    if 'forge_project' in session:
        try:
            stop_and_cleanup_app_by_process_id(session['forge_project'].get('process_id'), app_type='forge')
        except Exception as e:
            logger.warning(f"Error stopping previous forge project during resume: {e}")

    process_id = str(uuid.uuid4())
    project_name = entry.get('project_name') or "Forge Project"

    session['forge_project'] = {
        'files': files,
        'container_id': None,
        'process_id': process_id,
        'project_name': project_name
    }
    session.modified = True

    # Notify via Telegram
    try:
        db = get_db()
        cursor = db.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],))
        user_row = cursor.fetchone()
        if user_row:
            current_username = user_row['username']
            telegram_bot.send_message(f"🛠️ {current_username} resumed forge session: {project_name}")
    except Exception as e:
        logger.error(f"Failed to send Forge Resume Telegram notification: {e}")

    return jsonify({'success': True, 'message': 'Project loaded.', 'files': files, 'process_id': process_id})

@app.route('/api/forge/history/<int:history_id>', methods=['DELETE'])
def delete_forge_history(history_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    user_id = session['user_id']
    db = get_db()

    cursor = db.execute('SELECT process_id, container_id FROM forge_history WHERE id = ? AND user_id = ?', (history_id, user_id))
    entry = _fetchone_as_dict(cursor)

    if not entry:
        return jsonify({'error': 'Entry not found.'}), 404

    process_id = entry['process_id']

    # Stop if running
    stop_and_cleanup_app_by_process_id(process_id, app_type='forge')

    db.execute('DELETE FROM forge_history WHERE id = ?', (history_id,))
    db.commit()

    return jsonify({'success': True, 'message': 'History entry deleted.'})
cleanup_stale_containers()
    
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5013))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
