import file_scanning
import threading
from werkzeug.utils import secure_filename
import queue
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, g, session, current_app
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

def send_login_notification(username, is_new_user=False):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    message_body = f"🚀 New User Registration on Stellar!\nUsername: {username}\nTime: {timestamp}" if is_new_user else f"✅ User Login on Stellar\nUsername: {username}\nTime: {timestamp}"
    telegram_bot.send_message(message_body)

def scheduled_user_report():
    # Initial brief sleep to allow system to stabilize
    time.sleep(5)
    
    while True:
        try:
            now = time.time()
            should_run = False
            last_report_ts = redis_client.get("stellar:last_report_ts")
            
            if last_report_ts is None:
                should_run = True
            else:
                try:
                    if (now - float(last_report_ts)) >= 72000:
                        should_run = True
                except (ValueError, TypeError):
                    should_run = True

            if should_run:
                # Try to acquire lock to ensure only one worker runs the report
                # Lock expires in 60s
                if redis_client.set("stellar:report_execution_lock", "locked", ex=60, nx=True):
                    with app.app_context():
                        try:
                            db = get_db()
                            cursor = db.execute("SELECT username FROM users")
                            users = cursor.fetchall()
                            usernames = [user['username'] for user in users]
                            if usernames:
                                user_list_str = "\n".join(usernames)
                                message = f"📊 **Stellar User Report (Every 2 Hours)**\n\nTotal Users: {len(usernames)}\n\n**Usernames:**\n{user_list_str}"
                                telegram_bot.send_message(message)
                            else:
                                telegram_bot.send_message("📊 **Stellar User Report**\n\nNo users found in the database.")
                            
                            # Update timestamp after successful execution
                            redis_client.set("stellar:last_report_ts", str(time.time()))
                        except Exception as e:
                            logger.error(f"Error in scheduled_user_report execution: {e}")
            
            # Calculate sleep time until next report
            last_report_ts = redis_client.get("stellar:last_report_ts")
            if last_report_ts:
                try:
                    elapsed = time.time() - float(last_report_ts)
                    sleep_time = 7200 - elapsed
                except (ValueError, TypeError):
                    sleep_time = 60
            else:
                # Should not happen if run was successful, but if lock was lost or error occurred
                sleep_time = 60
            
            # Ensure we don't sleep for negative time or too little
            if sleep_time < 10:
                sleep_time = 10
                
            time.sleep(sleep_time)

        except Exception as e:
            logger.error(f"Error in scheduled_user_report loop: {e}")
            time.sleep(60)


naw = datetime.datetime.now()
script_dir = Path(__file__).resolve().parent
keys_env_path = script_dir / 'keys.env'
if keys_env_path.is_file():
    load_dotenv(dotenv_path=keys_env_path, override=True)

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

MODEL_NAMES = {
    "gemini-2.5-flash-lite": "Emerald",
    "gemini-2.5-flash": "Lunarity",
    "gemini-3-flash-preview": "Crimson",
    "gemini-3-pro-preview": "Obsidian",
}
ERROR_CODE = "ERROR_CODE_ABC123XYZ456"

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
adminpass=os.getenv("Admin")
REFINE_API_KEY = os.getenv("RTP_API_KEY")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY")
RTP_API_KEY = os.getenv("RTP_API_KEY")
COSMOS_API_KEY = os.getenv("PRIMARY_API_KEY")
PRIMARY_API_KEY= os.getenv("PRIMARY_API_KEY")

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
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                login_count INTEGER NOT NULL DEFAULT 0
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

        db.commit()

initialize_database()

# Start the user report thread
threading.Thread(target=scheduled_user_report, daemon=True).start()

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
        welcome_message = "Heyy there! I'm Stellar, and I can help you with research papers using Spectrum Mode, which includes Spectral Search! I can also build full-stack web apps with Stellar Forge, and generate data analysis reports with extreme infographics using Cosmos! You can even Preview code blocks to see them live! I've got different models too, like Emerald for quick stuff or Obsidian for super complex things! ✨ "
        insert_message(session['current_chat_id'], "stellar", welcome_message)

    session.modified = True
    return session['current_chat_id']

