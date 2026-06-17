"""
Google Chat adapter for the Tasting Room approval flow.

This is the Google Chat counterpart to the (now-removed) Telegram tasting-room
bot. Unlike the invoice adapter (a synchronous, interrupt-driven wizard), the
tasting-room flow is OUTBOUND-initiated: case_desk_graph creates a
ReservationActionRequest, we post an approval card to a Chat space, a human taps
a button, and the click resumes the channel-agnostic
services.tastingroom_service.process_action_decision().

It runs as a SEPARATE Google Chat app (its own GCP project → its own bot identity)
served from a dedicated route on this same server: /webhooks/google-chat/tastingroom.
Everything here is config-gated on GOOGLE_CHAT_TR_SPACE, so when it is unset no
approval card is pushed (the action is still persisted and visible via /status).

Button scheme: each button's action carries the same callback string the Telegram
path used — "tr:{action_id}:{decision}" — so _rows_for_action() is reused verbatim.

Production hardening (lifted from google_chat_adapter.py, learned the hard way):
  - ack-then-post: if the handler runs longer than Google Chat's ~30s webhook
    timeout, ack immediately and post the real result to the space when ready.
  - dedup + per-space lock: Google retries slow webhooks; we drop retried MESSAGEs
    and serialize events per space so concurrent retries never double-process.
  - retry: outbound card/result posts retry transient Chat API failures.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import time

import httpx

from app import config
from app.adapters.gchat_format import (
    normalize_addon_event,
    rewrite_card_buttons,
    wrap_addon_response,
)

log = logging.getLogger(__name__)

_CHAT_APP_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]

# How long to wait for a synchronous result before acking and finishing async.
# Must stay under Google Chat's ~30s webhook timeout.
_ACK_DEADLINE = float(os.getenv("GCHAT_ACK_DEADLINE", "20"))

# Per-space lock serializes events for one space so concurrent messages/clicks
# (including Google's webhook retries) never race.
_locks: dict[str, "asyncio.Lock"] = {}
# Bounded dedup of processed message ids — Google retries the webhook when a
# response is slow, which would otherwise double-process a command.
_seen_messages: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_SEEN_MAX = 1000


def _lock_for(key: str) -> "asyncio.Lock":
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _already_seen(message_name: str) -> bool:
    if not message_name:
        return False
    if message_name in _seen_messages:
        return True
    _seen_messages[message_name] = None
    while len(_seen_messages) > _SEEN_MAX:
        _seen_messages.popitem(last=False)
    return False


# ── Auth / posting ───────────────────────────────────────────────────────────

def _service_account_info() -> dict | None:
    """Decode the tasting-room Chat app's service account key.

    Falls back to the shared invoice service account so a single-project setup
    still works; a true separate bot supplies GOOGLE_TASTINGROOM_SA_JSON_B64.
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


def _refresh_token() -> str | None:
    """Mint a fresh Chat-app bearer token (sync — service account never expires)."""
    sa = _service_account_info()
    if not sa:
        return None
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as _GReq

    creds = service_account.Credentials.from_service_account_info(sa, scopes=_CHAT_APP_SCOPES)
    creds.refresh(_GReq())
    return creds.token


def _send_message(space_name: str, body: dict) -> tuple[bool, str]:
    """One POST attempt to a space. Returns (ok, message_name_or_detail)."""
    token = _refresh_token()
    if not token:
        return False, "no service account configured"
    url = f"https://chat.googleapis.com/v1/{space_name}/messages"
    with httpx.Client(timeout=30) as client:
        r = client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    if r.status_code in (200, 201):
        return True, (r.json() or {}).get("name", "")
    return False, f"{r.status_code}: {r.text[:200]}"


def _send_with_retry(space_name: str, body: dict, *, attempts: int = 3) -> str | None:
    """Post to a space, retrying transient failures. Returns the message name or None."""
    if not space_name or not body:
        return None
    last = None
    for i in range(1, attempts + 1):
        try:
            ok, detail = _send_message(space_name, body)
            if ok:
                if i > 1:
                    log.info("[tr:gc] post succeeded on attempt %d", i)
                return detail or ""
            last = detail
        except Exception as e:
            last = str(e)
        log.warning("[tr:gc] post attempt %d/%d failed: %s", i, attempts, last)
        if i < attempts:
            time.sleep(0.4 * i)
    log.error("[tr:gc] post gave up after %d attempts: %s", attempts, last)
    return None


