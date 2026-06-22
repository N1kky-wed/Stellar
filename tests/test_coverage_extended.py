"""
test_coverage_extended.py
=========================
Extended coverage tests targeting previously uncovered code paths in app.py, specifically:

  • parse_quota_block_duration  – quota/rate-limit error classification
  • get_seconds_until_pacific_midnight – DST-aware Pacific-midnight calculation
  • get_fallback_chain           – model-fallback ordering
  • GlobalKeyManager             – block_key / is_key_blocked / get_key_blocks
  • parse_log_line               – orchestrator log-line parser
  • sentinel_log_error route     – JS error telemetry ingestion
  • sentinel_status route        – healing status poll
  • sentinel_stream route        – SSE stream auth / ownership guard
  • orchestrator_quota_info      – quota dashboard data endpoint
  • orchestrator_refresh_quota   – quota refresh endpoint (error path)
  • send_agent_message           – admin DM / group message dispatch
  • list_agent_tasks             – task listing when DB absent or present
  • create_agent_task            – task creation with/without assignee
  • resolve_agent_task           – task resolution flow
  • get_agent_dms                – DM batch-fetch with resolved-task detection
"""

import json
import os
import sqlite3
import tempfile
import datetime
import pytest
from unittest.mock import patch, MagicMock

# Real sqlite3.connect preserved before any monkeypatching in individual tests
_real_sqlite3_connect = sqlite3.connect
_real_os_path_exists = os.path.exists


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator_memory_db(db_path):
    """Return a temporary memory.db with the agent_messages and agent_tasks tables."""
    conn = _real_sqlite3_connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY,
            channel TEXT,
            thread_id TEXT,
            sender_id TEXT,
            recipient_id TEXT,
            content TEXT,
            message_type TEXT,
            ref_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id INTEGER PRIMARY KEY,
            title TEXT,
            description TEXT,
            created_by TEXT,
            assigned_to TEXT,
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'normal',
            related_file TEXT,
            created_at TEXT,
            updated_at TEXT,
            resolved_by TEXT,
            resolved_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# ===========================================================================
# parse_quota_block_duration
# ===========================================================================

def test_parse_quota_block_duration_rpm_minute_keyword():
    """RPM-style error containing 'minute' should return 61s / 'RPM' reason."""
    from app import parse_quota_block_duration
    duration, reason = parse_quota_block_duration("Quota exceeded: requests per minute limit hit")
    assert duration == 61
    assert reason == 'RPM'


def test_parse_quota_block_duration_rpm_tpm_keyword():
    """'tpm' in error string maps to the minute-limit branch (61 s, RPM)."""
    from app import parse_quota_block_duration
    duration, reason = parse_quota_block_duration("Rate exceeded: TPM limit reached")
    assert duration == 61
    assert reason == 'RPM'


def test_parse_quota_block_duration_rpd_daily_keyword():
    """'requestsperday' in error string triggers the daily-quota branch (RPD)."""
    from app import parse_quota_block_duration
    with patch('app.get_seconds_until_pacific_midnight', return_value=7200):
        duration, reason = parse_quota_block_duration("RESOURCE_EXHAUSTED: RequestsPerDay exceeded")
    assert duration == 7200
    assert reason == 'RPD'


def test_parse_quota_block_duration_rpd_billing_keyword():
    """'billing details' error text triggers the daily-quota branch."""
    from app import parse_quota_block_duration
    with patch('app.get_seconds_until_pacific_midnight', return_value=3600):
        duration, reason = parse_quota_block_duration("You exceeded your current quota; update billing details")
    assert duration == 3600
    assert reason == 'RPD'


def test_parse_quota_block_duration_overload_503():
    """'503' or 'overloaded' errors should return 600s / 'OVERLOAD'."""
    from app import parse_quota_block_duration
    duration, reason = parse_quota_block_duration("503 Service Unavailable: model overloaded")
    assert duration == 600
    assert reason == 'OVERLOAD'


def test_parse_quota_block_duration_internal_500():
    """'internal error' text maps to the 500-error branch (10 s, INTERNAL)."""
    from app import parse_quota_block_duration
    duration, reason = parse_quota_block_duration("500 internal error during inference")
    assert duration == 10
    assert reason == 'INTERNAL'


def test_parse_quota_block_duration_fallback():
    """Unrecognised error strings fall back to 61 s / 'RPM'."""
    from app import parse_quota_block_duration
    duration, reason = parse_quota_block_duration("some completely unknown error message")
    assert duration == 61
    assert reason == 'RPM'


# ===========================================================================
# get_seconds_until_pacific_midnight
# ===========================================================================

