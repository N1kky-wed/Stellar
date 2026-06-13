import pytest
from unittest.mock import patch, MagicMock
from telegram_bot import TelegramBot

@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "123456789")

def test_telegram_bot_init_env(mock_env):
    bot = TelegramBot()
    assert bot.token == "fake_token"
    assert bot.chat_id == "123456789"
    assert bot.base_url == "https://api.telegram.org/botfake_token"

def test_telegram_bot_init_args():
    bot = TelegramBot(token="arg_token", default_chat_id="987654321")
    assert bot.token == "arg_token"
    assert bot.chat_id == "987654321"

@patch('telegram_bot.requests.get')
def test_get_updates_success(mock_get, mock_env):
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "result": []}
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    bot = TelegramBot()
    updates = bot.get_updates()
    
    assert updates == {"ok": True, "result": []}
    mock_get.assert_called_once_with("https://api.telegram.org/botfake_token/getUpdates", params={"timeout": 10}, timeout=15)

@patch('telegram_bot.requests.post')
def test_send_message_success(mock_post, mock_env):
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    bot = TelegramBot()
    bot.send_message("Hello World")

    mock_post.assert_called_once_with(
        "https://api.telegram.org/botfake_token/sendMessage",
        json={"chat_id": "123456789", "text": "Hello World"},
        timeout=10
    )

@patch('telegram_bot.requests.post')
def test_send_message_no_token(mock_post, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    bot = TelegramBot(token=None)
    bot.send_message("Hello")
    mock_post.assert_not_called()

@patch('telegram_bot.requests.get')
def test_discover_chat_id(mock_get, mock_env, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    # Setup mock response for getUpdates
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 111, "type": "group"}}},
            {"message": {"chat": {"id": 222, "type": "private"}}}
        ]
    }
    mock_get.return_value = mock_response

    bot = TelegramBot(default_chat_id=None) # Ensure no chat_id initially
    chat_id = bot._discover_chat_id()

    assert chat_id == "222"
    assert bot.chat_id == "222"

@patch('telegram_bot.requests.get')
def test_get_new_messages_success(mock_get, mock_env):
    """Asserts that get_new_messages parses updates correctly and updates last_update_id."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {
                "update_id": 1001,
                "message": {
                    "text": "Hello Telegram",
                    "chat": {"id": 999}
                }
            },
            {
                "update_id": 1002,
                "message": {
                    # message without text (e.g. photo/document)
                    "chat": {"id": 999}
                }
            }
        ]
    }
    mock_get.return_value = mock_response

    bot = TelegramBot()
    messages = bot.get_new_messages()
    
    assert len(messages) == 1
    assert messages[0] == {"text": "Hello Telegram", "chat_id": "999"}
    assert bot.last_update_id == 1002


@patch('telegram_bot.requests.get')
def test_get_updates_exception(mock_get, mock_env):
    """Asserts that get_updates returns None and logs/handles errors when a requests exception occurs."""
    mock_get.side_effect = Exception("Connection timed out")
    bot = TelegramBot()
    assert bot.get_updates() is None


@patch('telegram_bot.requests.post')
def test_send_message_exception(mock_post, mock_env):
    """Asserts that send_message handles exceptions gracefully without raising."""
    mock_post.side_effect = Exception("Failed connection")
    bot = TelegramBot()
    # Should not raise exception
    bot.send_message("Hello Test")


def test_get_updates_no_token():
    """Asserts that get_updates returns None early when token is not set."""
    bot = TelegramBot(token=None)
    bot.token = None # Force override
    assert bot.get_updates() is None


def test_discover_chat_id_already_set():
    """Asserts that _discover_chat_id returns early if chat_id is already set."""
    bot = TelegramBot(token="fake_token", default_chat_id="already_set")
    assert bot._discover_chat_id() == "already_set"


@patch('telegram_bot.requests.get')
def test_discover_chat_id_no_private_chats(mock_get):
    """Asserts that _discover_chat_id returns None if no private chat updates exist."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 111, "type": "group"}}}
        ]
    }
    mock_get.return_value = mock_response

    bot = TelegramBot(token="fake_token", default_chat_id=None)
    assert bot._discover_chat_id() is None


@patch('telegram_bot.requests.get')
def test_send_message_discover_fails(mock_get):
    """Asserts that send_message returns early if chat_id discovery fails."""
    # get_updates returns no updates
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "result": []}
    mock_get.return_value = mock_response

    bot = TelegramBot(token="fake_token", default_chat_id=None)
    # This should call _discover_chat_id which returns None, then return without sending
    bot.send_message("Hello Test")


