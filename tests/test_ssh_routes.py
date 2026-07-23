import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from app import app, redis_client

# Retrieve MockRedisClass from the instance type to avoid dist-packages import collision
MockRedisClass = type(redis_client)


# Dynamically add missing redis methods to MockRedisClass if not present
if not hasattr(MockRedisClass, 'decr'):
    def decr(self, name, amount=1):
        val = self.get(name)
        if val is None:
            val = -amount
        else:
            val = int(val) - amount
        self.set(name, str(val))
        return val
    MockRedisClass.decr = decr

if not hasattr(MockRedisClass, 'pipeline'):
    class MockPipeline:
        def __init__(self, client):
            self.client = client
            self.ops = []
        def incr(self, name):
            self.ops.append(('incr', name))
            return self
        def expire(self, name, time):
            return self
        def execute(self):
            for op, name in self.ops:
                if op == 'incr':
                    val = self.client.get(name)
                    if val is None:
                        val = 1
                    else:
                        val = int(val) + 1
                    self.client.set(name, str(val))
            return []
    def pipeline(self):
        return MockPipeline(self)
    MockRedisClass.pipeline = pipeline



@pytest.fixture(autouse=True)
def clear_redis():
    """Ensure Redis mock is cleared between test cases for independence."""
    if hasattr(redis_client, 'store'):
        redis_client.store.clear()
    yield
    if hasattr(redis_client, 'store'):
        redis_client.store.clear()


# ==============================================================================
# /auth/ssh Route Tests
# ==============================================================================

# Assert that an unauthenticated request redirects to home with redirect param.
def test_ssh_auth_page_logged_out_redirects_to_login(client):
    response = client.get('/auth/ssh')
    assert response.status_code == 302
    assert '/?redirect=/auth/ssh' in response.headers['Location']


# Assert that a user on the waitlist (not approved) gets waitlist HTML.
def test_ssh_auth_page_not_approved_renders_waitlist(client):
    from app import get_db
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (99, "unapproved@gmail.com", "Unapproved", "user", 0)')
        db.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = 99
        sess['username'] = "unapproved@gmail.com"
        sess['is_approved'] = False

    response = client.get('/auth/ssh')
    assert response.status_code == 200
    # Should display the waitlist template
    assert b'waitlist' in response.data or b'Waitlist' in response.data


# Assert that an approved user gets the SSH authentication TUI instruction page.
def test_ssh_auth_page_approved_renders_ssh_instructions(auth_client):
    # auth_client has an approved admin user in session (user_id=1, is_approved=True)
    response = auth_client.get('/auth/ssh')
    assert response.status_code == 200
    assert b'Web Terminal' in response.data
    assert b'deploymentList' in response.data


# Assert that a user whose session says unapproved but database is approved gets auto-promoted.
def test_ssh_auth_page_approved_in_db_auto_promotes_session(client):
    from app import get_db
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (98, "approved@gmail.com", "Approved", "user", 1)')
        db.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = 98
        sess['username'] = "approved@gmail.com"
        sess['is_approved'] = False

    response = client.get('/auth/ssh')
    assert response.status_code == 200
    assert b'Web Terminal' in response.data
    
    # Session is_approved should be updated to True
    with client.session_transaction() as sess:
        assert sess['is_approved'] is True


# ==============================================================================
# /api/ssh/generate-code Route Tests
# ==============================================================================

# Assert that generating a code without auth returns 401.
def test_ssh_generate_code_logged_out_returns_unauthorized(client):
    response = client.post('/api/ssh/generate-code')
    assert response.status_code == 401


# Assert that waitlist users are denied access to code generation.
def test_ssh_generate_code_not_approved_returns_forbidden(client):
    from app import get_db
    with client.application.app_context():
        db = get_db()
        db.execute('INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) VALUES (99, "unapproved@gmail.com", "Unapproved", "user", 0)')
        db.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = 99
        sess['username'] = "unapproved@gmail.com"
        sess['is_approved'] = False

    response = client.post('/api/ssh/generate-code')
    assert response.status_code == 403


# Assert that an approved user can successfully generate an 8-character code.
def test_ssh_generate_code_approved_generates_valid_code(auth_client):
    response = auth_client.post('/api/ssh/generate-code')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert 'code' in data
    
    # Expected format: XXXX-XXXX
    code = data['code']
    assert len(code) == 9
    assert '-' in code
    
    # Verify it is saved in Redis
    raw_code = code.replace('-', '')
    redis_key = f'ssh_auth_code:{raw_code}'
    assert redis_client.exists(redis_key)
    
    # Verify active code count in Redis is 1
    user_code_key = 'ssh_auth_code:user:1'
    assert int(redis_client.get(user_code_key)) == 1