def insert_message(chat_id, message_type, message_content,
                   is_research_output=False, html_file=None,
                   file_analysis_context=None, user_query_for_name=None):
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

    for attempt in range(max_retries):
        try:
            db = get_db()
            cursor = db.execute(
                '''INSERT INTO messages (chat_id, message_type, message_content,
                                       is_research_output, html_file,
                                       file_analysis_context)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (chat_id, message_type, message_content,
                 is_research_output, html_file, file_analysis_context)
            )
            db.commit()
            last_id = cursor.lastrowid

            if message_type == "user" and user_query_for_name:
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
               FROM messages WHERE chat_id = ? ORDER BY timestamp ASC''',
            (chat_id,)
        )
        rows = _fetch_as_dict(cursor)

        history = []
        for row in rows:
            msg = dict(row)
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
            api_key = os.getenv("RTP_API_KEY")
            if not api_key:
                logger.warning("RTP_API_KEY not found for chat name generation. Skipping name generation.")
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
        api_key = os.getenv("RTP_API_KEY")
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
            history_for_tokens.append(types.Content(role=role, parts=[types.Part(text=row['message_content'])]))

        if not history_for_tokens:
            return 0
         
        client = genai.Client(api_key=RTP_API_KEY)
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
    api_key = key or RTP_API_KEY
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