def test_get_seconds_until_pacific_midnight_returns_positive_int():
    """
    The function should always return a positive integer representing
    seconds to the next Pacific-time midnight, even without mocking DST.
    """
    from app import get_seconds_until_pacific_midnight
    seconds = get_seconds_until_pacific_midnight()
    assert isinstance(seconds, int)
    assert seconds > 0
    # Must be at most ~86460 seconds (one day + some slack)
    assert seconds <= 86460


def test_get_seconds_until_pacific_midnight_exception_fallback():
    """If an exception occurs inside the function, it returns the 4-hour fallback."""
    from app import get_seconds_until_pacific_midnight
    with patch('app.datetime') as mock_dt:
        mock_dt.datetime.now.side_effect = RuntimeError("bad datetime")
        result = get_seconds_until_pacific_midnight()
    assert result == 14400


# ===========================================================================
# get_fallback_chain
# ===========================================================================

def test_get_fallback_chain_start_in_chain():
    """
    When start_model is in the predefined chain, the returned list begins
    at that model and includes all subsequent fallbacks.
    """
    from app import get_fallback_chain
    chain = get_fallback_chain("gemini-3-flash-preview")
    assert chain[0] == "gemini-3-flash-preview"
    assert "gemma-4-31b-it" in chain


def test_get_fallback_chain_start_at_beginning():
    """Starting at the first model returns the complete fallback chain."""
    from app import get_fallback_chain
    chain = get_fallback_chain("gemini-3.5-flash")
    assert chain[0] == "gemini-3.5-flash"
    assert len(chain) >= 2


def test_get_fallback_chain_unknown_model():
    """
    An unknown model is placed first and followed by 'gemma-4-31b-it',
    giving the caller a guaranteed last resort.
    """
    from app import get_fallback_chain
    chain = get_fallback_chain("some-unknown-model-xyz")
    assert chain[0] == "some-unknown-model-xyz"
    assert chain[-1] == "gemma-4-31b-it"


# ===========================================================================
# GlobalKeyManager
# ===========================================================================

def test_global_key_manager_block_and_is_blocked():
    """
    After blocking a key for a model, is_key_blocked should report it as
    blocked before the duration expires.
    """
    from key_manager import GlobalKeyManager
    mgr = GlobalKeyManager()
    with patch.object(mgr, '_get_redis_keys', return_value=('k_until', 'k_reason')):
        with patch('key_manager.redis_client') as mock_redis:
            mock_redis.setex.return_value = True
            mock_redis.get.return_value = None   # Redis returns nothing; fallback to memory
            mgr.block_key('fake-api-key', 'gemini-3.5-flash', duration_seconds=300, reason='RPM')
            is_blocked, reason = mgr.is_key_blocked('fake-api-key', 'gemini-3.5-flash')
    assert is_blocked is True
    assert reason == 'RPM'


def test_global_key_manager_block_with_invalid_reason_clears_model():
    """
    When reason='INVALID', model_id should be reset to None (global block),
    and the key should subsequently report as globally blocked.
    """
    from key_manager import GlobalKeyManager
    mgr = GlobalKeyManager()
    with patch.object(mgr, '_get_redis_keys', return_value=('k_until', 'k_reason')):
        with patch('key_manager.redis_client') as mock_redis:
            mock_redis.setex.return_value = True
            mock_redis.get.return_value = None
            mgr.block_key('invalid-key', 'gemini-3.5-flash', duration_seconds=60, reason='INVALID')
            # The key must now be blocked under model_id=None (global scope)
            is_blocked, reason = mgr.is_key_blocked('invalid-key', None)
    assert is_blocked is True


def test_global_key_manager_not_blocked_after_expiry():
    """
    A key that was blocked in the past (blocked_until in the past) should
    not be reported as blocked.
    """
    from key_manager import GlobalKeyManager
    import time
    mgr = GlobalKeyManager()
    # Inject an expired entry directly into the in-memory store
    mgr.blocked_until[('old-key', 'gemini-3.5-flash')] = time.time() - 10  # expired
    mgr.block_reason[('old-key', 'gemini-3.5-flash')] = 'RPM'

    with patch('key_manager.redis_client') as mock_redis:
        mock_redis.get.return_value = None   # Nothing in Redis
        is_blocked, _ = mgr.is_key_blocked('old-key', 'gemini-3.5-flash')
    assert is_blocked is False


