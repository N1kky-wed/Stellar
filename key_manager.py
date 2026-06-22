import os
import re
import time
import hashlib
import logging
import datetime
from threading import Lock
import redis

# Setup logger for key management
logger = logging.getLogger("key_manager")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# -------------------------------------------------------------
# LAZY REDIS CLIENT FOR STATE SYNC ACROSS GUNICORN WORKERS
# -------------------------------------------------------------
class LazyRedis:
    def __init__(self, host='localhost', port=6379, db=0, decode_responses=True):
        self.host = host
        self.port = port
        self.db = db
        self.decode_responses = decode_responses
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = redis.StrictRedis(
                host=self.host,
                port=self.port,
                db=self.db,
                decode_responses=self.decode_responses
            )
        return self._client

    def __getattr__(self, name):
        return getattr(self.client, name)

redis_client = LazyRedis(host='localhost', port=6379, db=0, decode_responses=True)

# -------------------------------------------------------------
# DEFINE API MODEL RATE AND RESERVE LIMITS
# -------------------------------------------------------------
# Reordering and reserves: Keep 5 requests in reserve for RPD.
# RPM limits: limit to RPM - 1 (e.g. 4 instead of 5, 14 instead of 15) to keep safe.
def get_limits(model_name):
    if not model_name:
        return {"rpm": 4, "rpd": 15}
    
    m = model_name.lower()
    if "3.5-flash" in m or "2.5-flash" in m or "gemini-3-flash" in m:
        return {"rpm": 4, "rpd": 15}  # RPM: 5-1, RPD: 20-5
    elif "gemma" in m:
        return {"rpm": 14, "rpd": 1495}  # RPM: 15-1, RPD: 1500-5
    elif "3.1-flash-lite" in m or "flash-lite" in m:
        return {"rpm": 14, "rpd": 495}   # RPM: 15-1, RPD: 500-5
    elif "image-preview" in m:
        return {"rpm": 4, "rpd": 15}
    else:
        return {"rpm": 4, "rpd": 15}

def get_pacific_day_bucket():
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        year = now_utc.year
        march_1 = datetime.datetime(year, 3, 1, tzinfo=datetime.timezone.utc)
        march_1_dow = march_1.weekday()
        days_to_first_sunday = (6 - march_1_dow) % 7
        dst_start = march_1 + datetime.timedelta(days=days_to_first_sunday + 7)
        nov_1 = datetime.datetime(year, 11, 1, tzinfo=datetime.timezone.utc)
        nov_1_dow = nov_1.weekday()
        days_to_nov_sunday = (6 - nov_1_dow) % 7
        dst_end = nov_1 + datetime.timedelta(days=days_to_nov_sunday)
        is_dst = dst_start <= now_utc < dst_end
        pacific_offset = datetime.timedelta(hours=-7) if is_dst else datetime.timedelta(hours=-8)
        now_pacific = now_utc + pacific_offset
        return now_pacific.strftime("%Y-%m-%d")
    except Exception:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def get_seconds_until_pacific_midnight():
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        year = now_utc.year
        march_1 = datetime.datetime(year, 3, 1, tzinfo=datetime.timezone.utc)
        march_1_dow = march_1.weekday()
        days_to_first_sunday = (6 - march_1_dow) % 7
        dst_start = march_1 + datetime.timedelta(days=days_to_first_sunday + 7)
        nov_1 = datetime.datetime(year, 11, 1, tzinfo=datetime.timezone.utc)
        nov_1_dow = nov_1.weekday()
        days_to_nov_sunday = (6 - nov_1_dow) % 7
        dst_end = nov_1 + datetime.timedelta(days=days_to_nov_sunday)
        is_dst = dst_start <= now_utc < dst_end
        pacific_offset = datetime.timedelta(hours=-7) if is_dst else datetime.timedelta(hours=-8)
        now_pacific = now_utc + pacific_offset
        tomorrow_pacific = datetime.datetime(now_pacific.year, now_pacific.month, now_pacific.day, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=1)
        seconds_until_midnight = (tomorrow_pacific - now_pacific).total_seconds()
        return max(int(seconds_until_midnight), 60)
    except Exception as e:
        logger.error(f"Error calculating Pacific midnight offset: {e}")
        return 14400

