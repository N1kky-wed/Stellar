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