# Assert that exceeding the max code limit returns a rate limit error.
def test_ssh_generate_code_rate_limit_exceeded_returns_429(auth_client):
    # Manually pre-populate 5 active codes for user 1
    redis_client.set('ssh_auth_code:user:1', 5)
    
    response = auth_client.post('/api/ssh/generate-code')
    assert response.status_code == 429
    data = json.loads(response.data)
    assert 'Too many active codes' in data['error']


# Assert that a code collision gracefully handles failure and returns 500 when exhausted.
def test_ssh_generate_code_collision_exhaustion_returns_500(auth_client):
    # Mock redis_client.exists to always say the code exists (simulating collision)
    with patch.object(redis_client, 'exists', return_value=True):
        response = auth_client.post('/api/ssh/generate-code')
        assert response.status_code == 500
        data = json.loads(response.data)
        assert 'Could not generate a unique code' in data['error']


# ==============================================================================
# /api/ssh/verify-code Route Tests
# ==============================================================================

# Assert that an incorrect gateway secret is rejected with 403.
def test_ssh_verify_code_invalid_secret_returns_forbidden(client):
    response = client.post('/api/ssh/verify-code', json={
        'secret': 'wrong-secret',
        'code': 'ABCDEF'
    })
    assert response.status_code == 403
    data = json.loads(response.data)
    assert data['valid'] is False
    assert data['error'] == 'Unauthorized'


# Assert that verify-code IP rate limit rejects requests once threshold is reached.
def test_ssh_verify_code_rate_limit_exceeded_returns_429(client):
    # Set verification failure count to 10 for IP
    redis_client.set('ssh_verify_fail:127.0.0.1', 10)
    
    gateway_secret = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')
    response = client.post('/api/ssh/verify-code', json={
        'secret': gateway_secret,
        'code': 'ABCDEF'
    }, environ_base={'REMOTE_ADDR': '127.0.0.1'})
    
    assert response.status_code == 429
    data = json.loads(response.data)
    assert data['valid'] is False
    assert 'Too many failed attempts' in data['error']


# Assert that empty or malformed code formats return valid: False.
def test_ssh_verify_code_malformed_code_returns_invalid(client):
    gateway_secret = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')
    
    # Missing code
    response = client.post('/api/ssh/verify-code', json={
        'secret': gateway_secret
    })
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['valid'] is False

    # Short code
    response = client.post('/api/ssh/verify-code', json={
        'secret': gateway_secret,
        'code': 'ABC'
    })
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['valid'] is False


# Assert that a code not in Redis fails verification and increments rate limit counter.
def test_ssh_verify_code_non_existent_code_fails_and_increments_fails(client):
    gateway_secret = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')
    
    response = client.post('/api/ssh/verify-code', json={
        'secret': gateway_secret,
        'code': 'NONEXIST'
    }, environ_base={'REMOTE_ADDR': '127.0.0.1'})
    
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['valid'] is False
    
    # Failure count should be incremented
    assert int(redis_client.get('ssh_verify_fail:127.0.0.1')) == 1


# Assert that a valid code successfully authenticates and returns user details.
def test_ssh_verify_code_valid_code_succeeds(client):
    gateway_secret = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')
    
    # Seed a valid code in Redis
    code_data = json.dumps({
        'user_id': 42,
        'username': 'sshuser@gmail.com',
        'display_name': 'SSH User',
        'created_at': time.time()
    })
    redis_client.set('ssh_auth_code:ABCDEFGH', code_data)
    redis_client.set('ssh_auth_code:user:42', 1)
    
    response = client.post('/api/ssh/verify-code', json={
        'secret': gateway_secret,
        'code': 'ABCD-EFGH'  # verify with hyphen
    })
    
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['valid'] is True
    assert data['user_id'] == 42
    assert data['username'] == 'sshuser@gmail.com'
    assert data['display_name'] == 'SSH User'
    
    # Code should be consumed (deleted from Redis)
    assert not redis_client.exists('ssh_auth_code:ABCDEFGH')
    # User code counter should be decremented and deleted
    assert not redis_client.exists('ssh_auth_code:user:42')


# Assert that verification handles malformed JSON payload in Redis gracefully.
def test_ssh_verify_code_malformed_redis_data_fails_gracefully(client):
    gateway_secret = os.environ.get('SSH_GATEWAY_SECRET', 'stellar-ssh-internal-2024')
    
    # Seed malformed data
    redis_client.set('ssh_auth_code:ABCDEFGH', 'not-a-json-string')
    
    response = client.post('/api/ssh/verify-code', json={
        'secret': gateway_secret,
        'code': 'ABCDEFGH'
    })
    
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['valid'] is True
    assert data['user_id'] is None
    assert data['username'] is None
    assert data['display_name'] is None