def parse_quota_block_duration(error_msg):
    err_lower = error_msg.lower()
    if ('minute' in err_lower or 'queries per minute' in err_lower or
        'rpm' in err_lower or 'tpm' in err_lower or 'queriesperminute' in err_lower):
        return 61, 'RPM'
    elif ('requestsperday' in err_lower or 'requests per day' in err_lower or
          'daily' in err_lower or 'perday' in err_lower or 'projectpermodel-freetier' in err_lower or
          'exceeded your current quota' in err_lower or 'billing details' in err_lower or 'quota/rate limits' in err_lower):
        duration = get_seconds_until_pacific_midnight()
        return duration, 'RPD'
    elif ('overloaded' in err_lower or '503' in err_lower or 'service unavailable' in err_lower or 'service_unavailable' in err_lower):
        return 600, 'OVERLOAD'
    elif ('500' in err_lower or 'internal error' in err_lower or 'internal_error' in err_lower):
        return 10, 'INTERNAL'
    return 61, 'RPM'

# -------------------------------------------------------------
# GLOBAL KEY MANAGER CLASS
# -------------------------------------------------------------
class GlobalKeyManager:
    def __init__(self):
        self.lock = Lock()
        self.blocked_until = {}
        self.block_reason = {}
        # Memory fallback counters
        self.local_rpm = {}
        self.local_rpd = {}
        # Global model overload tracking
        self.model_overloaded = {}

    def _get_redis_keys(self, key_val, model_id):
        key_hash = hashlib.sha256(key_val.encode('utf-8')).hexdigest()
        scope = model_id if model_id is not None else "global"
        return f"stellar:blocked_until:{key_hash}:{scope}", f"stellar:block_reason:{key_hash}:{scope}"

    def block_key(self, key_val, model_id, duration_seconds, reason='RPM'):
        if reason == 'INVALID':
            model_id = None
        if reason == 'OVERLOAD' and model_id:
            duration_seconds = 600
            logger.warning("Model %s is overloaded. Blocking this model for ALL keys for %ds.", model_id, duration_seconds)
            with self.lock:
                self.model_overloaded[model_id] = time.time() + duration_seconds
            try:
                redis_client.setex(f"stellar:model_overloaded:{model_id}", int(duration_seconds), str(time.time() + duration_seconds))
            except Exception as e:
                logger.error(f"Error writing model overload to Redis: {e}")
        key_hash = hash(key_val) if key_val else 0
        logger.warning("API key blocked hash=%s model=%s duration_sec=%s reason=%s", key_hash, model_id, duration_seconds, reason)
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
        # 0. Check if model is globally overloaded (all keys blocked for this model)
        if model_id:
            try:
                overloaded_val = redis_client.get(f"stellar:model_overloaded:{model_id}")
                if overloaded_val:
                    if time.time() < float(overloaded_val):
                        return True, 'OVERLOAD'
            except Exception:
                pass
            with self.lock:
                if time.time() < self.model_overloaded.get(model_id, 0):
                    return True, 'OVERLOAD'

        # 1. First check if key has crossed rate limits or reserve limits in Redis
        try:
            limits = get_limits(model_id)
            current_time = time.time()
            current_minute = int(current_time // 60)
            current_day = get_pacific_day_bucket()
            key_hash = hashlib.sha256(key_val.encode('utf-8')).hexdigest()
            
            rpm_key = f"stellar:count_rpm:{key_hash}:{model_id}:{current_minute}"
            rpd_key = f"stellar:count_rpd:{key_hash}:{model_id}:{current_day}"
            
            rpm_val = redis_client.get(rpm_key)
            if rpm_val and int(rpm_val) >= limits["rpm"]:
                return True, 'RPM'
                
            rpd_val = redis_client.get(rpd_key)
            if rpd_val and int(rpd_val) >= limits["rpd"]:
                return True, 'RPD'
        except Exception:
            pass

        # 2. Check traditional Redis blocks
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

        # 3. Check local memory blocks and local counters
        with self.lock:
            current_time = time.time()
            current_minute = int(current_time // 60)
            current_day = get_pacific_day_bucket()
            limits = get_limits(model_id)
            
            if self.local_rpm.get((key_val, model_id, current_minute), 0) >= limits["rpm"]:
                return True, 'RPM'
            if self.local_rpd.get((key_val, model_id, current_day), 0) >= limits["rpd"]:
                return True, 'RPD'
                
            blocked_time = self.blocked_until.get((key_val, model_id), 0)
            if time.time() < blocked_time:
                return True, self.block_reason.get((key_val, model_id), 'RPM')
            if model_id is not None:
                blocked_time_global = self.blocked_until.get((key_val, None), 0)
                if time.time() < blocked_time_global:
                    return True, self.block_reason.get((key_val, None), 'RPM')
        return False, None

    def record_request(self, key_val, model_id):
        if not key_val:
            return
        limits = get_limits(model_id)
        current_time = time.time()
        current_minute = int(current_time // 60)
        current_day = get_pacific_day_bucket()

        # Redis implementation
        try:
            key_hash = hashlib.sha256(key_val.encode('utf-8')).hexdigest()
            rpm_key = f"stellar:count_rpm:{key_hash}:{model_id}:{current_minute}"
            rpd_key = f"stellar:count_rpd:{key_hash}:{model_id}:{current_day}"
            
            rpm_count = redis_client.incr(rpm_key)
            if rpm_count == 1:
                redis_client.expire(rpm_key, 80)
                
            rpd_count = redis_client.incr(rpd_key)
            if rpd_count == 1:
                redis_client.expire(rpd_key, 90000)
                
            if rpm_count >= limits["rpm"]:
                remaining = 60 - int(current_time % 60)
                block_duration = max(10, remaining)
                self.block_key(key_val, model_id, block_duration, reason='RPM')
                logger.warning("Key RPM limit hit (%d/%d) for model %s. Blocking for %ds", rpm_count, limits["rpm"], model_id, block_duration)
                
            if rpd_count >= limits["rpd"]:
                block_duration = get_seconds_until_pacific_midnight()
                self.block_key(key_val, model_id, block_duration, reason='RPD')
                logger.warning("Key RPD reserve limit hit (%d/%d) for model %s. Blocking for %ds", rpd_count, limits["rpd"], model_id, block_duration)
            return
        except Exception as e:
            logger.error(f"Error incrementing request counts in Redis: {e}")

        # Local memory fallback
        with self.lock:
            for k in list(self.local_rpm.keys()):
                if k[2] != current_minute:
                    del self.local_rpm[k]
            for k in list(self.local_rpd.keys()):
                if k[2] != current_day:
                    del self.local_rpd[k]
                    
            rpm_key_local = (key_val, model_id, current_minute)
            self.local_rpm[rpm_key_local] = self.local_rpm.get(rpm_key_local, 0) + 1
            rpm_count = self.local_rpm[rpm_key_local]
            
            rpd_key_local = (key_val, model_id, current_day)
            self.local_rpd[rpd_key_local] = self.local_rpd.get(rpd_key_local, 0) + 1
            rpd_count = self.local_rpd[rpd_key_local]
            
            if rpm_count >= limits["rpm"]:
                remaining = 60 - int(current_time % 60)
                block_duration = max(10, remaining)
                self.blocked_until[(key_val, model_id)] = time.time() + block_duration
                self.block_reason[(key_val, model_id)] = 'RPM'
                
            if rpd_count >= limits["rpd"]:
                block_duration = get_seconds_until_pacific_midnight()
                self.blocked_until[(key_val, model_id)] = time.time() + block_duration
                self.block_reason[(key_val, model_id)] = 'RPD'

    def get_key_blocks(self, key_val, models):
        blocks = {}
        global_blocked, global_reason = self.is_key_blocked(key_val, None)
        if global_blocked:
            remaining = 0
            g_blocked_until = 0.0
            try:
                k_until, _ = self._get_redis_keys(key_val, None)
                blocked_until_val = redis_client.get(k_until)
                if blocked_until_val:
                    g_blocked_until = float(blocked_until_val)
                    remaining = max(0.0, g_blocked_until - time.time())
            except Exception:
                pass
            if remaining == 0:
                with self.lock:
                    blocked_time = self.blocked_until.get((key_val, None), 0)
                    remaining = max(0.0, blocked_time - time.time())
                    g_blocked_until = time.time() + remaining
            blocks["global"] = {
                "blocked": True,
                "reason": global_reason or 'RPM',
                "remaining_seconds": int(remaining),
                "blocked_until": g_blocked_until
            }
        else:
            blocks["global"] = {
                "blocked": False,
                "reason": None,
                "remaining_seconds": 0,
                "blocked_until": 0.0
            }

        for model in models:
            model_blocked = False
            model_reason = None
            model_remaining = 0.0
            model_blocked_until = 0.0

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
                            model_blocked_until = blocked_until_time
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
                        model_blocked_until = blocked_time

            effective_blocked, effective_reason = self.is_key_blocked(key_val, model)
            if effective_blocked:
                if model_blocked:
                    blocks[model] = {
                        "blocked": True,
                        "reason": model_reason or effective_reason or 'RPM',
                        "remaining_seconds": int(model_remaining),
                        "blocked_until": model_blocked_until
                    }
                else:
                    limits = get_limits(model)
                    if effective_reason == 'RPM':
                        rem = 60 - int(time.time() % 60)
                    else:
                        rem = get_seconds_until_pacific_midnight()
                    blocks[model] = {
                        "blocked": True,
                        "reason": effective_reason,
                        "remaining_seconds": int(rem),
                        "blocked_until": time.time() + rem
                    }
            else:
                blocks[model] = {
                    "blocked": False,
                    "reason": None,
                    "remaining_seconds": 0,
                    "blocked_until": 0.0
                }
        return blocks

KEY_MANAGER = GlobalKeyManager()

# -------------------------------------------------------------
# DYNAMIC LOAD OF API KEYS FROM keys.env
# -------------------------------------------------------------
PRIMARY_API_KEY = None
BACKUP_API_KEYS = []

def load_keys():
    global PRIMARY_API_KEY, BACKUP_API_KEYS
    primary = None
    backups = {}
    keys_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.env")
    if os.path.exists(keys_env_path):
        with open(keys_env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r'^(PRIMARY_API_KEY|BACKUP_API_KEY_\d+)="?([^"\s]+)"?$', line)
                if match:
                    name = match.group(1)
                    val = match.group(2)
                    if name == "PRIMARY_API_KEY":
                        primary = val
                    else:
                        try:
                            num = int(name.split("_")[-1])
                            backups[num] = val
                        except ValueError:
                            pass
    PRIMARY_API_KEY = primary or os.getenv("PRIMARY_API_KEY")
    sorted_backup_keys = [backups[k] for k in sorted(backups.keys())]
    if not sorted_backup_keys:
        i = 1
        while True:
            val = os.getenv(f"BACKUP_API_KEY_{i}")
            if val:
                sorted_backup_keys.append(val)
                i += 1
            else:
                break
    BACKUP_API_KEYS = sorted_backup_keys
    logger.info("Keys successfully loaded from keys.env. Total Backups: %d", len(BACKUP_API_KEYS))

# Initialize keys on import
load_keys()

# -------------------------------------------------------------
# AUTO-INSTRUMENT google-genai Client TO TRACK AND BLOCK KEYS
# -------------------------------------------------------------
try:
    from google import genai
    original_client_init = genai.Client.__init__

    def wrapped_client_init(self, *args, **kwargs):
        original_client_init(self, *args, **kwargs)
        api_key = kwargs.get('api_key') or (args[0] if args else None)
        if not api_key:
            api_key = os.environ.get("GEMINI_API_KEY") or PRIMARY_API_KEY
        
        # Wrap models.generate_content
        if hasattr(self, 'models') and hasattr(self.models, 'generate_content'):
            original_generate_content = self.models.generate_content
            def wrapped_generate_content(*args, **kwargs):
                model = kwargs.get('model') or (args[0] if args else None)
                KEY_MANAGER.record_request(api_key, model)
                return original_generate_content(*args, **kwargs)
            self.models.generate_content = wrapped_generate_content

        # Wrap models.count_tokens
        if hasattr(self, 'models') and hasattr(self.models, 'count_tokens'):
            original_count_tokens = self.models.count_tokens
            def wrapped_count_tokens(*args, **kwargs):
                model = kwargs.get('model') or (args[0] if args else None)
                KEY_MANAGER.record_request(api_key, model)
                return original_count_tokens(*args, **kwargs)
            self.models.count_tokens = wrapped_count_tokens

        # Wrap chats.create
        if hasattr(self, 'chats') and hasattr(self.chats, 'create'):
            original_chats_create = self.chats.create
            def wrapped_chats_create(*args, **kwargs):
                model = kwargs.get('model') or (args[0] if args else None)
                chat_obj = original_chats_create(*args, **kwargs)
                if chat_obj and hasattr(chat_obj, 'send_message'):
                    original_send_message = chat_obj.send_message
                    def wrapped_send_message(*args, **kwargs):
                        KEY_MANAGER.record_request(api_key, model)
                        return original_send_message(*args, **kwargs)
                    chat_obj.send_message = wrapped_send_message
                return chat_obj
            self.chats.create = wrapped_chats_create

    genai.Client.__init__ = wrapped_client_init
    logger.info("Successfully monkeypatched google.genai.Client to track request rates.")
except Exception as e:
    logger.error("Failed to monkeypatch google.genai.Client: %s", e)
