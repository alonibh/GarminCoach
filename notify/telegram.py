from __future__ import annotations

import logging
import urllib.request
import urllib.error
import json
import config
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

logger = logging.getLogger(__name__)


def _tenant_chat_id(requested: str | None = None) -> str | None:
    if not config.MULTI_USER_ENABLED:
        return requested or config.TELEGRAM_CHAT_ID
    try:
        from telegram_link import chat_id_for_current_tenant
        expected = chat_id_for_current_tenant()
    except RuntimeError:
        return None
    if requested is not None and str(requested) != expected:
        return None
    return expected

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(urllib.error.URLError),
    reraise=True
)
def _make_request(url: str, payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status == 200

def send_message(
    text: str,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
    *,
    parse_mode: str | None = "Markdown",
) -> bool:
    """Send a message to the tenant's Telegram chat."""
    bot_token = config.TELEGRAM_BOT_TOKEN
    target_chat_id = _tenant_chat_id(chat_id)
    if not bot_token or not target_chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    try:
        return _make_request(url, payload)
    except urllib.error.URLError as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


def send_link_message(text: str, chat_id: str) -> bool:
    """Send only a non-sensitive linking result before tenant context exists."""
    if not config.MULTI_USER_ENABLED or not config.TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        return _make_request(url, {"chat_id": str(chat_id), "text": text})
    except urllib.error.URLError:
        return False

def answer_callback_query(callback_query_id: str, text: str | None = None) -> bool:
    """Acknowledge a callback query to stop the loading spinner."""
    bot_token = config.TELEGRAM_BOT_TOKEN
    if not bot_token: return False
    
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text: payload["text"] = text
        
    try:
        return _make_request(url, payload)
    except urllib.error.URLError:
        return False

def edit_message_text(
    text: str,
    chat_id: str,
    message_id: int,
    reply_markup: dict | None = None,
    *,
    parse_mode: str | None = "Markdown",
) -> bool:
    """Edit an existing message, e.g. to remove inline buttons after an action."""
    bot_token = config.TELEGRAM_BOT_TOKEN
    target_chat_id = _tenant_chat_id(chat_id)
    if not bot_token or not target_chat_id: return False
    
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {"chat_id": target_chat_id, "message_id": message_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    try:
        return _make_request(url, payload)
    except urllib.error.URLError as e:
        logger.error(f"Failed to edit Telegram message: {e}")
        return False

def send_chat_action(chat_id: str, action: str = "typing") -> bool:
    """Send a chat action (like 'typing') to indicate activity."""
    bot_token = config.TELEGRAM_BOT_TOKEN
    target_chat_id = _tenant_chat_id(chat_id)
    if not bot_token or not target_chat_id: return False
    
    url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
    payload = {"chat_id": target_chat_id, "action": action}
    
    try:
        return _make_request(url, payload)
    except urllib.error.URLError as e:
        logger.error(f"Failed to send chat action: {e}")
        return False

def set_webhook(domain: str) -> bool:
    """Set the webhook for the bot to receive incoming messages."""
    bot_token = config.TELEGRAM_BOT_TOKEN
    secret = config.TELEGRAM_WEBHOOK_SECRET
    if not bot_token:
        return False
        
    url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    webhook_url = f"https://{domain}/telegram/webhook"
    payload = {"url": webhook_url, "secret_token": secret}
    
    try:
        return _make_request(url, payload)
    except urllib.error.URLError as e:
        logger.error(f"Failed to set Telegram webhook: {e}")
        return False