def test_global_key_manager_redis_fallback_on_error():
    """
    If Redis raises an exception during is_key_blocked, the manager falls
    back to in-memory state without crashing.
    """
    from key_manager import GlobalKeyManager
    import time
    mgr = GlobalKeyManager()
    mgr.blocked_until[('api-key-x', 'gemini-3.5-flash')] = time.time() + 300
    mgr.block_reason[('api-key-x', 'gemini-3.5-flash')] = 'RPD'

    with patch('key_manager.redis_client') as mock_redis:
        mock_redis.get.side_effect = Exception("Redis connection refused")
        is_blocked, reason = mgr.is_key_blocked('api-key-x', 'gemini-3.5-flash')
    assert is_blocked is True
    assert reason == 'RPD'


def test_global_key_manager_get_key_blocks_returns_structure():
    """
    get_key_blocks should return a dict keyed by 'global' and each model,
    each with 'blocked', 'reason', and 'remaining_seconds'.
    """
    from key_manager import GlobalKeyManager
    mgr = GlobalKeyManager()
    with patch('key_manager.redis_client') as mock_redis:
        mock_redis.get.return_value = None
        blocks = mgr.get_key_blocks('some-key', ['gemini-3.5-flash', 'gemma-4-31b-it'])
    assert 'global' in blocks
    assert 'gemini-3.5-flash' in blocks
    assert 'gemma-4-31b-it' in blocks
    for scope in blocks.values():
        assert 'blocked' in scope
        assert 'remaining_seconds' in scope


def test_global_key_manager_model_overloaded():
    """
    If a key is blocked with OVERLOAD reason, it should mark that model as globally overloaded,
    blocking all other keys for that model.
    """
    from key_manager import GlobalKeyManager
    mgr = GlobalKeyManager()
    with patch('key_manager.redis_client') as mock_redis:
        mock_redis.setex.return_value = True
        mock_redis.get.return_value = None
        
        # Block key 1 on model gemini-3.5-flash with reason OVERLOAD
        mgr.block_key('key-1', 'gemini-3.5-flash', duration_seconds=600, reason='OVERLOAD')
        
        # Now check if key-2 is blocked on model gemini-3.5-flash (even though key-2 was never explicitly blocked)
        is_blocked, reason = mgr.is_key_blocked('key-2', 'gemini-3.5-flash')
        assert is_blocked is True
        assert reason == 'OVERLOAD'
        
        # Check if key-2 is NOT blocked on another model like gemini-3.1-flash-lite
        is_blocked_other, _ = mgr.is_key_blocked('key-2', 'gemini-3.1-flash-lite')
        assert is_blocked_other is False


# ===========================================================================
# parse_log_line
# ===========================================================================

def test_parse_log_line_system_message():
    """
    A log line that does NOT start with '[AgentSlug]' should be parsed
    as an Orchestrator system message.
    """
    from app import parse_log_line
    line = "2026-06-10 12:34:56,789 - INFO - [orchestrator.py:42] - Starting next agent run"
    result = parse_log_line(line)
    assert result is not None
    assert result['sender'] == 'Orchestrator'
    assert result['type'] == 'system'
    assert 'Starting next agent run' in result['content']
    assert result['level'] == 'INFO'


def test_parse_log_line_known_agent():
    """
    A log line whose message starts with '[newton]' is parsed as an
    agent message with the correct display name.
    """
    from app import parse_log_line
    line = "2026-06-10 13:00:00,000 - INFO - [main.py:10] - [Newton] PR opened successfully"
    result = parse_log_line(line)
    assert result is not None
    assert result['type'] == 'agent'
    assert 'Newton' in result['sender']
    assert 'PR opened successfully' in result['content']


def test_parse_log_line_unknown_agent_slug():
    """
    An unknown agent slug is capitalised and used as the sender name.
    """
    from app import parse_log_line
    line = "2026-06-11 09:00:00,000 - WARNING - [app.py:5] - [zorp] Some custom agent message"
    result = parse_log_line(line)
    assert result is not None
    assert result['type'] == 'agent'
    assert result['sender'] == 'Zorp'


def test_parse_log_line_malformed_returns_none():
    """
    A line that does not match the expected format should return None.
    """
    from app import parse_log_line
    result = parse_log_line("this is not a valid log line at all")
    assert result is None


def test_parse_log_line_all_known_agent_slugs():
    """
    All six agent slugs should resolve to their full display names.
    """
    from app import parse_log_line
    slugs_and_names = {
        'bolt': 'Bolt (Performance Engineer)',
        'sentinel': 'Sentinel (Security Engineer)',
        'palette': 'Palette (UI Engineer)',
        'newton': 'Newton (Test Engineer)',
        'lucios': 'Lucios (Observability Engineer)',
        'proton': 'Proton (Documentation Engineer)',
        'mercury': 'Mercury (Reliability Engineer)',
    }
    for slug, expected_name in slugs_and_names.items():
        line = f"2026-06-10 10:00:00,000 - INFO - [agent.py:1] - [{slug.capitalize()}] Hello from agent"
        result = parse_log_line(line)
        assert result is not None, f"parse_log_line returned None for slug '{slug}'"
        assert result['sender'] == expected_name, (
            f"Expected sender '{expected_name}' for slug '{slug}', got '{result['sender']}'"
        )