async def _post_message_to_space(space_name: str, body: dict) -> bool:
    """Async wrapper around the retrying poster (for the ack-then-post path)."""
    if not space_name or not body:
        return False
    name = await asyncio.to_thread(_send_with_retry, space_name, body)
    return name is not None


def _approval_card(action_id: str, body_text: str,
                   rows: list[list[tuple[str, str]]]) -> dict:
    """Build a cardsV2 message body from the channel-agnostic button rows.

    Each row becomes a buttonList; the button's bare function is the
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
    """Post an approval card to the configured space (with retry). Returns msg name."""
    if not is_enabled():
        return None
    name = _send_with_retry(config.GOOGLE_CHAT_TR_SPACE, _approval_card(action_id, body_text, rows))
    if name is not None:
        log.info("[tr:gc] posted approval card for %s → %s", action_id, name)
    return name


def _to_message_body(resp: dict) -> dict:
    """Convert a handler response to a Chat REST Message body for async posting.

    Keeps only text/cardsV2 (drops the interaction-only actionResponse) and
    rewrites card buttons to the tasting-room callback form so they still work.
    """
    if not isinstance(resp, dict):
        return {}
    body = {k: v for k, v in resp.items() if k in ("text", "cardsV2") and v not in ("", None)}
    if body.get("cardsV2"):
        try:
            rewrite_card_buttons(body["cardsV2"], endpoint_url=config.GOOGLE_CHAT_TR_ENDPOINT_URL)
        except Exception:
            pass
    return body


# ── Inbound event handling ───────────────────────────────────────────────────

def _is_authorized_approver(email: str) -> bool:
    """Only configured approvers (Cecil/Lisa) may act on cards/commands.

    Empty allowlist = open to any authenticated space member (back-compatible).
    """
    allow = config.GOOGLE_CHAT_TR_AUTHORIZED_EMAILS
    if not allow:
        return True
    return (email or "").strip().lower() in allow


def _text_resp(msg: str, *, update: bool = False) -> dict:
    resp: dict = {"text": msg[:4000]}
    resp["actionResponse"] = {"type": "UPDATE_MESSAGE" if update else "NEW_MESSAGE"}
    return resp


async def handle_tastingroom_event(event: dict) -> dict:
    """Entry point for /webhooks/google-chat/tastingroom. Never raises.

    Deadline race: if the work finishes within _ACK_DEADLINE, respond
    synchronously (best UX). If it runs long (LLM command parsing, email send),
    ack so Google Chat doesn't time out, and post the real result to the space
    when it lands.
    """
    is_addon = "chat" in event
    ev = normalize_addon_event(event) if is_addon else event
    space_name = (ev.get("space") or {}).get("name") or ""
    etype = ev.get("type")
    # Surface the space resource name so GOOGLE_CHAT_TR_SPACE can be read straight
    # from the logs (it's what proactive approval cards post into).
    log.info("[tr:gc] inbound type=%s space=%s user=%s",
             etype, space_name, (ev.get("user") or {}).get("email"))

    async def _run() -> dict:
        try:
            return await _route(ev)
        except Exception as e:
            log.error("[tr:gc] unhandled error: %s", e, exc_info=True)
            return _text_resp("Sorry — something went wrong handling that.")

    async_enabled = (
        (os.getenv("GCHAT_ASYNC", "on") or "on").lower() == "on"
        and bool(space_name)
        and etype in ("MESSAGE", "CARD_CLICKED")
    )
    if not async_enabled:
        resp = await _run()
        return wrap_addon_response(resp) if is_addon else resp

    # Compute in the background; deliver sync if fast, else ack + post async.
    holder: dict = {}
    finished = asyncio.Event()

    async def _compute():
        holder["resp"] = await _run()
        finished.set()

    asyncio.create_task(_compute())
    try:
        await asyncio.wait_for(asyncio.shield(finished.wait()), timeout=_ACK_DEADLINE)
        resp = holder["resp"]                       # finished in time → sync result
        return wrap_addon_response(resp) if is_addon else resp
    except asyncio.TimeoutError:
        pass

    log.info("[tr:gc:async] slow op (>%.0fs) — acking; will post result to %s",
             _ACK_DEADLINE, space_name)

    async def _post_when_ready():
        await finished.wait()
        await _post_message_to_space(space_name, _to_message_body(holder.get("resp", {})))

    asyncio.create_task(_post_when_ready())
    ack = _text_resp("⏳ Working on it — I'll post the result here in a moment.")
    return wrap_addon_response(ack) if is_addon else ack


async def _route(ev: dict) -> dict:
    etype = ev.get("type", "")
    user = ev.get("user", {}) or {}
    email = (user.get("email") or "").strip().lower()
    decided_by = "gchat_" + (email or user.get("name") or "unknown")
    space_name = (ev.get("space") or {}).get("name") or ""

    if etype == "ADDED_TO_SPACE":
        return _text_resp(
            "Winefornia Tasting Room\n\n"
            "Reservation approvals will appear here as cards — tap a button to act."
        )
    if etype == "REMOVED_FROM_SPACE":
        return {"text": ""}

    # Only authorized approvers (Cecil/Lisa) may act on cards or commands.
    if etype in ("CARD_CLICKED", "MESSAGE") and not _is_authorized_approver(email):
        log.warning("[tr:gc] unauthorized actor %r blocked on %s", email, etype)
        return _text_resp("You're not authorized to act on tasting-room reservations.",
                          update=(etype == "CARD_CLICKED"))

    if etype == "CARD_CLICKED":
        async with _lock_for(space_name):
            return await _handle_click(ev, decided_by)

    if etype == "MESSAGE":
        message_name = (ev.get("message", {}) or {}).get("name", "")
        async with _lock_for(space_name):
            if _already_seen(message_name):
                log.info("[tr:gc] dropping duplicate/retried MESSAGE %s", message_name)
                return {"text": ""}
            return await _handle_message(ev, decided_by)

    return _text_resp("Unknown event type.")


def _decide_and_advance(action_id: str, decision: str, decided_by: str) -> dict:
    """Apply the card decision, then re-run the agent to propose + post the NEXT
    step (unless the case was rejected/escalated). This chains the flow forward —
    e.g. tapping internal-available advances to asking Josh — so a button tap, not
    just an inbound email, drives the case to its next step. Runs sync (off-loop)."""
    from services.tastingroom_service import process_action_decision
    result = process_action_decision(action_id, decision, decided_by)
    # Re-coordinate only after a STATUS-RESOLVING tap (yes/no/paid/…), which tells
    # us a party's answer and lets the agent propose the next step. Do NOT
    # re-coordinate after "approve" — that SENDS an outreach (e.g. the Josh email),
    # after which we WAIT for the reply (the watcher resumes on the inbound). This
    # is what prevents a re-send loop. reject/escalate are terminal.
    if (result.get("ok")
            and decision not in ("approve", "reject", "escalate")
            and result.get("status") not in ("rejected", "escalated")
            and result.get("reservation_id")):
        try:
            from vertex_agent.intake import coordinate_reservation
            nxt = coordinate_reservation(result["reservation_id"]) or {}
            result["next_action"] = (nxt.get("proposed_action") or {}).get("action")
        except Exception as e:
            log.warning("[tr:gc] re-coordinate after decision failed: %s", e)
    return result


async def _handle_click(ev: dict, decided_by: str) -> dict:
    action_name = (ev.get("action", {}) or {}).get("actionMethodName", "")
    if not action_name.startswith("tr:"):
        return _text_resp("Unrecognized action.", update=True)
    parts = action_name.split(":", 2)
    if len(parts) != 3:
        return _text_resp("Malformed action.", update=True)
    _, action_id, decision = parts

    result = await asyncio.to_thread(_decide_and_advance, action_id, decision, decided_by)

    if result.get("ok"):
        status = result.get("status", "")
        rid = result.get("reservation_id", "")
        nxt = result.get("next_action")
        if status == "rejected":
            msg = f"Got it — skipped. ({rid})"
        elif status == "escalated":
            msg = f"Marked for you to handle manually. ({rid})"
        elif status in ("sent", "completed"):
            msg = f"Done! ({rid})"
        else:
            msg = f"Updated — {status}. ({rid})"
        if nxt:
            msg += f"\n\nNext step queued: {nxt} — a card for it is on its way."
        return _text_resp(msg, update=True)

    # An already-decided action means this was a retried/duplicate click — stay
    # silent rather than posting a confusing "didn't work" message.
    error = (result.get("error") or "")
    if "already" in error.lower():
        log.info("[tr:gc] ignoring retried click on already-decided action %s", action_id)
        return {"text": ""}
    return _text_resp(f"That didn't work: {error}", update=True)


async def _handle_message(ev: dict, decided_by: str) -> dict:
    msg = ev.get("message", {}) or {}
    text = (msg.get("argumentText") or msg.get("text") or "").strip()
    if not text:
        return {"text": ""}
    from services.tastingroom_chat_service import handle_tastingroom_chat
    reply = await asyncio.to_thread(handle_tastingroom_chat, text, chat_id=decided_by)
    return _text_resp(reply or "Done.")