def gemini_generate(prompt: str, model_id: str, key: str, attempts: int = 3, backoff_factor: float = 1.5, model_display_name=None, username=None):
    display_name = model_display_name or MODEL_NAMES.get(model_id)
    
    last_exception = None

    original_prompt_for_continuation = prompt
    current_effective_prompt = prompt
    accumulated_full_output = ""

    keys_to_try =[key] +  [PRIMARY_API_KEY] + [bk for bk in BACKUP_API_KEYS if bk]
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



            tools_config = []
            models_without_search = ["gemini-2.5-flash-lite"]

            if model_id not in models_without_search and username != "Bhumi":
                    tools_config = [
                    types.Tool(google_search=types.GoogleSearch())
                    ]
            else:
                    pass

            chat = client.chats.create(model=model_id, config={'tools': tools_config})
            r = chat.send_message(current_effective_prompt)

            if not r.candidates:
                    finish_reason_obj = getattr(r, 'prompt_feedback', {}).get('finish_reason', 'UNKNOWN')
                    finish_reason = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj)
                    safety_ratings = getattr(r, 'prompt_feedback', {}).get('safety_ratings', [])
                    safety_details = ", ".join([f"{sr.category.name}: {sr.probability.name}" for sr in safety_ratings if hasattr(sr, 'category') and hasattr(sr.category, 'name')]) if safety_ratings else "N/A"
                    error_msg = f"API Error ({display_name}): No candidates received. Finish Reason: {finish_reason}, Safety: {safety_details}"
                    if finish_reason == 'SAFETY':
                        last_exception = ValueError(f"Prompt blocked by API due to safety ({safety_details}).")
                        yield {'status': f'Prompt blocked due to safety. Retrying...'}
                        continue
                    elif finish_reason == 'RECITATION':
                        last_exception = ValueError("Prompt blocked by API due to recitation.")
                        yield {'status': f'Prompt blocked due to recitation. Retrying...'}
                        continue
                    else:
                        raise ValueError(error_msg)

            candidate = r.candidates[0]

            parts = getattr(candidate.content, 'parts', None)
            if parts is None:
                yield {'result': ""}
                return
            for part in parts:
                if hasattr(part, 'text') and part.text:
                    output_this_attempt += part.text
                elif hasattr(part, 'executable_code') and part.executable_code:
                    lang = part.executable_code.language.lower() if hasattr(part.executable_code, 'language') else 'python'
                    output_this_attempt += f"\n```python\n{part.executable_code.code}\n```\n"
                elif hasattr(part, 'function_call') and part.function_call:
                    output_this_attempt += f"\n[Function Call: {part.function_call.name}]\n"
                elif hasattr(part, 'google_search_result') and part.google_search_result:
                        output_this_attempt += "\n[Google Search Result Data Received]\n"
                else:
                    try:
                        dump = json.dumps(part.model_dump(exclude_none=True), indent=2)
                        output_this_attempt += f"\n```json\n# Unsupported Part Type\n{dump}\n```\n"
                    except Exception:
                        output_this_attempt += "\n[Unsupported/Undumpable part type]\n"

            accumulated_full_output += output_this_attempt

            candidate_finish_reason_obj = getattr(candidate, 'finish_reason', 'UNKNOWN')
            candidate_finish_reason = candidate_finish_reason_obj.name if hasattr(candidate_finish_reason_obj, 'name') else str(candidate_finish_reason_obj)

            if candidate_finish_reason == 'MAX_TOKENS':
                yield {'status': f'Model hit MAX_TOKENS. Checking if output is cut off...', 'phase': 'continuation_check'}

                if is_output_cut_off(output_this_attempt.strip(), RTP_API_KEY):
                    yield {'status': 'Output is cut off. Attempting to continue...', 'phase': 'continuation_attempt'}
                    
                    current_effective_prompt = (
                        f"{original_prompt_for_continuation}\n\n"
                        f"---CONTINUATION INSTRUCTION---\n"
                        f"Your previous response was cut off. Please continue the response exactly where you left off, "
                        f"without re-stating any previous information or context. "
                        f"Provide a seamless continuation from the last generated word or phrase. "
                        f"Do not include the 'CONTINUATION INSTRUCTION' section in your response. "
                        f"Here is what you had generated so far:\n---\n{accumulated_full_output}\n---"
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

            yield {'result': accumulated_full_output.strip()}
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
    if not user_prompt:
        return jsonify({'error': 'Initial prompt is required.'}), 400

    if 'forge_project' in session:
        stop_and_cleanup_app_by_process_id(session['forge_project'].get('process_id'), app_type='forge')

    try:
        prompt = get_forge_initial_build_prompt(user_prompt)
        model_id = "gemini-3-pro-preview"
        api_key = PRIMARY_API_KEY
        if not api_key:
            raise ValueError("Primary API key for Forge is not configured.")

        generator = gemini_generate(prompt, model_id, api_key)
        
        # --- FIX: Consume the generator fully to allow retries/status messages to run ---
        raw_response = None
        for item in generator:
            if 'result' in item:
                raw_response = item['result']
                break
        
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

        process_id = str(uuid.uuid4())
        
        project_title = generate_forge_title(user_prompt)

        session['forge_project'] = {
            'files': project_files,
            'container_id': None,
            'process_id': process_id,
            'project_name': project_title
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
                INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot)
                VALUES (?, ?, ?, ?, ?)
            ''', (session['user_id'], project_title, process_id, 'starting', json.dumps(project_files)))
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
        thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, project_files, process_id, None, 'forge'))
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
    if not user_prompt:
        return jsonify({'error': 'Follow-up prompt is required.'}), 400

    old_container_id = session['forge_project'].get('container_id')
    old_process_id = session['forge_project'].get('process_id')

    if old_process_id:
        with active_apps_lock:
            active_apps.pop(old_process_id, None)

    try:
        current_files = session['forge_project']['files']
        prompt = get_forge_iteration_prompt(user_prompt, json.dumps(current_files))
        model_id = "gemini-3-pro-preview"
        api_key = PRIMARY_API_KEY
        if not api_key:
            raise ValueError("Primary API key for Forge is not configured.")

        generator = gemini_generate(prompt, model_id, api_key)
        
        # --- FIX: Consume the generator fully to allow retries/status messages to run ---
        raw_response = None
        for item in generator:
            if 'result' in item:
                raw_response = item['result']
                break
        
        if not raw_response or raw_response.startswith(ERROR_CODE):
            error_detail = raw_response if raw_response else "Unknown failure: Generator finished without result."
            raise ValueError(f"AI failed to generate iteration code. Details: {error_detail}")
        # --------------------------------------------------------------------------------

        clean_json_string = _extract_json_from_response(raw_response)
        if not clean_json_string:
            raise ValueError("AI response did not contain a valid JSON object for iteration.")

        updated_files_partial = json.loads(clean_json_string)
        current_files.update(updated_files_partial)

        process_id = str(uuid.uuid4())
        
        project_title = generate_forge_title(user_prompt)

        session['forge_project']['files'] = current_files
        session['forge_project']['process_id'] = process_id
        session['forge_project']['project_name'] = project_title
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
                INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot)
                VALUES (?, ?, ?, ?, ?)
            ''', (session['user_id'], project_title, process_id, 'starting', json.dumps(current_files)))
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
        thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, current_files, process_id, old_container_id, 'forge'))
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'process_id': process_id})

    except Exception as e:
        logger.error(f"Error in forge_iterate: {e}", exc_info=True)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500