# ===========================================================================
# sentinel_log_error  (POST /api/sentinel/log_error)
# ===========================================================================

def test_sentinel_log_error_no_deployment_mapping(auth_client):
    """
    If the subdomain in the reported URL does not match any repo_history row,
    the endpoint returns 404 with 'No deployment mapping found'.
    """
    payload = {
        'url': 'https://nonexistent-subdomain.stellarai.live',
        'error': {
            'type': 'ReferenceError',
            'message': 'x is not defined',
            'stack': 'ReferenceError at line 1',
            'source': 'app.js',
            'line': 1,
        }
    }
    response = auth_client.post('/api/sentinel/log_error', json=payload)
    assert response.status_code == 404
    data = json.loads(response.data)
    assert 'No deployment mapping found' in data['error']


def test_sentinel_log_error_success_owner(auth_client):
    """
    When the caller is the owner of the deployment, log_backend_crash is
    called with trigger_heal=True and the endpoint returns a success payload.
    """
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO repo_history (id, user_id, project_name, process_id, status, subdomain, files_snapshot) "
            "VALUES (100, 1, 'TestProj', 'proc-100', 'running', 'myapp123', '[]')"
        )
        db.commit()

    with patch('app.log_backend_crash', return_value=42) as mock_log:
        payload = {
            'url': 'https://myapp123.stellarai.live',
            'error': {
                'type': 'TypeError',
                'message': 'Cannot read property of null',
                'stack': 'TypeError at main.js:10',
                'source': 'main.js',
                'line': 10,
            }
        }
        response = auth_client.post('/api/sentinel/log_error', json=payload)

    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'success'
    assert data['error_id'] == 42
    # Owner is authenticated — trigger_heal should be True
    mock_log.assert_called_once()
    call_kwargs = mock_log.call_args
    assert call_kwargs[1]['trigger_heal'] is True


def test_sentinel_log_error_non_owner_no_heal(client):
    """
    When an unauthenticated visitor reports an error for a deployment they
    do not own, trigger_heal must be False.
    """
    from app import get_db
    with client.application.app_context():
        db = get_db()
        # Create a second user who owns the app
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (50, 'owner@example.com', 'Owner', 'user', 1)"
        )
        db.execute(
            "INSERT OR IGNORE INTO repo_history (id, user_id, project_name, process_id, status, subdomain, files_snapshot) "
            "VALUES (101, 50, 'OwnerProj', 'proc-101', 'running', 'ownersapp', '[]')"
        )
        db.commit()

    # Do NOT set session — visitor is unauthenticated
    with patch('app.log_backend_crash', return_value=99) as mock_log:
        payload = {
            'url': 'https://ownersapp.stellarai.live',
            'error': {
                'type': 'SyntaxError',
                'message': 'bad syntax',
                'stack': '',
                'source': None,
                'line': None,
            }
        }
        response = client.post('/api/sentinel/log_error', json=payload)

    assert response.status_code == 200
    mock_log.assert_called_once()
    call_kwargs = mock_log.call_args
    assert call_kwargs[1]['trigger_heal'] is False


def test_sentinel_log_error_malformed_url(auth_client):
    """
    A URL where the hostname cannot be parsed should still not crash the
    endpoint; it returns 404 because process_id is never resolved.
    """
    payload = {
        'url': 'not-a-url-at-all',
        'error': {'type': 'Error', 'message': 'oops', 'stack': ''}
    }
    response = auth_client.post('/api/sentinel/log_error', json=payload)
    assert response.status_code == 404


# ===========================================================================
# sentinel_status  (GET /api/sentinel/status)
# ===========================================================================

def test_sentinel_status_no_matching_subdomain(client):
    """
    A URL whose subdomain has no matching deployment returns {healing: false}.
    """
    response = client.get('/api/sentinel/status?url=https://ghost-app.stellarai.live')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is False


def test_sentinel_status_healing_false_when_no_redis_key(auth_client):
    """
    If the Redis healing key is absent, healing is reported as False even
    for a known deployment.
    """
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO repo_history (id, user_id, project_name, process_id, status, subdomain, files_snapshot) "
            "VALUES (200, 1, 'HealProj', 'proc-200', 'running', 'healapp', '[]')"
        )
        db.commit()

    with patch('app.redis_client.get', return_value=None):
        response = auth_client.get('/api/sentinel/status?url=https://healapp.stellarai.live')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is False


