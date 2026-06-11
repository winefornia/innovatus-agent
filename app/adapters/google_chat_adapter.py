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
from agents.invoice_graph import invoice_graph
from db.retry import invoke_with_retry
from services.gateway import NormalizedMessage, gateway
from services.invoice_interrupts import current_invoice_interrupt as which
from services.invoice_interrupts import clarifying_question
from app.adapters.gchat_format import (
    normalize_addon_event as _normalize_addon_event,
    wrap_addon_response as _wrap_addon_response,
)

log = logging.getLogger(__name__)

# Per-space tier wizard accumulator  {space_id: {tier, schedule}}
_wizard: dict[str, dict] = {}

# Serialize events per conversation. Google Chat retries a webhook if it doesn't
# get a 200 within ~30s, and a user can double-click a card — either can fire two
# events for the same space concurrently. Without this, two graph.invoke calls
# race on the same checkpoint (gc_<space_id>) and corrupt it.
_locks: dict[str, asyncio.Lock] = {}

# Dedup retried MESSAGE events by resource name. A fresh dispatch opens a new case
# with a new Square idempotency key, so re-running a retried message would create a
# SECOND draft rather than being deduped — drop the retry instead.
_seen_messages: set[str] = set()
_SEEN_MAX = 500


def _lock_for(thread_id: str) -> asyncio.Lock:
    lock = _locks.get(thread_id)
    if lock is None:
        lock = _locks[thread_id] = asyncio.Lock()
    return lock


def _already_seen(message_name: str) -> bool:
    """True if this Google Chat message was already handled (i.e. a webhook retry)."""
    if message_name in _seen_messages:
        return True
    _seen_messages.add(message_name)
    if len(_seen_messages) > _SEEN_MAX:
        _seen_messages.clear()   # cheap bound; the per-space lock covers the gap
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

    if ix == "missing":
        q = clarifying_question(state)
        if q:
            return _text(q, is_card_click=is_card_click)
        fields = state.get("missing_fields", [])
        return _text("I need a bit more info. Please provide:\n• " + "\n• ".join(fields),
                      is_card_click=is_card_click)

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
        total = (pr.get("total_before_tax_cents") or 0) / 100
        body  = "\n".join(lines) or "  (no items)"
        disc_line = f"\n  Discount: -${disc_cents/100:.2f}" if disc_cents > 0 else ""
        msg = (
            f"Invoice Ready -- {name}\n"
            f"Tier: {tier}  |  Due: {sched}\n\n"
            f"{body}\n"
            f"{disc_line}"
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


# Chat media download auth, ordered most-stable first. Each candidate is tried
# in turn until one returns a 200 (see _download_chat_media). The point of the
# ordering is to stop depending on hand-minted user OAuth tokens, whose refresh
# tokens silently expire (7 days for a testing-mode OAuth app) or get revoked —
# that was the root cause of the intermittent "invalid PDF" / download failures.
#
#   1. Service-account APP auth (the Chat app reading its OWN message's
#      attachment). Scope chat.bot, no delegation/subject. Never needs a refresh
#      token, no per-user setup — as stable as the SA key itself.
#   2. Service-account domain-wide delegation, impersonating a space member with
#      chat.messages.readonly. Needs the scope authorized for the SA client ID in
#      the Workspace Admin console (same SA the Gmail path already delegates).
#   3. User OAuth token (GOOGLE_TOKEN_JSON_B64_<ACCOUNT>) — legacy fallback.
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

    # Fallback only for link-style attachments (source=DRIVE_FILE / external URL).
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

# ── New "Workspace add-on" event format adapter ──────────────────────────────
# Google's updated Chat app event format nests everything under `chat` and wraps
# the synchronous response in hostAppDataAction. We normalize inbound events into
# the classic shape the dispatcher already understands, then wrap the response.
# Docs: https://developers.google.com/workspace/add-ons/chat/quickstart-http

async def handle_google_chat_event(event: dict) -> dict:
    """Entry point. Detects event format, normalizes, dispatches, wraps response."""
    is_addon = "chat" in event
    if is_addon:
        event = _normalize_addon_event(event)
    resp = await _dispatch_classic_event(event)
    if is_addon:
        resp = _wrap_addon_response(resp)
    return resp


async def _dispatch_classic_event(event: dict) -> dict:
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
        message_name = event.get("message", {}).get("name", "")
        async with _lock_for(thread_id):
            if message_name and _already_seen(message_name):
                log.info("[gc] dropping duplicate MESSAGE %s", message_name)
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
                    log.info("[gc:pdf] extracted %d chars from PDF", len(extracted))
                except Exception as e:
                    log.error("[gc:pdf] extraction error: %s", e)
                    return _text(f"Could not read that PDF: {e}")
            else:
                return _text("Could not download the PDF attachment. Try pasting the order text instead.")
            break

    if not text:
        return {"text": ""}

    # If there's a pending text-input interrupt, resume it
    try:
        snapshot = await asyncio.to_thread(invoice_graph.get_state, config)
        ix = which(snapshot.values) if snapshot and snapshot.next else None
        if ix in ("missing", "edit_instruction", "edit_clarification"):
            log.info("[gc:message] resuming %s interrupt space=%s", ix, space_id)
            result = await asyncio.to_thread(invoke_with_retry, invoice_graph, Command(resume=text), config=config)
            return render(result, space_id)
    except Exception:
        pass

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
            result = await asyncio.to_thread(invoke_with_retry, invoice_graph, Command(resume=resume_val), config=config)
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
            result = await asyncio.to_thread(invoke_with_retry, invoice_graph, Command(resume="edit"), config=config)
            return render(result, space_id, is_card_click=True)
        except Exception as e:
            log.error("[gc:edit] error: %s", e, exc_info=True)
            return _text(f"Error starting edit: {e}", is_card_click=True)

    # ── All other card actions → stale-click guard then resume graph ───────
    if action_name in _VALID_AT:
        try:
            snapshot = await asyncio.to_thread(invoice_graph.get_state, config)
            ix = which(snapshot.values) if snapshot else None
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
        result = await asyncio.to_thread(invoke_with_retry, invoice_graph, Command(resume=resume_val), config=config)
        ix_after = which(result)
        log.info("[gc:click] after resume: which=%r", ix_after)
        return render(result, space_id, is_card_click=True)
    except Exception as e:
        log.error("[gc:click] error: %s", e, exc_info=True)
        return _text(f"Error: {e}", is_card_click=True)
