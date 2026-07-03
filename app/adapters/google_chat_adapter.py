"""
Google Chat adapter for Winefornia Invoice Agent.

Mirrors bot.py (Telegram) but speaks Google Chat's event/response format.
Google Chat is the UI surface only — the invoice graph, DB, and all
business logic are identical to the Telegram path.

Event flow:
  POST /webhooks/google-chat
    → ADDED_TO_SPACE  → greeting text
    → MESSAGE         → handle_message_event()  (mirrors on_message)
    → CARD_CLICKED    → handle_card_clicked()   (mirrors on_callback)

Thread ID scheme: gc_{space_id}   (e.g. gc_AAAAbcde1fg)
"""

import asyncio
import base64
import json
import logging
import os
import httpx
from langgraph.types import Command
from agents.invoice_graph import invoice_graph, checkpointer
from services.gateway import NormalizedMessage, gateway
from services.invoice_interrupts import (
    current_invoice_interrupt as which,
    interrupt_payload,
    TEXT_INPUT_INTERRUPTS,
)

log = logging.getLogger(__name__)

# Per-space tier wizard accumulator  {space_id: {tier, schedule}}
_wizard: dict[str, dict] = {}

# ── Stability primitives ─────────────────────────────────────────────────────
# Per-thread lock serializes events for one space so concurrent messages/clicks
# never race on the shared LangGraph checkpoint.
_locks: dict[str, "asyncio.Lock"] = {}
# Bounded dedup of processed message ids. Google Chat RETRIES the webhook when a
# response is slow (invoice runs can take 30–60s), which would otherwise double-
# process and create duplicate invoices. We record a message id on first sight
# (before processing) so any retry is dropped.
import collections
_seen_messages: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_SEEN_MAX = 1000


def _lock_for(thread_id: str) -> "asyncio.Lock":
    lock = _locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[thread_id] = lock
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


# Run blocking graph/LLM/DB work off the event loop so one slow invoice never
# stalls health checks or other spaces (which would risk a mid-run restart).
async def _ainvoke(*args, **kwargs):
    return await asyncio.to_thread(invoice_graph.invoke, *args, **kwargs)


async def _aget_state(config: dict):
    return await asyncio.to_thread(invoice_graph.get_state, config)


# How long to wait for a synchronous result before acking and finishing async.
# Must stay under Google Chat's ~30s webhook timeout.
_ACK_DEADLINE = float(os.getenv("GCHAT_ACK_DEADLINE", "20"))


def _to_message_body(resp: dict) -> dict:
    """Convert a handler response to a Chat REST Message body for async posting.

    Keeps only text/cardsV2 (drops the interaction-only actionResponse) and
    rewrites card buttons to the add-on callback form so they still work.
    """
    if not isinstance(resp, dict):
        return {}
    body = {k: v for k, v in resp.items() if k in ("text", "cardsV2") and v not in ("", None)}
    if body.get("cardsV2"):
        try:
            from app.adapters.gchat_format import rewrite_card_buttons
            rewrite_card_buttons(body["cardsV2"])
        except Exception:
            pass
    return body