def test_sentinel_status_healing_true_when_redis_key_present(auth_client):
    """
    If the Redis healing key is set, healing is reported as True.
    """
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO repo_history (id, user_id, project_name, process_id, status, subdomain, files_snapshot) "
            "VALUES (201, 1, 'HealProj2', 'proc-201', 'running', 'healapp2', '[]')"
        )
        db.commit()

    with patch('app.redis_client.get', return_value=b'1'):
        response = auth_client.get('/api/sentinel/status?url=https://healapp2.stellarai.live')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is True


def test_sentinel_status_no_url_param(client):
    """
    Calling the status endpoint without a URL parameter returns {healing: false}.
    """
    response = client.get('/api/sentinel/status')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['healing'] is False


# ===========================================================================
# sentinel_stream  (GET /api/sentinel/stream/<process_id>)
# ===========================================================================

def test_sentinel_stream_unauthenticated(client):
    """Unauthenticated request to the sentinel stream endpoint returns 401."""
    response = client.get('/api/sentinel/stream/proc-abc')
    assert response.status_code == 401


def test_sentinel_stream_process_not_found(auth_client):
    """A process_id not in repo_history returns 404."""
    response = auth_client.get('/api/sentinel/stream/proc-does-not-exist')
    assert response.status_code == 404


def test_sentinel_stream_forbidden_for_non_owner(client):
    """
    A non-admin user who does not own the process should get 403 Forbidden.
    """
    from app import get_db
    with client.application.app_context():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (60, 'attacker@evil.com', 'Attacker', 'user', 1)"
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (61, 'victim@good.com', 'Victim', 'user', 1)"
        )
        db.execute(
            "INSERT OR IGNORE INTO repo_history (id, user_id, project_name, process_id, status, subdomain, files_snapshot) "
            "VALUES (300, 61, 'VictimProj', 'proc-victim', 'running', 'victimapp', '[]')"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 60
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.get('/api/sentinel/stream/proc-victim')
    assert response.status_code == 403


def test_sentinel_stream_admin_can_access_any_process(auth_client):
    """
    An admin user can access the sentinel stream for any process, even if
    they do not own it.
    """
    from app import get_db
    with auth_client.application.app_context():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (70, 'other@example.com', 'Other', 'user', 1)"
        )
        db.execute(
            "INSERT OR IGNORE INTO repo_history (id, user_id, project_name, process_id, status, subdomain, files_snapshot) "
            "VALUES (301, 70, 'OtherProj', 'proc-other', 'running', 'otherapp', '[]')"
        )
        db.commit()

    # auth_client is user_id=1 with role='admin'
    # Mock the pubsub to avoid an infinite loop
    mock_pubsub = MagicMock()
    mock_pubsub.get_message.return_value = None
    with patch('app.redis_client.pubsub', return_value=mock_pubsub):
        with patch('app.redis_client.lrange', return_value=[]):
            response = auth_client.get('/api/sentinel/stream/proc-other')
    assert response.status_code == 200


# ===========================================================================
# orchestrator_quota_info  (GET /api/admin/orchestrator/quota-info)
# ===========================================================================

def test_orchestrator_quota_info_non_admin(client):
    """Non-admin users should receive 403 Forbidden."""
    with client.application.app_context():
        from app import get_db
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (80, 'reg@user.com', 'Regular', 'user', 1)"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 80
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.get('/api/admin/orchestrator/quota-info')
    assert response.status_code == 403


def test_orchestrator_quota_info_no_db(auth_client):
    """
    When the orchestrator DB file is absent, the endpoint returns the
    default empty quota structure with 200.
    """
    with patch('sqlite3.connect', side_effect=Exception("DB not found")):
        response = auth_client.get('/api/admin/orchestrator/quota-info')
    assert response.status_code == 200
    data = json.loads(response.data)
    # Should contain defaults
    assert 'gemini_avg_cost' in data


