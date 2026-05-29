"""Telegram Bot API service.

Handles sending messages via the Telegram Bot API and parsing inbound webhook updates.
Supports text messages and document/PDF attachments.

One-time setup — register the webhook after deployment:
    GET /webhooks/telegram/set?url=https://your-domain.com
"""

import httpx
from typing import Optional

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_TASTINGROOM_BOT_TOKEN

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_FILE_BASE = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

_TR_BASE = f"https://api.telegram.org/bot{TELEGRAM_TASTINGROOM_BOT_TOKEN}" if TELEGRAM_TASTINGROOM_BOT_TOKEN else ""

# Telegram max message length is 4096 chars
_MAX_LEN = 4000


def send_message(chat_id: int | str, text: str, *, bot: str = "invoice") -> dict:
    """Send a plain-text message to a Telegram chat."""
    base = _TR_BASE if bot == "tastingroom" else _BASE
    if bot == "tastingroom" and not base:
        raise RuntimeError("TELEGRAM_TASTINGROOM_BOT_TOKEN is not set")
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN] + "…"
    resp = httpx.post(
        f"{base}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def send_inline_keyboard(
    chat_id: int | str,
    text: str,
    rows: list[list[tuple[str, str]]],
    *,
    bot: str = "invoice",
) -> dict:
    """Send a Telegram message with inline keyboard buttons."""
    base = _TR_BASE if bot == "tastingroom" else _BASE
    if bot == "tastingroom" and not base:
        raise RuntimeError("TELEGRAM_TASTINGROOM_BOT_TOKEN is not set")
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN] + "…"
    reply_markup = {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }
    resp = httpx.post(
        f"{base}/sendMessage",
        json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def set_webhook(url: str) -> dict:
    """Register this server's URL as the Telegram webhook. Call once after deploy."""
    resp = httpx.post(
        f"{_BASE}/setWebhook",
        json={"url": url, "allowed_updates": ["message", "callback_query"]},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def extract_chat_id(payload: dict) -> Optional[int]:
    """Extract chat_id from a Telegram update."""
    try:
        return payload["message"]["chat"]["id"]
    except (KeyError, TypeError):
        return None


def extract_text(payload: dict) -> Optional[str]:
    """Extract message text from a Telegram update."""
    try:
        return payload["message"].get("text")
    except (KeyError, TypeError):
        return None


def extract_document(payload: dict) -> Optional[dict]:
    """Return document metadata if the update contains a file/PDF.

    Returns {"file_id": str, "filename": str, "mime_type": str} or None.
    """
    try:
        msg = payload["message"]
        doc = msg.get("document")
        if doc:
            return {
                "file_id": doc["file_id"],
                "filename": doc.get("file_name", "attachment.pdf"),
                "mime_type": doc.get("mime_type", "application/octet-stream"),
            }
    except (KeyError, TypeError):
        return None
    return None


def download_document(file_id: str) -> bytes:
    """Download a Telegram document and return its raw bytes.

    Two-step: getFile to resolve the path, then download from the CDN.
    """
    # Step 1 — resolve file_id → file_path
    meta = httpx.get(f"{_BASE}/getFile", params={"file_id": file_id}, timeout=10)
    meta.raise_for_status()
    file_path = meta.json()["result"]["file_path"]

    # Step 2 — download the file
    file_resp = httpx.get(f"{_FILE_BASE}/{file_path}", timeout=30, follow_redirects=True)
    file_resp.raise_for_status()
    return file_resp.content