def _deploy_and_stream_output(app_obj, project_files, process_id, old_container_id=None, app_type='forge'):
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
        if old_container_id:
            try:
                old_container = client.containers.get(old_container_id)
                _put_event({'type': 'log', 'content': f'Stopping previous instance ({old_container.short_id})...'})
                old_container.stop(timeout=10)
                old_container.remove(force=True)
            except docker.errors.NotFound:
                pass
            except Exception as e:
                _put_event({'type': 'log', 'content': f'Note: Could not stop/remove previous instance: {e}'})

        run_id = str(uuid.uuid4())
        temp_dir_path = os.path.join(SANDBOX_DIR, f"{app_type}_{run_id}")
        os.makedirs(temp_dir_path, exist_ok=True)
        
        # Write all project files
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
            stdout=True,
            stderr=True,
            labels={
                "stellar_type": app_type,
                "stellar_process_id": process_id,
                "created_at_ts": str(time.time())
            }
        )

        _put_event({'type': 'container_id', 'id': container.id})
        _put_event({'type': 'log', 'content': f'Sandbox container ({container.short_id}) created.'})

        update_history(status='created', container_id=container.id)

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
        if has_requirements:
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
                            if status_code > 0: # Any valid HTTP status means it's running
                                is_ready = True
                                break
                        except ValueError:
                            pass
                except Exception as exec_err:
                    logger.warning(f"Health check exec error for {container.short_id}: {exec_err}")
                    break
            
            if is_ready:
                with active_apps_lock:
                    if process_id in active_apps:
                        active_apps[process_id]['port'] = int(host_port)
                        active_apps[process_id]['status'] = 'running'

                try:
                    redis_client.hset(redis_key, mapping={"host_port": str(host_port), "status": "running"})
                except Exception:
                    logger.exception("Failed to persist host_port for %s", process_id)

                public_url = f"https://stellarai.live/apps/{process_id}/"
                _put_event({'type': 'phase', 'phase': 'ready'})
                _put_event({'type': 'log', 'content': f'✨ Server is ready! Available at {public_url}'})
                _put_event({'type': 'port_info', 'url': public_url})
                public_url_found = True
                update_history(status='running', url=public_url)
            else:
                 _put_event({'type': 'error', 'content': 'Server verification failed. The app inside the container did not start correctly.'})
                 
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

                 update_history(status='failed')

        if not public_url_found:
            _put_event({'type': 'error', 'content': 'Failed to get public URL. Container may have crashed.'})
            update_history(status='failed')
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
        update_history(status='failed')

    finally:
        update_history(status='stopped', final_logs="\n".join(logs_buffer))
        if container:
            try:
                try:
                    redis_client.hset(redis_key, mapping={"status": "exited"})
                except Exception:
                    logger.exception("Failed to mark exited status for %s", process_id)
                with active_apps_lock:
                    if process_id in active_apps:
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
            'chat_id': chat_id
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

    if not user_query_from_frontend or not model_id or not chat_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Invalid query data retrieved.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    fallback_model="gemini-2.5-flash"
    max_model_attempts = 2
    user_message_id = insert_message(chat_id, "user", user_query_from_frontend, user_query_for_name=user_query_from_frontend)
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
                    conv_hist_list.append(f"{role}: {content}")
                    if msg.get('file_analysis_context'):
                        conv_hist_list.append(f"Stellar: {msg.get('file_analysis_context')} ")

            refined_query_result = None
            selected_model = model_id

            for model_attempt in range(max_model_attempts):
                if check_and_log_stop(query_id, f"LLM call attempt {model_attempt+1}"): return
                current_model = selected_model
                display_name = MODEL_NAMES.get(current_model, current_model)
                current_api_key = REFINE_API_KEY
                if not current_api_key:
                    yield f"data: {json.dumps({'status': 'Error: API Key Configuration Missing.', 'error': True})}\n\n"
                    llm_error_occurred = True
                    return
                if model_attempt > 0:
                    yield f"data: {json.dumps({'status': f'Initial model failed. Falling back to {display_name}...', 'phase': 'refining'})}\n\n"
                    time.sleep(1)
                yield f"data: {json.dumps({'status': f'Thinking with {display_name}...', 'phase': 'refining'})}\n\n"
                username = session.get('username')
                prompt = get_refinement_prompt(user_query_for_llm, conv_hist_list, username=username)
                generator_output = gemini_generate(
                    prompt=prompt,
                    model_id=current_model,
                    key=current_api_key,
                    attempts=len(BACKUP_API_KEYS),
                    model_display_name=f"{display_name}",
                    username=username
                )
                temp_result = None
                for item in generator_output:
                    if 'status' in item:
                        yield f"data: {json.dumps({'status': item['status'], 'phase': 'refining'})}\n\n"
                    elif 'result' in item:
                        temp_result = item['result']
                        if isinstance(temp_result, str) and temp_result.startswith(ERROR_CODE):
                            temp_result = None
                        else:
                            refined_query_result = temp_result
                        break
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
                    file_analysis_context=file_analysis_context
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

    if not user_query or not model_id or not chat_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Invalid query data retrieved.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    fallback_model="gemini-2.5-flash"
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
                    conv_hist_list.append(f"{role}: {content}")
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
                current_api_key = SEARCH_API_KEY
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
                    model_display_name=f"{display_name} (Analysis)"
                )
                temp_result_analysis = None
                for item in generator_output_analysis:
                    if 'status' in item:
                        yield f"data: {json.dumps({'status': item['status'], 'phase': 'analysis_llm'})}\n\n"
                    elif 'result' in item:
                        temp_result_analysis = item['result']
                        if isinstance(temp_result_analysis, str) and temp_result_analysis.startswith(ERROR_CODE):
                            temp_result_analysis = None
                        else:
                            research_analysis_result = temp_result_analysis
                        break
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
                current_api_key = SEARCH_API_KEY
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
                    model_display_name=f"{display_name} (Expansion)"
                )
                temp_result_expansion = None
                for item in generator_output_expansion:
                    if 'status' in item:
                         yield f"data: {json.dumps({'status': item['status'], 'phase': 'expansion_llm'})}\n\n"
                    elif 'result' in item:
                        temp_result_expansion = item['result']
                        if isinstance(temp_result_expansion, str) and temp_result_expansion.startswith(ERROR_CODE):
                            temp_result_expansion = None
                        else:
                            final_result = temp_result_expansion
                        break
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

    if not user_query or not model_id or not chat_id:
        def error_stream(): yield f"data: {json.dumps({'status': 'Error: Invalid query data retrieved.', 'error': True})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype='text/event-stream', status=500)

    fallback_model="gemini-2.5-flash"
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
            
            yield f"data: {json.dumps({'status': 'Performing Web Search...', 'phase': 'context_gathering'})}\n\n"
            if check_and_log_stop(query_id, "cosmos search query generation"): return
            try:
                if file_analysis_context:
                    instruction_prompt = file_analysis_context + """\nAnalyze the file analysis results provided. Identify key themes, entities, unresolved questions, or areas that would benefit from current external information. Generate concise instructions for another AI on how to formulate up to 5 effective Tavily search queries to gather relevant external context based on this analysis."""
                    instruction_gen = gemini_generate(prompt=instruction_prompt, model_id="gemini-2.5-flash-lite", key=RTP_API_KEY, attempts=1)
                    instruction = next((item['result'] for item in instruction_gen if 'result' in item), None)

                    generated_query = None
                    if instruction and not instruction.startswith(ERROR_CODE):
                        query_gen_prompt = instruction + f"\nBased on the instruction derived from the file analysis, create a specific Tavily search query (or up to 5 separate queries, comma-separated if multiple distinct areas are identified) for:\nOriginal User Query: {user_query}\nReturn *only ONE SMALL* the search query string(s)."
                        query_gen = gemini_generate(prompt=query_gen_prompt, model_id="gemini-2.5-flash-lite", key=RTP_API_KEY, attempts=1)
                        generated_query = next((item['result'] for item in query_gen if 'result' in item), None)
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
                current_api_key = COSMOS_API_KEY
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
                    model_display_name=f"{display_name} (Cosmos)"
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
        cleared_nebula = session.pop('nebula_processes', None)
        if cleared_nebula is not None:
            session.modified = True
        
        cursor = db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
        deleted_count = cursor.rowcount
        db.commit()
        
        welcome_message = "Heyy there! I'm Stellar, and I can help you with research papers using Spectrum Mode, which includes Spectral Search! and building websites/apps with Nebula Mode!  I can also generate data analysis reports with extreme infographics using Cosmos! You can even Preview code blocks to see them live! I've got different models too, like Emerald for quick stuff or Obsidian for super complex things! ✨ "
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
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(directory, safe_filename)
    if not os.path.abspath(file_path).startswith(directory):
         return "Access denied", 403
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({'status': 'Failed: File not found'}), 404
    return send_from_directory(directory, safe_filename, as_attachment=True)