def test_orchestrator_quota_info_with_db(auth_client):
    """
    When the orchestrator DB exists with quota_data, the endpoint correctly
    parses and returns the quota information.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        conn = _real_sqlite3_connect(temp_db_path)
        conn.execute("CREATE TABLE orchestrator_state (key TEXT PRIMARY KEY, value TEXT)")
        quota_data = {
            'gemini': {'status': 'OK', 'weekly_percent': 30.0},
            'claude': {'status': 'OK', 'weekly_percent': 20.0},
        }
        conn.execute("INSERT INTO orchestrator_state VALUES ('quota_data', ?)", (json.dumps(quota_data),))
        conn.execute("INSERT INTO orchestrator_state VALUES ('gemini_avg_cost', '1.5')")
        conn.execute("INSERT INTO orchestrator_state VALUES ('claude_avg_cost', '3.2')")
        conn.execute("INSERT INTO orchestrator_state VALUES ('gemini_runs_count', '10')")
        conn.execute("INSERT INTO orchestrator_state VALUES ('claude_runs_count', '5')")
        conn.commit()
        conn.close()

        def mock_connect(path):
            if 'orchestrator' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('sqlite3.connect', side_effect=mock_connect):
            response = auth_client.get('/api/admin/orchestrator/quota-info')

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['gemini_avg_cost'] == 1.5
        assert data['claude_avg_cost'] == 3.2
        assert data['gemini_runs_count'] == 10
        assert data['claude_runs_count'] == 5
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


# ===========================================================================
# orchestrator_refresh_quota  (GET /api/admin/orchestrator/refresh-quota)
# ===========================================================================

def test_orchestrator_refresh_quota_non_admin(client):
    """Non-admin users should receive 403 Forbidden."""
    with client.application.app_context():
        from app import get_db
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (81, 'reg2@user.com', 'Regular2', 'user', 1)"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 81
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.get('/api/admin/orchestrator/refresh-quota')
    assert response.status_code == 403


def test_orchestrator_refresh_quota_error_path(auth_client):
    """
    If fetching quota from the container raises an exception, the endpoint
    returns 500 with an error message (not crash).
    """
    with patch('orchestrator.quota.fetch_quota_data_from_container', side_effect=Exception("container unreachable")):
        response = auth_client.get('/api/admin/orchestrator/refresh-quota')
    assert response.status_code == 500
    data = json.loads(response.data)
    assert 'error' in data


# ===========================================================================
# get_agent_dms  (GET /api/admin/agent_messages/dms)
# ===========================================================================

def test_get_agent_dms_non_admin(client):
    """Non-admin users should receive 403 Forbidden."""
    with client.application.app_context():
        from app import get_db
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (90, 'user90@test.com', 'U90', 'user', 1)"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 90
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.get('/api/admin/agent_messages/dms?agent_id=newton')
    assert response.status_code == 403


def test_get_agent_dms_missing_agent_id(auth_client):
    """Missing agent_id query parameter returns 400."""
    response = auth_client.get('/api/admin/agent_messages/dms')
    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Missing agent_id' in data['error']


def test_get_agent_dms_no_db(auth_client):
    """If the memory DB does not exist, the endpoint returns an empty list."""
    with patch('os.path.exists', return_value=False):
        response = auth_client.get('/api/admin/agent_messages/dms?agent_id=newton')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data == []


def test_get_agent_dms_with_resolved_task(auth_client):
    """
    DM messages whose thread_id corresponds to a resolved task should have
    is_resolved=True, while others remain is_resolved=False. Verifies the
    N+1 batch-fetch optimisation works correctly.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)
        conn = _real_sqlite3_connect(temp_db_path)
        now_str = datetime.datetime.now().isoformat()
        # Insert an open task (id=10) and a resolved task (id=11)
        conn.execute(
            "INSERT INTO agent_tasks (id, title, created_by, status, created_at, updated_at) VALUES (10, 'Open task', 'admin', 'open', ?, ?)",
            (now_str, now_str)
        )
        conn.execute(
            "INSERT INTO agent_tasks (id, title, created_by, status, created_at, updated_at) VALUES (11, 'Resolved task', 'admin', 'resolved', ?, ?)",
            (now_str, now_str)
        )
        # Insert DMs referencing each task
        conn.execute(
            "INSERT INTO agent_messages (channel, thread_id, sender_id, recipient_id, content, message_type, created_at) "
            "VALUES ('dm', 'resolve:task:10', 'admin', 'newton', 'Please fix test coverage', 'text', ?)",
            (now_str,)
        )
        conn.execute(
            "INSERT INTO agent_messages (channel, thread_id, sender_id, recipient_id, content, message_type, created_at) "
            "VALUES ('dm', 'resolve:task:11', 'admin', 'newton', 'Good job!', 'text', ?)",
            (now_str,)
        )
        conn.commit()
        conn.close()

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.get('/api/admin/agent_messages/dms?agent_id=newton')

        assert response.status_code == 200
        messages = json.loads(response.data)
        assert len(messages) == 2

        # Map thread_id to is_resolved
        by_thread = {m['thread_id']: m for m in messages}
        assert by_thread['resolve:task:10']['is_resolved'] is False
        assert by_thread['resolve:task:11']['is_resolved'] is True
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


