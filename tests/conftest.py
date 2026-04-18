import sys
import os
import pytest
from unittest.mock import MagicMock, patch

# Add project root to sys.path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# --- Mocks before import ---
# We mock redis and docker because they are instantiated or used at module level
# or we want to prevent any real connection attempts during import/setup.

# Mock redis
# Flask-Session checks isinstance(client, redis.Redis), so we need a real class
class MockRedisClass:
    def __init__(self, *args, **kwargs):
        self.store = {}
    def ping(self):
        return True
    def get(self, name):
        return self.store.get(name)
    def set(self, name, value, ex=None, px=None, nx=False, xx=False):
        self.store[name] = value
        return True
    def delete(self, *names):
        for name in names:
            self.store.pop(name, None)
        return True
    def hget(self, name, key):
        if name in self.store and isinstance(self.store[name], dict):
             return self.store[name].get(key)
        return None
    def hset(self, name, key=None, value=None, mapping=None):
        if name not in self.store:
            self.store[name] = {}
        if mapping:
            self.store[name].update(mapping)
        if key and value:
            self.store[name][key] = value
        return 1
    def hgetall(self, name):
        return self.store.get(name, {})
    def publish(self, channel, message):
        pass
    def pubsub(self, **kwargs):
        mock_pubsub = MagicMock()
        mock_pubsub.listen.return_value = []
        return mock_pubsub

mock_redis = MagicMock()
mock_redis.Redis = MockRedisClass
mock_redis.StrictRedis = MockRedisClass
# Also Flask-Session might check redis.from_url
mock_redis.from_url = MagicMock(return_value=MockRedisClass())
sys.modules['redis'] = mock_redis

# Mock docker
mock_docker = MagicMock()
sys.modules['docker'] = mock_docker

# Mock twilio - preventing any accidental network calls or cred checks
mock_twilio = MagicMock()
sys.modules['twilio'] = mock_twilio
sys.modules['twilio.rest'] = MagicMock()
sys.modules['twilio.base'] = MagicMock()
sys.modules['twilio.base.exceptions'] = MagicMock()

# We do NOT mock google.* modules here because they are complex namespace packages.
# We will rely on installed packages and mock the Client classes later if needed.
# However, if app.py or other modules try to use them at import time, we might need to patch.
# app.py does NOT seem to use google.genai at import time (only inside functions).

# Mock sqlitecloud just in case
sys.modules['sqlitecloud'] = MagicMock()

# --- End Mocks ---

# Setup Env Vars
os.environ['TAVILY_API_KEY'] = 'test_key'
os.environ['Admin'] = 'admin'
os.environ['RTP_API_KEY'] = 'test_key'
os.environ['SEARCH_API_KEY'] = 'test_key'
os.environ['PRIMARY_API_KEY'] = 'test_key'

# Import app
try:
    from app import app
except ImportError as e:
    print(f"Failed to import app: {e}")
    raise

@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SESSION_TYPE'] = 'filesystem'
    app.config['SESSION_PERMANENT'] = False

    import tempfile
    db_fd, db_path = tempfile.mkstemp()

    with patch('app.DATABASE_NAME', db_path):
        with app.app_context():
            from app import initialize_database
            initialize_database()

        with app.test_client() as client:
            with app.app_context():
                yield client

    os.close(db_fd)
    os.unlink(db_path)

@pytest.fixture
def mock_docker_client():
    return mock_docker.from_env.return_value

@pytest.fixture
def mock_redis_client():
    return mock_redis.StrictRedis.return_value