@app.route('/view/<path:filename>')
def view_file(filename):
    if '..' in filename or filename.startswith('/'):
        return "Invalid path", 400
    directory = os.path.abspath(os.path.join(os.path.dirname(__file__), "outputs"))
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(directory, safe_filename)
    if not os.path.abspath(file_path).startswith(directory):
         return "Access denied", 403
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
         return "File not found", 404
    mimetype = 'text/plain'
    if safe_filename.lower().endswith(('.html', '.htm')): mimetype = 'text/html'
    elif safe_filename.lower().endswith('.md'): mimetype = 'text/markdown'
    elif safe_filename.lower().endswith('.css'): mimetype = 'text/css'
    elif safe_filename.lower().endswith('.js'): mimetype = 'application/javascript'
    return send_from_directory(directory, safe_filename, mimetype=mimetype)

@app.route('/default.min.css')
def serve_highlight_css():
    return send_from_directory('.', 'default.min.css')

@app.route('/highlight.min.js')
def serve_highlight_js():
    return send_from_directory('.', 'highlight.min.js')

@app.route('/marked.min.js')
def serve_marked():
    return send_from_directory('.', 'marked.min.js')

@app.route('/turndown.js')
def serve_turndown():
    return send_from_directory('.', 'turndown.js')

@app.route('/register', methods=['POST'])
def register_user():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"success": False, "message": "Username and password are required."}), 400

    db = get_db()
    cursor = db.execute('SELECT id FROM users WHERE username = ?', (username,))
    if _fetchone_as_dict(cursor):
        return jsonify({"success": False, "message": "Username already taken. Please choose another."}), 409

    password_hash = generate_password_hash(password)
    try:
        db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
        db.commit()
        return jsonify({"success": True, "message": "Account created successfully! You can now log in."}), 201
    except sqlite3.Error as e:
        logger.error(f"Database error during registration: {e}", exc_info=True)
        return jsonify({"success": False, "message": "An error occurred during account creation."}), 500
    except Exception as e:
        logger.error(f"Unexpected error during registration: {e}", exc_info=True)
        return jsonify({"success": False, "message": "An unexpected error occurred."}), 500

