"""Access control helpers for Telegram bot entry points."""

from __future__ import annotations

from app.config import (
    TELEGRAM_TASTINGROOM_AUTHORIZED_CHAT_IDS,
    TELEGRAM_TASTINGROOM_AUTHORIZED_USER_IDS,
)


def _string_id(value: object) -> str:
    return str(value or "").strip()


def is_authorized_tastingroom_update(update_obj: dict) -> bool:
    """Allow only configured tasting-room Telegram chats or user accounts."""
    allowed_chat_ids = set(TELEGRAM_TASTINGROOM_AUTHORIZED_CHAT_IDS)
    allowed_user_ids = set(TELEGRAM_TASTINGROOM_AUTHORIZED_USER_IDS)
    if not allowed_chat_ids and not allowed_user_ids:
        return False

    chat_id = _string_id((update_obj.get("chat") or {}).get("id"))
    user_id = _string_id((update_obj.get("from") or {}).get("id"))
    return bool((chat_id and chat_id in allowed_chat_ids) or (user_id and user_id in allowed_user_ids))
