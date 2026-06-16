"""
Google Chat adapter for the Tasting Room approval flow.

This is the Google Chat counterpart to tastingroom_bot.py (Telegram). Unlike the
invoice adapter (a synchronous, interrupt-driven wizard), the tasting-room flow is
OUTBOUND-initiated: case_desk_graph creates a ReservationActionRequest, we post an
approval card to a Chat space, a human taps a button, and the click resumes the
exact same channel-agnostic services.tastingroom_service.process_action_decision().

It runs as a SEPARATE Google Chat app (its own GCP project → its own bot identity)
served from a dedicated route on this same server: /webhooks/google-chat/tastingroom.
Everything here is config-gated on GOOGLE_CHAT_TR_SPACE, so when it is unset the
Telegram path is the only approval channel and nothing in this module fires.

Button scheme: each button's action carries the same callback string the Telegram
path uses — "tr:{action_id}:{decision}" — so _rows_for_action() is reused verbatim.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import httpx

from app import config
from app.adapters.gchat_format import (
    normalize_addon_event,
    rewrite_card_buttons,
    wrap_addon_response,
)

log = logging.getLogger(__name__)

_CHAT_APP_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]


# ── Auth / posting ───────────────────────────────────────────────────────────

def _service_account_info() -> dict | None:
    """Decode the tasting-room Chat app's service account key.

    Falls back to the shared invoice service account so a single-project setup
    still works; a true separate bot supplies GOOGLE_CHAT_TR_SERVICE_ACCOUNT_JSON_B64.
    """
    raw = (
        config.GOOGLE_CHAT_TR_SERVICE_ACCOUNT_JSON_B64
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "")
    )
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception as e:  # pragma: no cover - defensive
        log.warning("[tr:gc:auth] bad service account JSON: %s", e)
        return None


def is_enabled() -> bool:
    """True when the Google Chat approval channel is configured."""
    return bool(config.GOOGLE_CHAT_TR_SPACE) and _service_account_info() is not None


def _approval_card(action_id: str, body_text: str,
                   rows: list[list[tuple[str, str]]]) -> dict:
    """Build a cardsV2 message body from the channel-agnostic button rows.

    Each row becomes a buttonList; the button's bare function is the Telegram-style
    "tr:{action_id}:{decision}" callback string, which rewrite_card_buttons() turns
    into the add-on parameter form pointing back at the tasting-room endpoint.
    """
    sections = [{"widgets": [{"textParagraph": {"text": body_text[:3500]}}]}]
    for row in rows:
        sections[0]["widgets"].append({
            "buttonList": {"buttons": [
                {"text": label, "onClick": {"action": {"function": callback}}}
                for label, callback in row
            ]}
        })
    cards = [{"cardId": f"tr_{action_id}", "card": {"sections": sections}}]
    rewrite_card_buttons(cards, endpoint_url=config.GOOGLE_CHAT_TR_ENDPOINT_URL)
    return {"cardsV2": cards}


def post_action_card(action_id: str, body_text: str,
                     rows: list[list[tuple[str, str]]]) -> str | None:
    """Post an approval card to the configured space. Returns the message name."""
    if not is_enabled():
        return None
    sa = _service_account_info()
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as _GReq

        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=_CHAT_APP_SCOPES
        )
        creds.refresh(_GReq())
        space = config.GOOGLE_CHAT_TR_SPACE
        url = f"https://chat.googleapis.com/v1/{space}/messages"
        body = _approval_card(action_id, body_text, rows)
        with httpx.Client(timeout=30) as client:
            r = client.post(
                url, headers={"Authorization": f"Bearer {creds.token}"}, json=body
            )
        if r.status_code in (200, 201):
            name = (r.json() or {}).get("name", "")
            log.info("[tr:gc] posted approval card for %s → %s", action_id, name)
            return name
        log.error("[tr:gc] post failed %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("[tr:gc] post error: %s", e, exc_info=True)
    return None


# ── Inbound event handling ───────────────────────────────────────────────────

def _text_resp(msg: str, *, update: bool = False) -> dict:
    resp: dict = {"text": msg[:4000]}
    resp["actionResponse"] = {"type": "UPDATE_MESSAGE" if update else "NEW_MESSAGE"}
    return resp


async def handle_tastingroom_event(event: dict) -> dict:
    """Entry point for /webhooks/google-chat/tastingroom. Never raises."""
    is_addon = "chat" in event
    ev = normalize_addon_event(event) if is_addon else event
    try:
        resp = await _route(ev)
    except Exception as e:
        log.error("[tr:gc] unhandled error: %s", e, exc_info=True)
        resp = _text_resp("Sorry — something went wrong handling that.")
    return wrap_addon_response(resp) if is_addon else resp


async def _route(ev: dict) -> dict:
    etype = ev.get("type", "")
    user = ev.get("user", {}) or {}
    decided_by = "gchat_" + (user.get("email") or user.get("name") or "unknown")

    if etype == "ADDED_TO_SPACE":
        return _text_resp(
            "Winefornia Tasting Room\n\n"
            "Reservation approvals will appear here as cards — tap a button to act."
        )
    if etype == "REMOVED_FROM_SPACE":
        return {"text": ""}

    if etype == "CARD_CLICKED":
        action_name = (ev.get("action", {}) or {}).get("actionMethodName", "")
        if not action_name.startswith("tr:"):
            return _text_resp("Unrecognized action.", update=True)
        parts = action_name.split(":", 2)
        if len(parts) != 3:
            return _text_resp("Malformed action.", update=True)
        _, action_id, decision = parts
        import asyncio
        from services.tastingroom_service import process_action_decision
        result = await asyncio.to_thread(
            process_action_decision, action_id, decision, decided_by
        )
        if result.get("ok"):
            status = result.get("status", "")
            rid = result.get("reservation_id", "")
            if status == "rejected":
                msg = f"Got it — skipped. ({rid})"
            elif status == "escalated":
                msg = f"Marked for you to handle manually. ({rid})"
            elif status in ("sent", "completed"):
                msg = f"Done! ({rid})"
            else:
                msg = f"Updated — {status}. ({rid})"
            return _text_resp(msg, update=True)
        return _text_resp(f"That didn't work: {result.get('error')}", update=True)

    if etype == "MESSAGE":
        text = (ev.get("message", {}) or {}).get("argumentText") \
            or (ev.get("message", {}) or {}).get("text") or ""
        text = text.strip()
        if not text:
            return {"text": ""}
        import asyncio
        from services.tastingroom_chat_service import handle_tastingroom_chat
        reply = await asyncio.to_thread(
            handle_tastingroom_chat, text, chat_id=decided_by
        )
        return _text_resp(reply or "Done.")

    return _text_resp("Unknown event type.")