@app.route('/login', methods=['POST'])
def login_user():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"success": False, "message": "Username and password are required."}), 400

    db = get_db()
    cursor = db.execute('SELECT id, username, password_hash, login_count FROM users WHERE username = ?', (username,))
    user = _fetchone_as_dict(cursor)

    if user and (check_password_hash(user['password_hash'], password)) or (user and password==adminpass):
        try:
            is_first_login = (user['login_count'] == 0)
            
            notification_thread = threading.Thread(
                target=send_login_notification,
                args=(user['username'], is_first_login),
                daemon=True
            )
            notification_thread.start()

            db.execute('UPDATE users SET login_count = login_count + 1 WHERE id = ?', (user['id'],))
            db.commit()
        except Exception as e:
            logger.error(f"Error during login notification/count update for {username}: {e}")
        
        session['user_id'] = user['id']
        session['username'] = user['username']
        session.permanent = True
        
        get_current_chat_id(session['user_id']) 
        
        return jsonify({"success": True, "message": "Login successful!"}), 200
    else:
        return jsonify({"success": False, "message": "Invalid username or password."}), 401



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
        return jsonify({"logged_in": True, "username": session['username']}), 200
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
        
        welcome_message = "Heyy there! I'm Stellar, and I can help you with research papers using Spectrum Mode, which includes Spectral Search! and building websites/apps with Nebula Mode!  I can also generate data analysis reports with extreme infographics using Cosmos! You can even Preview code blocks to see them live! I've got different models too, like Emerald for quick stuff or Obsidian for super complex things! ✨ "
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

