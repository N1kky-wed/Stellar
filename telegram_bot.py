import os
import requests
import logging
import time

logger = logging.getLogger(__name__)

class TelegramBot:
    def __init__(self, token=None, default_chat_id=None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.chat_id = default_chat_id or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        self.last_update_id = 0

    def get_updates(self, offset=None):
        if not self.token:
            return None
        try:
            url = f"{self.base_url}/getUpdates"
            params = {"timeout": 10}
            if offset:
                params["offset"] = offset
            t0 = time.time()
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            logger.info("Telegram updates fetched duration_sec=%.2f", time.time() - t0)
            return response.json()
        except Exception as e:
            logger.error("Failed to get Telegram updates: %s", e, exc_info=True)
            return None
            
    def get_new_messages(self):
        updates = self.get_updates(offset=self.last_update_id + 1)
        messages = []
        if updates and updates.get("ok"):
            for update in updates.get("result", []):
                self.last_update_id = update["update_id"]
                if "message" in update and "text" in update["message"]:
                    messages.append({
                        "text": update["message"]["text"],
                        "chat_id": str(update["message"]["chat"]["id"])
                    })
        return messages

    def _discover_chat_id(self):
        # First check if we already have it
        if self.chat_id:
            return self.chat_id

        updates = self.get_updates()
        if updates and updates.get("ok"):
            results = updates.get("result", [])
            if results:
                # Take the most recent chat ID from a private message
                for update in reversed(results):
                    if "message" in update and "chat" in update["message"] and update["message"]["chat"]["type"] == "private":
                         self.chat_id = str(update["message"]["chat"]["id"])
                         logger.info(f"Discovered Telegram Chat ID: {self.chat_id}")
                         return self.chat_id
        return None

    def send_message(self, text):
        if not self.token:
             logger.warning("Telegram token not set. Skipping message.")
             return

        if not self.chat_id:
            self._discover_chat_id()
        
        if not self.chat_id:
            logger.warning("Telegram Chat ID not found. Please message the bot @stellaraisystems_bot to initialize notifications.")
            return

        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text
            }
            t0 = time.time()
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram message sent to chat_id=%s duration_sec=%.2f", self.chat_id, time.time() - t0)
        except Exception as e:
            logger.error("Failed to send Telegram message: %s", e, exc_info=True)