# ===========================================================================
# send_agent_message  (POST /api/admin/agent_messages/send)
# ===========================================================================

def test_send_agent_message_non_admin(client):
    """Non-admin users should receive 403 Forbidden."""
    with client.application.app_context():
        from app import get_db
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (91, 'user91@test.com', 'U91', 'user', 1)"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 91
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.post('/api/admin/agent_messages/send', json={'content': 'hello', 'channel': 'group'})
    assert response.status_code == 403


def test_send_agent_message_missing_content(auth_client):
    """Request without 'content' returns 400."""
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.post('/api/admin/agent_messages/send', json={})
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)

    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Missing content' in data['error']


def test_send_agent_message_group_success(auth_client):
    """
    Sending a group message should insert a row in agent_messages and
    publish it to Redis.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                with patch('app.redis_client.publish') as mock_publish:
                    response = auth_client.post(
                        '/api/admin/agent_messages/send',
                        json={'content': 'Hello team!', 'channel': 'group'}
                    )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True

        # Verify published to Redis
        mock_publish.assert_called_once()
        channel_arg = mock_publish.call_args[0][0]
        assert channel_arg == 'agent_events'

        # Verify DB insertion
        conn = _real_sqlite3_connect(temp_db_path)
        row = conn.execute("SELECT * FROM agent_messages WHERE content = 'Hello team!'").fetchone()
        conn.close()
        assert row is not None
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


def test_send_agent_message_dm_autocreates_task(auth_client):
    """
    Sending a DM with a 'resolve:task:<id>' thread_id that has no
    corresponding task row should auto-create the task in agent_tasks.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.post(
                    '/api/admin/agent_messages/send',
                    json={
                        'content': 'Write new tests for sentinel_healer',
                        'channel': 'dm',
                        'recipient_id': 'newton',
                        'thread_id': 'resolve:task:9999',
                    }
                )

        assert response.status_code == 200
        # Verify auto-created task
        conn = _real_sqlite3_connect(temp_db_path)
        task = conn.execute("SELECT * FROM agent_tasks WHERE id = 9999").fetchone()
        conn.close()
        assert task is not None
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


def test_send_agent_message_no_db(auth_client):
    """If the memory DB does not exist, the endpoint returns 500."""
    with patch('os.path.exists', return_value=False):
        response = auth_client.post(
            '/api/admin/agent_messages/send',
            json={'content': 'test', 'channel': 'group'}
        )
    assert response.status_code == 500


# ===========================================================================
# list_agent_tasks  (GET /api/admin/agent_tasks/list)
# ===========================================================================

def test_list_agent_tasks_non_admin(client):
    """Non-admin users should receive 403 Forbidden."""
    with client.application.app_context():
        from app import get_db
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (92, 'user92@test.com', 'U92', 'user', 1)"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 92
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.get('/api/admin/agent_tasks/list')
    assert response.status_code == 403


def test_list_agent_tasks_no_db(auth_client):
    """If the orchestrator DB does not exist, the endpoint returns an empty list."""
    with patch('os.path.exists', return_value=False):
        response = auth_client.get('/api/admin/agent_tasks/list')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data == []


def test_list_agent_tasks_with_data(auth_client):
    """
    When the orchestrator memory DB exists, all tasks are returned as a JSON array,
    each containing the expected fields. list_agent_tasks reads from memory.db.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)
        conn = _real_sqlite3_connect(temp_db_path)
        now_str = datetime.datetime.now().isoformat()
        conn.execute(
            "INSERT INTO agent_tasks (id, title, description, created_by, assigned_to, status, priority, created_at, updated_at) "
            "VALUES (500, 'Test Task Alpha', 'Desc', 'admin', 'newton', 'open', 'high', ?, ?)",
            (now_str, now_str)
        )
        conn.commit()
        conn.close()

        MEM_DB_PATH = '/home/stellaradmin/my_app/orchestrator/memory.db'

        def mock_exists(path):
            if path == MEM_DB_PATH:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if path == MEM_DB_PATH:
                c = _real_sqlite3_connect(temp_db_path)
                c.row_factory = sqlite3.Row
                return c
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.get('/api/admin/agent_tasks/list')

        assert response.status_code == 200
        tasks = json.loads(response.data)
        assert len(tasks) >= 1
        task = next(t for t in tasks if t['id'] == 500)
        assert task['title'] == 'Test Task Alpha'
        assert task['assigned_to'] == 'newton'
        assert task['status'] == 'open'
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


# ===========================================================================
# create_agent_task  (POST /api/admin/agent_tasks/create)
# ===========================================================================

def test_create_agent_task_non_admin(client):
    """Non-admin users should receive 403 Forbidden."""
    with client.application.app_context():
        from app import get_db
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (93, 'user93@test.com', 'U93', 'user', 1)"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 93
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.post('/api/admin/agent_tasks/create', json={'title': 'Task'})
    assert response.status_code == 403


def test_create_agent_task_missing_title(auth_client):
    """Request without 'title' returns 400."""
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.post('/api/admin/agent_tasks/create', json={'description': 'No title here'})
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)

    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Missing title' in data['error']


def test_create_agent_task_success_with_assignee(auth_client):
    """
    Creating a task with an assignee should:
    - return success=True with a task_id
    - insert a row in agent_tasks
    - insert a DM task_ref message linking the thread
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.post('/api/admin/agent_tasks/create', json={
                    'title': 'Improve SSH test coverage',
                    'description': 'Write tests for ssh_gateway.py',
                    'assigned_to': 'newton',
                    'priority': 'high',
                })

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'task_id' in data

        # Verify task row exists
        conn = _real_sqlite3_connect(temp_db_path)
        task = conn.execute("SELECT * FROM agent_tasks WHERE title = 'Improve SSH test coverage'").fetchone()
        assert task is not None
        assert task[4] == 'newton'  # assigned_to column

        # Verify DM task_ref message was inserted
        dm = conn.execute("SELECT * FROM agent_messages WHERE message_type = 'task_ref' AND recipient_id = 'newton'").fetchone()
        assert dm is not None
        conn.close()
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