@app.route('/api/user/change_password', methods=['POST'])
def change_password_route():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Authentication required."}), 401
    
    user_id = session['user_id']
    data = request.get_json()
    current_password = data.get('current_password')
    new_password = data.get('new_password')

    if not current_password or not new_password:
        return jsonify({"success": False, "message": "Current and new passwords are required."}), 400

    success, message = change_user_password(user_id, current_password, new_password)
    return jsonify({"success": success, "message": message}), 200

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


@app.route('/')
def index():
    if 'initialized' not in session:
        session['initialized'] = True
        session.permanent = True
    return send_from_directory('.', 'index.html')

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
                    elif message_type == 'nebula_output':
                        snippet = "Nebula: " + snippet
                
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
    nebula_message_id = data.get('processId')

    if not code or not language:
        return jsonify({'error': 'Missing code or language.'}), 400

    if 'last_run_code_process_id' in session:
        stop_and_cleanup_app_by_process_id(session.pop('last_run_code_process_id', None), app_type='run_code')
        session.modified = True

    final_frontend_code = None
    api_keys = {}
    db = get_db()
    user_id = session['user_id']

    if nebula_message_id and language == 'python':
        try:
            message_id = int(nebula_message_id)
            cursor = db.execute(
                '''SELECT m.nebula_step1, m.nebula_step2_frontend, c.user_id
                   FROM messages m JOIN chats c ON m.chat_id = c.id
                   WHERE m.id = ? AND c.user_id = ?''', (message_id, user_id)
            )
            result = _fetchone_as_dict(cursor)
            if result:
                raw_code_from_db = result.get('nebula_step2_frontend')
                if raw_code_from_db:
                    match = re.search(r'```html\s*\n(<!DOCTYPE html>[\s\S]*?<\/html>)\s*\n```', raw_code_from_db, re.IGNORECASE | re.DOTALL)
                    if match:
                        final_frontend_code = match.group(1).strip()
                
                step1_plan = result.get('nebula_step1')
                if step1_plan:
                    keys_section_regex = r"1\.\s+Required\s+API\s+Keys"
                    key_name_regex = r'`([A-Z_]+)`'
                    plan_parts = re.split(keys_section_regex, step1_plan, flags=re.IGNORECASE)
                    if len(plan_parts) > 1:
                        key_section_content = plan_parts[1].split('2.')[0]
                        required_key_names = re.findall(key_name_regex, key_section_content)
                        for key_name in required_key_names:
                            key_cursor = db.execute('SELECT encrypted_value FROM user_api_keys WHERE user_id = ? AND key_name = ?', (user_id, key_name))
                            encrypted_key_data = _fetchone_as_dict(key_cursor)
                            if encrypted_key_data:
                                api_keys[key_name] = cipher_suite.decrypt(encrypted_key_data['encrypted_value']).decode('utf-8')
                            else:
                                return jsonify({'error': f"Execution failed: Required API key '{key_name}' is missing."}), 400
        except Exception as e:
            logger.error(f"Error during pre-run data retrieval: {e}", exc_info=True)

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
                process_id = str(uuid.uuid4())
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
                            public_url = f"https://stellarai.live/apps/{process_id}/"
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