async def _post_message_to_space(space_name: str, body: dict) -> bool:
    """Post a message to a Chat space via the REST API (app auth, chat.bot).

    Used to deliver a result that finished AFTER the synchronous webhook ack, so
    a slow run no longer shows the operator a timeout error.
    """
    if not space_name or not body:
        return False
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as _GReq
        sa = _service_account_info()
        if not sa:
            log.error("[gc:async] no service account configured — cannot post result")
            return False
        creds = service_account.Credentials.from_service_account_info(sa, scopes=_CHAT_APP_SCOPES)
        await asyncio.to_thread(creds.refresh, _GReq())
        url = f"https://chat.googleapis.com/v1/{space_name}/messages"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                url, headers={"Authorization": f"Bearer {creds.token}"}, json=body
            )
        if r.status_code in (200, 201):
            log.info("[gc:async] posted result to %s", space_name)
            return True
        log.error("[gc:async] post failed %s: %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        log.error("[gc:async] post error: %s", e)
        return False

# Stale-click guard: maps action name → valid interrupt stages
_VALID_AT: dict[str, set[str]] = {
    "gc_confirm_yes": {"confirm_customer"},
    "gc_confirm_no":  {"confirm_customer"},
    "gc_approve":     {"approval"},
    "gc_reject":      {"approval"},
    "gc_edit":        {"approval"},
    "gc_send":        {"send"},
    "gc_draft":       {"send"},
    "gc_email_send":  {"email"},
    "gc_email_skip":  {"email"},
}

# Maps action name → resume value passed to Command(resume=...)
_RESUME: dict[str, str] = {
    "gc_confirm_yes": "yes",
    "gc_confirm_no":  "no",
    "gc_approve":     "approved",
    "gc_reject":      "rejected",
    "gc_send":        "send",
    "gc_draft":       "draft",
    "gc_email_send":  "send",
    "gc_email_skip":  "skip",
}


# ── Google Chat response builders ───────────────────────────────────────────

def _text(msg: str, *, is_card_click: bool = False) -> dict:
    """Build a text response. If responding to a card click, include actionResponse."""
    resp: dict = {"text": msg[:4096]}
    if is_card_click:
        resp["actionResponse"] = {"type": "NEW_MESSAGE"}
    return resp


def _card(card_id: str, body_text: str, buttons: list[tuple[str, str]],
          *, is_card_click: bool = False) -> dict:
    """Build a cardsV2 response. If responding to a card click, update the message."""
    resp: dict = {
        "cardsV2": [{
            "cardId": card_id,
            "card": {
                "sections": [{
                    "widgets": [
                        {"textParagraph": {"text": body_text}},
                        {"buttonList": {"buttons": [
                            {
                                "text": label,
                                "onClick": {"action": {"function": action}},
                            }
                            for label, action in buttons
                        ]}},
                    ]
                }]
            },
        }]
    }
    if is_card_click:
        resp["actionResponse"] = {"type": "UPDATE_MESSAGE"}
    return resp


def _tier_card(space_id: str, customer_name: str, known_tier: str,
               *, is_card_click: bool = False) -> dict:
    msg = f"Invoice for {customer_name}."
    if known_tier:
        msg += f"\nTier on file: {known_tier}"
    msg += "\n\nSelect pricing tier:"
    _wizard[space_id] = {}
    resp: dict = {
        "cardsV2": [{
            "cardId": "tier_card",
            "card": {
                "sections": [{
                    "widgets": [
                        {"textParagraph": {"text": msg}},
                        {"buttonList": {"buttons": [
                            {"text": "Wholesale (30%)",    "onClick": {"action": {"function": "gc_tier_Wholesale"}}},
                            {"text": "Corporate (20%)",    "onClick": {"action": {"function": "gc_tier_Corporate"}}},
                            {"text": "Club Member (15%)",  "onClick": {"action": {"function": "gc_tier_Club_Member"}}},
                            {"text": "Employee (50%)",     "onClick": {"action": {"function": "gc_tier_Employee"}}},
                            {"text": "Direct (0%)",        "onClick": {"action": {"function": "gc_tier_Direct"}}},
                            {"text": "FOB/Export (50%)",   "onClick": {"action": {"function": "gc_tier_FOB_Export"}}},
                        ]}},
                    ]
                }]
            },
        }]
    }
    if is_card_click:
        resp["actionResponse"] = {"type": "UPDATE_MESSAGE"}
    return resp


def _schedule_card(tier: str) -> dict:
    return {
        "actionResponse": {"type": "UPDATE_MESSAGE"},
        "cardsV2": [{
            "cardId": "schedule_card",
            "card": {
                "sections": [{
                    "widgets": [
                        {"textParagraph": {"text": f"Tier: {tier} ✓\n\nPayment schedule:"}},
                        {"buttonList": {"buttons": [
                            {"text": "Upon Receipt", "onClick": {"action": {"function": "gc_sched_UPON_RECEIPT"}}},
                            {"text": "NET 7",        "onClick": {"action": {"function": "gc_sched_NET_7"}}},
                            {"text": "NET 14",       "onClick": {"action": {"function": "gc_sched_NET_14"}}},
                            {"text": "NET 30",       "onClick": {"action": {"function": "gc_sched_NET_30"}}},
                        ]}},
                    ]
                }]
            },
        }]
    }


def _methods_card(sched: str) -> dict:
    label = sched.replace("_", " ")
    return {
        "actionResponse": {"type": "UPDATE_MESSAGE"},
        "cardsV2": [{
            "cardId": "methods_card",
            "card": {
                "sections": [{
                    "widgets": [
                        {"textParagraph": {"text": f"Schedule: {label} ✓\n\nPayment methods:"}},
                        {"buttonList": {"buttons": [
                            {"text": "Card + Bank ACH", "onClick": {"action": {"function": "gc_methods_CARD+BANK_ACCOUNT"}}},
                            {"text": "Card only",       "onClick": {"action": {"function": "gc_methods_CARD"}}},
                            {"text": "Bank ACH only",   "onClick": {"action": {"function": "gc_methods_BANK_ACCOUNT"}}},
                        ]}},
                    ]
                }]
            },
        }]
    }


# ── State renderer (mirrors bot.py render()) ─────────────────────────────────

def render(state: dict, space_id: str, *, is_card_click: bool = False) -> dict:
    """Build the Google Chat response JSON based on current graph interrupt."""
    ix = which(state)
    payload = interrupt_payload(state) or {}

    if ix == "missing":
        # Prefer the graph's focused LLM question; fall back to the field list.
        question = payload.get("question")
        if not question:
            fields = state.get("missing_fields", [])
            question = ("I need a bit more info. Please provide:\n• " + "\n• ".join(fields)
                        if fields else "Could you share a bit more detail on this order?")
        return _text(question, is_card_click=is_card_click)

    elif ix == "price_confirmation":
        question = payload.get("question")
        if not question:
            pending = state.get("awaiting_price") or []
            label = (pending[0].get("label") if pending else "") or "an item"
            question = (f"I don't have a price on file for “{label}”, and none was stated in the order.\n\n"
                        f"What price per bottle should I charge? Reply with the amount "
                        f"(e.g. 45 or $45.00) — I'll use it as-is.")
        return _text(question, is_card_click=is_card_click)

    elif ix == "shipping":
        question = payload.get("question") or (
            "Shipping for this Square invoice?\n\n"
            "Reply 'free' to waive it, or enter a custom amount like '$30'."
        )
        return _text(question, is_card_click=is_card_click)

    elif ix == "confirm_customer":
        c = state.get("customer", {})
        name = c.get("full_name") or c.get("company") or "Unknown"
        parts = [c.get("company"), c.get("email"), c.get("phone"), c.get("tier_name")]
        detail = "\n".join(f"  {p}" for p in parts if p)
        body = f"Found a potential match:\n\n{name}\n{detail}\n\nIs this the right customer?"
        return _card("confirm_customer_card", body, [
            ("Yes, this is them", "gc_confirm_yes"),
            ("No, create new",    "gc_confirm_no"),
        ], is_card_click=is_card_click)

    elif ix == "tier":
        c = state.get("customer", {})
        name = c.get("full_name") or c.get("company") or "customer"
        known = c.get("tier_name") or ""
        return _tier_card(space_id, name, known, is_card_click=is_card_click)

    elif ix == "approval":
        pr    = state.get("invoice_preview", {})
        c     = state.get("customer", {})
        name  = c.get("full_name") or c.get("company") or "Customer"
        tier  = state.get("tier_name") or pr.get("tier_name") or ""
        sched = (state.get("payment_schedule") or "").replace("_", " ")
        items = state.get("line_items", [])
        lines = []
        for i in items:
            vintage = i.get("vintage")
            prod = " ".join(filter(None, [i.get("product_name"), str(vintage) if vintage is not None else None]))
            qty  = i.get("quantity", 0)
            tot  = (i.get("line_total_cents") or 0) / 100
            lines.append(f"  {prod} x {qty} = ${tot:.2f}")
        disc_cents = pr.get("discount_cents") or 0
        ship_cents = pr.get("shipping_cents")
        ship_str   = ("Waived" if ship_cents == 0
                      else ("TBD" if ship_cents is None else f"${ship_cents/100:.2f}"))
        wine_total = (pr.get("wine_total_cents") or pr.get("total_before_tax_cents") or 0) / 100
        total = (pr.get("total_before_tax_cents") or 0) / 100
        body  = "\n".join(lines) or "  (no items)"
        disc_line = f"\n  Discount: -${disc_cents/100:.2f}" if disc_cents > 0 else ""
        msg = (
            f"Invoice Ready -- {name}\n"
            f"Tier: {tier}  |  Due: {sched}\n\n"
            f"{body}\n"
            f"{disc_line}"
            f"\n  Wine total: ${wine_total:.2f}"
            f"\n  Shipping: {ship_str}"
            f"\n  Total: ${total:.2f}\n\n"
            "Create this draft in Square?"
        )
        return _card("approval_card", msg, [
            ("Approve",  "gc_approve"),
            ("Edit",     "gc_edit"),
            ("Reject",   "gc_reject"),
        ], is_card_click=is_card_click)

    elif ix == "send":
        sq_id = state.get("square_invoice_id", "")
        pr    = state.get("invoice_preview", {})
        total = (pr.get("total_before_tax_cents") or 0) / 100
        c     = state.get("customer", {})
        name  = c.get("full_name") or c.get("company") or "customer"
        sq_link = f"https://squareup.com/dashboard/invoices/{sq_id}"
        msg   = f"Draft saved in Square\n{name} - ${total:.2f}\nID: {sq_id}\n{sq_link}\n\nSend to client?"
        return _card("send_card", msg, [
            ("Send to Client", "gc_send"),
            ("Keep as Draft",  "gc_draft"),
        ], is_card_click=is_card_click)

    elif ix == "email":
        c    = state.get("customer", {})
        name = c.get("full_name") or c.get("company") or "client"
        em   = c.get("email") or ""
        msg  = f"Invoice sent! Send email receipt to {name}" + (f" ({em})" if em else "") + "?"
        return _card("email_card", msg, [
            ("Send Receipt", "gc_email_send"),
            ("Skip",         "gc_email_skip"),
        ], is_card_click=is_card_click)

    else:
        final = (state or {}).get("final_response")
        if final:
            return _text(final, is_card_click=is_card_click)
        return _text("Done.", is_card_click=is_card_click)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_text(event: dict) -> str:
    """Extract clean message text, stripping @mention prefix in rooms."""
    msg = event.get("message", {})
    # argumentText has the text without the @mention (preferred in rooms)
    text = msg.get("argumentText") or msg.get("text") or ""
    return text.strip()


def _reset_thread(thread_id: str) -> None:
    """Wipe a thread's checkpoint so a new invoice starts from a clean slate.

    Google Chat uses one persistent thread per space (gc_<space_id>), so without
    this a new order/PDF would inherit the previous invoice's customer, tier,
    preview, etc. We reset whenever a message starts a NEW request rather than
    answering a pending text-input interrupt — giving every invoice a clear
    start and end within the same chat space.
    """
    try:
        checkpointer.delete_thread(thread_id)
        log.info("[gc] reset thread %s (new request — cleared prior invoice state)", thread_id)
    except Exception as e:
        log.warning("[gc] reset thread %s failed: %s", thread_id, e)


# Chat media download auth, ordered most-stable first. Each candidate is tried
# in turn until one returns a 200 (see _download_chat_media). Service-account app
# auth never needs a refresh token; user OAuth tokens silently expire (7 days for
# a testing-mode OAuth app) — that was the root cause of intermittent "invalid
# PDF" / download failures.
_CHAT_APP_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
_CHAT_USER_SCOPES = ["https://www.googleapis.com/auth/chat.messages.readonly"]


def _service_account_info() -> dict | None:
    """Decode the shared service-account JSON (same secret the Gmail path uses)."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception as e:
        log.warning("[gc:auth] bad GOOGLE_SERVICE_ACCOUNT_JSON_B64: %s", e)
        return None


def _user_token_info(sender_email: str = "") -> dict | None:
    """Per-account user OAuth token, preferring the sender, then the default."""
    def _b64_for(email: str) -> str | None:
        if not email or "@" not in email:
            return None
        safe = email.upper().replace("@", "_").replace(".", "_").replace("-", "_")
        return os.environ.get(f"GOOGLE_TOKEN_JSON_B64_{safe}")

    raw = _b64_for(sender_email) or _b64_for(os.environ.get("GOOGLE_ACCOUNT_EMAIL", ""))
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception as e:
        log.warning("[gc:auth] bad user token for %r: %s", sender_email, e)
        return None


def _chat_cred_candidates(sender_email: str = ""):
    """Yield (label, creds) pairs that may authorize a Chat media download,
    most-stable first. The caller tries each until one returns a 200."""
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials

    sa_info = _service_account_info()
    if sa_info:
        # 1. App authentication — the Chat app reads its own attachment.
        try:
            yield "sa_app", service_account.Credentials.from_service_account_info(
                sa_info, scopes=_CHAT_APP_SCOPES)
        except Exception as e:
            log.warning("[gc:auth] sa_app creds failed: %s", e)
        # 2. Domain-wide delegation impersonating a space member.
        subject = (sender_email if sender_email and "@" in sender_email
                   else os.environ.get("GOOGLE_DELEGATED_USER_EMAIL")
                   or os.environ.get("GOOGLE_ACCOUNT_EMAIL", ""))
        if subject and "@" in subject:
            try:
                yield "sa_dwd", service_account.Credentials.from_service_account_info(
                    sa_info, scopes=_CHAT_USER_SCOPES, subject=subject)
            except Exception as e:
                log.warning("[gc:auth] sa_dwd creds failed: %s", e)

    # 3. User OAuth token (legacy fallback).
    info = _user_token_info(sender_email)
    if info:
        try:
            yield "user_oauth", Credentials.from_authorized_user_info(info)
        except Exception as e:
            log.warning("[gc:auth] user_oauth creds failed: %s", e)


async def _download_chat_media(resource_name: str, sender_email: str = "") -> bytes | None:
    """Download an UPLOADED_CONTENT attachment via the Chat API media endpoint.

    Tries each auth strategy (app auth → delegation → user token) and returns the
    bytes from the first that yields a 200, so a single stale credential no longer
    breaks PDF intake.
    """
    from urllib.parse import quote
    from google.auth.transport.requests import Request as _GReq

    url = f"https://chat.googleapis.com/v1/media/{quote(resource_name, safe='')}?alt=media"
    candidates = list(_chat_cred_candidates(sender_email))
    if not candidates:
        log.error("[gc:download] no Chat credential available for sender=%r "
                  "(set GOOGLE_SERVICE_ACCOUNT_JSON_B64 or GOOGLE_TOKEN_JSON_B64_*)",
                  sender_email)
        return None

    last_status = None
    async with httpx.AsyncClient(timeout=30) as client:
        for label, creds in candidates:
            try:
                await asyncio.to_thread(creds.refresh, _GReq())
            except Exception as e:
                log.warning("[gc:download] %s token refresh failed: %s", label, e)
                continue
            r = await client.get(url, headers={"Authorization": f"Bearer {creds.token}"},
                                 follow_redirects=True)
            if r.status_code == 200:
                log.info("[gc:download] media OK via %s (%d bytes)", label, len(r.content))
                return r.content
            last_status = r.status_code
            log.warning("[gc:download] media %s via %s body=%s",
                        r.status_code, label, r.text[:300])

    log.error("[gc:download] all Chat credentials failed for sender=%r (last status=%s)",
              sender_email, last_status)
    return None


async def _download_attachment(attachment: dict, sender_email: str = "") -> bytes | None:
    """Download a Google Chat attachment.

    Uploaded files (source=UPLOADED_CONTENT) must be fetched via the Chat media
    API using attachmentDataRef.resourceName + a credential that can read the
    message. The browser-facing downloadUri (chat.google.com/api/get_attachment_url)
    needs a logged-in user session and returns an HTML page to a programmatic
    caller, which then fails downstream as an "invalid PDF" — so we never use it
    for uploaded content.
    """
    ref = attachment.get("attachmentDataRef") or {}
    resource_name = ref.get("resourceName")
    if resource_name:
        try:
            return await _download_chat_media(resource_name, sender_email)
        except Exception as e:
            log.error("[gc:download] Chat media API download failed: %s", e)
            return None

    # Google Drive file (source=DRIVE_FILE) — fetch via the Drive API using the
    # service account's domain-wide delegation (impersonating the workspace user,
    # who owns the file). The browser downloadUri below can't be used: it needs a
    # logged-in session and returns HTML to a programmatic caller.
    drive_id = (attachment.get("driveDataRef") or {}).get("driveFileId")
    if drive_id:
        try:
            from services.drive_service import download_drive_file
            # Impersonate the sender (the file owner) so we can read files they own.
            return await asyncio.to_thread(download_drive_file, drive_id, sender_email)
        except Exception as e:
            log.error("[gc:download] Drive download failed: %s", e)
            return None

    # Fallback only for link-style attachments (external URL).
    uri = attachment.get("downloadUri")
    if not uri:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(uri, follow_redirects=True)
            r.raise_for_status()
            return r.content
    except Exception as e:
        log.error("[gc:download] fallback download failed: %s", e)
        return None


# ── Main dispatcher ──────────────────────────────────────────────────────────

# ── Workspace Add-on format bridge ───────────────────────────────────────────
# The app is deployed as a Google Workspace Add-on, whose events nest under
# `chat` (messagePayload / buttonClickedPayload …) with a top-level
# commonEventObject, and require responses wrapped in hostAppDataAction.
#
# CRITICAL: in this format a card button's action.function must be the app's
# full HTTP endpoint URL — the real action name rides in action.parameters and
# returns in commonEventObject.parameters. A bare method name ("gc_approve")
# leaves Google with nowhere to deliver the click → "unable to process". The
# normalize/wrap/rewrite helpers below (in gchat_format) handle this; they are
# pure + unit-testable and are the proven implementation recovered from the
# last-known-good deployment.

from app.adapters.gchat_format import (
    normalize_addon_event as _normalize_addon_event,
    wrap_addon_response as _wrap_addon_response,
)


async def handle_google_chat_event(event: dict) -> dict:
    """Entry point. Detects event format, normalizes, dispatches, wraps response.

    Deadline race: if the work finishes within _ACK_DEADLINE, respond
    synchronously (normal, best UX). If it runs long, return a quick ack so
    Google Chat doesn't time out, and a background task posts the real result to
    the space via the Chat API when it's done. Never raises.
    """
    is_addon = "chat" in event
    ev = _normalize_addon_event(event) if is_addon else event
    if is_addon:
        log.info("[gc:addon] normalizing Workspace Add-on event")
    space_name = (ev.get("space") or {}).get("name") or ""
    etype = ev.get("type")
    async_enabled = (
        (os.getenv("GCHAT_ASYNC", "on") or "on").lower() == "on"
        and bool(space_name)
        and etype in ("MESSAGE", "CARD_CLICKED")
    )

    async def _run() -> dict:
        try:
            return await _route_event(ev)
        except Exception as e:
            log.error("[gc] unhandled error: %s", e, exc_info=True)
            return _text("Sorry — something went wrong handling that. Please try again.")

    if not async_enabled:
        resp = await _run()
        return _wrap_addon_response(resp) if is_addon else resp

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
        return _wrap_addon_response(resp) if is_addon else resp
    except asyncio.TimeoutError:
        pass

    # Slow op: ack now, and post the real result to the space when it lands.
    log.info("[gc:async] slow op (>%.0fs) — acking; will post result to %s",
             _ACK_DEADLINE, space_name)

    async def _post_when_ready():
        await finished.wait()
        await _post_message_to_space(space_name, _to_message_body(holder.get("resp", {})))

    asyncio.create_task(_post_when_ready())
    ack = _text("⏳ Working on it — I'll post the result here in a moment.")
    return _wrap_addon_response(ack) if is_addon else ack


async def _route_event(event: dict) -> dict:
    event_type = event.get("type", "")

    if event_type == "ADDED_TO_SPACE":
        return _text(
            "Winefornia Invoice Agent\n\n"
            "Send me a customer order and I'll create a Square invoice draft.\n\n"
            "Examples:\n"
            '- "John Smith, Oak Barrel, 12 Cabernet 2022, 6 Rose 2021"\n'
            "- Paste a forwarded email order\n"
            "- Send a PDF attachment\n\n"
            "I'll walk you through the rest step by step."
        )

    if event_type == "REMOVED_FROM_SPACE":
        return {"text": ""}

    space_name = event.get("space", {}).get("name", "spaces/unknown")
    space_id   = space_name.split("/")[-1]
    thread_id  = f"gc_{space_id}"
    config     = {"configurable": {"thread_id": thread_id}}

    if event_type == "MESSAGE":
        message_name = (event.get("message") or {}).get("name", "")
        async with _lock_for(thread_id):
            if _already_seen(message_name):
                log.info("[gc] dropping duplicate/retried MESSAGE %s", message_name)
                return {"text": ""}
            return await _handle_message(event, space_id, thread_id, config)

    if event_type == "CARD_CLICKED":
        async with _lock_for(thread_id):
            return await _handle_card_clicked(event, space_id, thread_id, config)

    return _text("Unknown event type.")


async def _handle_message(event: dict, space_id: str, thread_id: str, config: dict) -> dict:
    """Mirrors bot.py on_message()."""
    text = _extract_text(event)
    user = event.get("user", {})
    sender_id = user.get("email") or user.get("name") or space_id

    # ── PDF attachment handling (mirrors bot.py _run_pdf) ────────────────────
    msg_obj = event.get("message", {})
    attachments = msg_obj.get("attachment", [])
    log.info("[gc:message] attachments=%d keys=%s", len(attachments),
             list(msg_obj.keys()) if attachments else "N/A")
    if attachments:
        log.info("[gc:message] attachment payload: %s", attachments)
    is_new_pdf = False
    for att in attachments:
        content_type = att.get("contentType", "")
        name = att.get("name", "")
        if "pdf" in content_type or name.lower().endswith(".pdf"):
            log.info("[gc:message] PDF attachment detected: %s", name)
            pdf_bytes = await _download_attachment(att, sender_email=sender_id)
            if pdf_bytes:
                try:
                    from services.pdf_service import extract_invoice_fields_from_pdf
                    extracted = await asyncio.to_thread(extract_invoice_fields_from_pdf, pdf_bytes)
                    text = extracted  # use extracted text instead of message text
                    is_new_pdf = True  # a PDF always starts a new invoice
                    log.info("[gc:pdf] extracted %d chars from PDF", len(extracted))
                except Exception as e:
                    log.error("[gc:pdf] extraction error: %s", e)
                    return _text(f"Could not read that PDF: {e}")
            else:
                return _text("Could not download the PDF attachment. Try pasting the order text instead.")
            break

    # A pasted Google Drive link (no file attachment) — download + digest it the
    # same way, so "here's the order: drive.google.com/open?id=…" just works.
    if not is_new_pdf and text:
        from services.drive_service import download_drive_file, extract_drive_file_ids
        for fid in extract_drive_file_ids(text):
            pdf_bytes = await asyncio.to_thread(download_drive_file, fid, sender_id)
            if not pdf_bytes:
                continue
            try:
                from services.pdf_service import extract_invoice_fields_from_pdf
                text = await asyncio.to_thread(extract_invoice_fields_from_pdf, pdf_bytes)
                is_new_pdf = True
                log.info("[gc:pdf] digested Drive link %s (%d chars)", fid, len(text))
                break
            except Exception as e:
                log.error("[gc:pdf] Drive-link extraction error: %s", e)

    if not text:
        return {"text": ""}

    # A typed reply to a pending text-input interrupt CONTINUES the current
    # invoice. A new PDF never continues — it's always a fresh invoice.
    pending_ix = None
    if not is_new_pdf:
        try:
            snapshot = await _aget_state(config)
            pending_ix = which(snapshot)
            log.info("[gc:message] pending interrupt=%r (snapshot.next=%s)",
                     pending_ix, getattr(snapshot, "next", None))
        except Exception as e:
            log.error("[gc:message] get_state failed: %s", e)

    if pending_ix in TEXT_INPUT_INTERRUPTS:
        # We are mid-invoice waiting on this reply — resume, do NOT fall through
        # to a fresh run (a resume error must surface, not silently restart).
        log.info("[gc:message] resuming %s interrupt space=%s", pending_ix, space_id)
        try:
            result = await _ainvoke(Command(resume=text), config=config)
            return render(result, space_id)
        except Exception as e:
            log.error("[gc:message] resume failed (%s): %s", pending_ix, e, exc_info=True)
            return _text(f"Sorry — I hit an error continuing that step: {e}\n\nPlease try again.",
                         is_card_click=False)

    # New request (new PDF, new order, or a message after the prior invoice
    # ended). Wipe any leftover state so this invoice starts from a clean slate.
    await asyncio.to_thread(_reset_thread, thread_id)

    # Start fresh through the gateway so guardrails, control-layer traces, and
    # workflow records match the Telegram/API paths.
    log.info("[gc:message] new run space=%s text=%r", space_id, text[:80])
    try:
        result = await asyncio.to_thread(
            gateway.dispatch,
            NormalizedMessage(
                user_id=f"gc_{sender_id}",
                channel="google_chat",
                session_id=thread_id,
                text=text,
                raw={"space_id": space_id, "sender_id": sender_id},
                attachments=[],
                sender_id=sender_id,
            ),
        )
        ix = which(result)
        log.info("[gc:run] which=%r intent=%r customer_confirmed=%r tier=%r",
                 ix, result.get("intent"), result.get("customer_confirmed"), result.get("tier_name"))
        return render(result, space_id)
    except Exception as e:
        log.error("[gc:run] error: %s", e, exc_info=True)
        return _text(f"Something went wrong: {e}\n\nPlease try again.")


async def _handle_card_clicked(event: dict, space_id: str, thread_id: str, config: dict) -> dict:
    """Mirrors bot.py on_callback(). All responses include actionResponse for Google Chat."""
    action      = event.get("action", {})
    action_name = action.get("actionMethodName", "")
    log.info("[gc:click] action=%r space=%s", action_name, space_id)

    # ── Tier wizard: step 1 — tier selected ────────────────────────────────
    if action_name.startswith("gc_tier_"):
        tier = action_name[len("gc_tier_"):].replace("_", " ")
        _wizard.setdefault(space_id, {})["tier"] = tier
        log.info("[gc:wizard] tier=%r", tier)
        return _schedule_card(tier)

    # ── Tier wizard: step 2 — schedule selected ────────────────────────────
    if action_name.startswith("gc_sched_"):
        sched = action_name[len("gc_sched_"):]
        _wizard.setdefault(space_id, {})["schedule"] = sched
        log.info("[gc:wizard] sched=%r", sched)
        return _methods_card(sched)

    # ── Tier wizard: step 3 — methods selected → resume graph ─────────────
    if action_name.startswith("gc_methods_"):
        methods_str = action_name[len("gc_methods_"):]
        ws    = _wizard.pop(space_id, {})
        tier  = ws.get("tier", "Wholesale")
        sched = ws.get("schedule", "NET_30")
        resume_val = f"{tier}, {sched}, {methods_str}"
        log.info("[gc:wizard] resuming with: %r", resume_val)
        try:
            result = await _ainvoke(Command(resume=resume_val), config=config)
            ix = which(result)
            log.info("[gc:wizard] result: which=%r tier=%r items=%d",
                     ix, result.get("tier_name"), len(result.get("line_items", [])))
            return render(result, space_id, is_card_click=True)
        except Exception as e:
            log.error("[gc:wizard] error: %s", e, exc_info=True)
            return _text(f"Error applying tier: {e}", is_card_click=True)

    # ── Edit — resume graph into the edit-instruction checkpoint ─────────
    if action_name == "gc_edit":
        try:
            result = await _ainvoke(Command(resume="edit"), config=config)
            return render(result, space_id, is_card_click=True)
        except Exception as e:
            log.error("[gc:edit] error: %s", e, exc_info=True)
            return _text(f"Error starting edit: {e}", is_card_click=True)

    # ── All other card actions → stale-click guard then resume graph ───────
    if action_name in _VALID_AT:
        try:
            snapshot = await _aget_state(config)
            ix = which(snapshot) if snapshot else None
        except Exception:
            ix = None
        log.info("[gc:click] action=%r current_interrupt=%r valid_at=%s",
                 action_name, ix, _VALID_AT[action_name])
        if ix not in _VALID_AT[action_name]:
            log.warning("[gc:click] DROPPING stale action=%r (interrupt=%r)", action_name, ix)
            return {"actionResponse": {"type": "UPDATE_MESSAGE"},
                    "text": "This action has already been processed."}

    resume_val = _RESUME.get(action_name)
    if resume_val is None:
        log.warning("[gc:click] unknown action %r", action_name)
        return _text(f"Unknown action: {action_name}", is_card_click=True)

    try:
        log.info("[gc:click] resuming graph action=%r thread=%s", action_name, thread_id)
        result = await _ainvoke(Command(resume=resume_val), config=config)
        ix_after = which(result)
        log.info("[gc:click] after resume: which=%r", ix_after)
        return render(result, space_id, is_card_click=True)
    except Exception as e:
        log.error("[gc:click] error: %s", e, exc_info=True)
        return _text(f"Error: {e}", is_card_click=True)