def test_create_agent_task_success_no_assignee(auth_client):
    """
    Creating a task without an assignee should succeed and NOT insert a DM message.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.post('/api/admin/agent_tasks/create', json={
                    'title': 'General improvement task',
                })

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True

        # Verify NO DM message was inserted
        conn = _real_sqlite3_connect(temp_db_path)
        dm_count = conn.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]
        conn.close()
        assert dm_count == 0
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


# ===========================================================================
# resolve_agent_task  (POST /api/admin/agent_tasks/resolve)
# ===========================================================================

def test_resolve_agent_task_non_admin(client):
    """Non-admin users should receive 403 Forbidden."""
    with client.application.app_context():
        from app import get_db
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, display_name, role, is_approved) "
            "VALUES (94, 'user94@test.com', 'U94', 'user', 1)"
        )
        db.commit()
    with client.session_transaction() as sess:
        sess['user_id'] = 94
        sess['role'] = 'user'
        sess['is_approved'] = True

    response = client.post('/api/admin/agent_tasks/resolve', json={'task_id': 1})
    assert response.status_code == 403


def test_resolve_agent_task_missing_task_id(auth_client):
    """Request without 'task_id' returns 400."""
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.post('/api/admin/agent_tasks/resolve', json={})
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)

    assert response.status_code == 400
    data = json.loads(response.data)
    assert 'Missing task_id' in data['error']


def test_resolve_agent_task_success(auth_client):
    """
    Resolving an existing task should update its status to 'resolved'
    and set resolved_by='admin'.
    """
    db_fd, temp_db_path = tempfile.mkstemp()
    try:
        _make_orchestrator_memory_db(temp_db_path)
        conn = _real_sqlite3_connect(temp_db_path)
        now_str = datetime.datetime.now().isoformat()
        conn.execute(
            "INSERT INTO agent_tasks (id, title, created_by, status, created_at, updated_at) "
            "VALUES (600, 'Task to resolve', 'admin', 'open', ?, ?)",
            (now_str, now_str)
        )
        conn.commit()
        conn.close()

        def mock_exists(path):
            if 'memory.db' in path:
                return True
            return _real_os_path_exists(path)

        def mock_connect(path):
            if 'memory.db' in path:
                return _real_sqlite3_connect(temp_db_path)
            return _real_sqlite3_connect(path)

        with patch('os.path.exists', side_effect=mock_exists):
            with patch('sqlite3.connect', side_effect=mock_connect):
                response = auth_client.post('/api/admin/agent_tasks/resolve', json={'task_id': 600})

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True

        # Verify task status updated in DB
        conn = _real_sqlite3_connect(temp_db_path)
        task = conn.execute("SELECT status, resolved_by FROM agent_tasks WHERE id = 600").fetchone()
        conn.close()
        assert task[0] == 'resolved'
        assert task[1] == 'admin'
    finally:
        os.close(db_fd)
        os.unlink(temp_db_path)


def test_resolve_agent_task_no_db(auth_client):
    """If the memory DB does not exist, the endpoint returns 500."""
    with patch('os.path.exists', return_value=False):
        response = auth_client.post('/api/admin/agent_tasks/resolve', json={'task_id': 1})
    assert response.status_code == 500