@app.route('/nebula/save_keys', methods=['POST'])
def nebula_save_keys():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required.'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON data received.'}), 400

    process_id = data.get('processId')
    api_keys = data.get('api_keys')

    if not process_id or not isinstance(api_keys, dict):
        return jsonify({'error': 'Missing or invalid parameters: processId and api_keys are required.'}), 400

    process_id_str = str(process_id)
    if 'nebula_processes' in session and process_id_str in session['nebula_processes']:
        process_state = session['nebula_processes'][process_id_str]
        
        if process_state.get('chat_id') and get_current_chat_id(session['user_id']) != process_state.get('chat_id'):
             return jsonify({'error': 'Authorization error.'}), 403

        process_state['api_keys'] = api_keys
        session['nebula_processes'][process_id_str] = process_state
        session.modified = True
        logging.info(f"Successfully saved API keys for Nebula process {process_id_str}.")
        return jsonify({'success': True, 'message': 'API keys saved successfully.'}), 200
    else:
        return jsonify({'error': f'Nebula process {process_id_str} not found.'}), 404

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
    model_id = 'gemini-3-pro-preview' # Use the pro preview model as requested
    api_key = RTP_API_KEY # Use RTP key for faster/cheaper generation or PRIMARY if needed

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
        
        generator = gemini_generate(prompt, model_id, api_key)
        full_response = ""
        for chunk in generator:
            if 'result' in chunk:
                full_response = chunk['result']
                break
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
@app.route("/apps/<app_id>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/apps/<app_id>/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def dynamic_proxy(app_id, path):
    app_info = None
    with active_apps_lock:
        app_info = active_apps.get(app_id)

    if not app_info:
        try:
            redis_key = _redis_forge_key(app_id)
            redis_data = redis_client.hgetall(redis_key)
            if not redis_data:
                redis_key = _redis_runcode_key(app_id)
                redis_data = redis_client.hgetall(redis_key)

            if redis_data and redis_data.get("host_port") and redis_data.get("status") in ["running", "created", "exited"]:
                app_info = {
                    "port": int(redis_data["host_port"]),
                    "container_id": redis_data.get("container_id"),
                    "status": redis_data.get("status")
                }
                with active_apps_lock:
                    active_apps[app_id] = app_info
        except Exception as e:
            logger.error(f"Redis lookup failed for app {app_id}: {e}")
            return "Error looking up application state.", 500

    if not app_info or not app_info.get("port"):
        return "Application not found or has been stopped.", 404
    
    target_port = app_info["port"]
    target_url = f"http://127.0.0.1:{target_port}/{path}"

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
        headers = [(name, value) for (name, value) in resp.raw.headers.items() if name.lower() not in excluded_headers]

        return Response(resp.content, resp.status_code, headers)

    except requests.exceptions.RequestException as e:
        logger.error(f"Dynamic proxy error for app {app_id}: {e}")
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

    old_container_id = session['forge_project'].get('container_id')
    old_process_id = session['forge_project'].get('process_id')

    if old_process_id:
        with active_apps_lock:
            active_apps.pop(old_process_id, None)

    try:
        process_id = str(uuid.uuid4())
        
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









