"""
Google Chat adapter for the conversational invoicing assistant.

The invoicing counterpart to app/adapters/google_chat_tastingroom.py, and a
DELIBERATE sibling to the existing invoice-graph adapter (google_chat_adapter.py):
that one runs the deterministic, interrupt-driven invoice wizard; THIS one is the
free-form chat brain (vertex_agent.invoice_chat_agent) — "what's wholesale on the
Viognier?", "set the FOB price to $41", "invoice Oak Barrel and send it" — that
understands intent and acts through a tight, confirm-first tool set.

It runs on its own dedicated route, /webhooks/google-chat/invoice-chat, and is
config-gated on GOOGLE_CHAT_INVCHAT_* so it stays dormant until configured. It can
reuse the tasting-room/shared service-account credentials, so a single-project
setup works without provisioning a new bot.

PDFs: when staff attach an order PDF, we download it with the bot token, digest it
to text via services.pdf_service, and feed that text to the agent as input state —
the agent reads it, pulls out the customer + items, and stages an invoice.

Production hardening (lifted from the tasting-room adapter, learned the hard way):
  - ack-then-post: if the handler runs past Google Chat's ~30s timeout, ack and
    post the real result to the space when ready.
  - dedup + per-space lock: Google retries slow webhooks; drop retried MESSAGEs and
    serialize per space so concurrent retries never double-process.
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
    wrap_addon_response,
)

log = logging.getLogger(__name__)

_CHAT_APP_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
_ACK_DEADLINE = float(os.getenv("GCHAT_ACK_DEADLINE", "20"))

_locks: dict[str, "asyncio.Lock"] = {}
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
    """Decode the Chat app's service account key for posting async results.

    This adapter is the front door for the INVOICE Chat app (it serves
    /webhooks/google-chat), so it must post as that app's identity — the same
    GOOGLE_SERVICE_ACCOUNT_JSON_B64 the graph adapter uses. Order: an explicit
    invoice-chat key if set, then the invoice app SA, then the tasting-room key as
    a last resort. (Posting as the tasting-room bot fails — it isn't in this space.)
    """
    raw = (
        config.GOOGLE_CHAT_INVCHAT_SERVICE_ACCOUNT_JSON_B64
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "")
        or config.GOOGLE_CHAT_TR_SERVICE_ACCOUNT_JSON_B64
    )
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception as e:  # pragma: no cover - defensive
        log.warning("[inv:gc:auth] bad service account JSON: %s", e)
        return None


def _refresh_token() -> str | None:
    """Mint a fresh Chat-app bearer token (sync). None if no SA configured."""
    sa = _service_account_info()
    if not sa:
        return None
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as _GReq

        creds = service_account.Credentials.from_service_account_info(sa, scopes=_CHAT_APP_SCOPES)
        creds.refresh(_GReq())
        return creds.token
    except Exception as e:  # pragma: no cover - defensive
        log.warning("[inv:gc:auth] token refresh failed: %s", e)
        return None


def _post_message_sync(space_name: str, body: dict) -> bool:
    token = _refresh_token()
    if not token or not space_name or not body:
        return False
    url = f"https://chat.googleapis.com/v1/{space_name}/messages"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
        return r.status_code in (200, 201)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("[inv:gc] post failed: %s", e)
        return False


async def _post_message_to_space(space_name: str, body: dict) -> bool:
    if not space_name or not body:
        return False
    return await asyncio.to_thread(_post_message_sync, space_name, body)


# ── PDF attachment digest ────────────────────────────────────────────────────

def _download_attachment(resource_name: str) -> bytes | None:
    """Download an uploaded Chat attachment's bytes via the media endpoint."""
    token = _refresh_token()
    if not token or not resource_name:
        return None
    url = f"https://chat.googleapis.com/v1/media/{resource_name}?alt=media"
    try:
        with httpx.Client(timeout=60) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            return r.content
        log.warning("[inv:gc] attachment download %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # pragma: no cover - defensive
        log.warning("[inv:gc] attachment download error: %s", e)
    return None


def _digest_one_pdf(pdf_bytes: bytes, label: str) -> str:
    """Extract a doc's FULL text (no purpose assumption) so the agent can route it
    by intent — answer a question about it, look up/edit pricing, or build an order.
    Returns "" on failure."""
    try:
        from services.pdf_service import extract_text_from_pdf
        return f"[Attached document: {label}]\n{extract_text_from_pdf(pdf_bytes)}"
    except Exception as e:  # pragma: no cover - defensive
        log.warning("[inv:gc] PDF digest failed for %s: %s", label, e)
        return ""


def _digest_pdfs(msg: dict, text: str, user_email: str = "") -> str:
    """Digest every PDF referenced by the message into order text, else "".

    Covers all three shapes staff use:
      - an uploaded Chat file   → attachmentDataRef.resourceName (Chat media API)
      - a Google Drive file     → driveDataRef.driveFileId (Drive API, delegated)
      - a pasted Drive link     → drive.google.com/open?id=<ID> etc. in the text

    `user_email` is the sender — Drive downloads impersonate them (the file owner).
    """
    from services.drive_service import download_drive_file, extract_drive_file_ids

    chunks: list[str] = []
    seen_drive: set[str] = set()

    attachments = msg.get("attachment") or msg.get("attachments") or []
    if isinstance(attachments, list):
        for att in attachments:
            name = att.get("contentName") or att.get("name") or "attachment"
            content_type = (att.get("contentType") or att.get("content_type") or "").lower()
            is_pdf = "pdf" in content_type or str(name).lower().endswith(".pdf")
            ref = (att.get("attachmentDataRef") or {}).get("resourceName")
            drive_id = (att.get("driveDataRef") or {}).get("driveFileId")
            pdf_bytes = None
            if ref:                       # uploaded Chat file
                if not is_pdf:
                    continue
                pdf_bytes = _download_attachment(ref)
            elif drive_id:                # Drive file attached in Chat
                seen_drive.add(drive_id)
                pdf_bytes = download_drive_file(drive_id, user_email)
                if not pdf_bytes:
                    log.info("[inv:gc] Drive attachment %s (%s) could not be downloaded", name, drive_id)
            if pdf_bytes:
                got = _digest_one_pdf(pdf_bytes, name)
                if got:
                    chunks.append(got)

    # Drive links pasted into the message text.
    for fid in extract_drive_file_ids(text or ""):
        if fid in seen_drive:
            continue
        seen_drive.add(fid)
        pdf_bytes = download_drive_file(fid, user_email)
        if pdf_bytes:
            got = _digest_one_pdf(pdf_bytes, f"Drive file {fid}")
            if got:
                chunks.append(got)

    return "\n\n".join(chunks)


# ── Inbound event handling ───────────────────────────────────────────────────

def _is_authorized(email: str) -> bool:
    """Fail-closed: an empty allowlist (missing/blank/malformed env var) denies
    everyone rather than opening the invoicing assistant to the whole workspace."""
    allow = config.GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS
    if not allow:
        log.error("[invchat:gc] authorized-emails allowlist is empty — denying all "
                  "(set GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS)")
        return False
    return (email or "").strip().lower() in allow


def _text_resp(msg: str) -> dict:
    return {"text": (msg or "")[:4000], "actionResponse": {"type": "NEW_MESSAGE"}}


async def handle_invoice_chat_event(event: dict) -> dict:
    """Entry point for /webhooks/google-chat/invoice-chat. Never raises.

    Deadline race: respond synchronously when fast; if it runs long (LLM tool
    loop, PDF digest, Square calls), ack so Google Chat doesn't time out and post
    the real result to the space when it lands.
    """
    is_addon = "chat" in event
    ev = normalize_addon_event(event) if is_addon else event
    space_name = (ev.get("space") or {}).get("name") or ""
    etype = ev.get("type")
    log.info("[inv:gc] inbound type=%s space=%s user=%s",
             etype, space_name, (ev.get("user") or {}).get("email"))

    async def _run() -> dict:
        try:
            return await _route(ev)
        except Exception as e:
            log.error("[inv:gc] unhandled error: %s", e, exc_info=True)
            return _text_resp("Sorry — something went wrong handling that.")

    can_post = _service_account_info() is not None
    async_enabled = (
        (os.getenv("GCHAT_ASYNC", "on") or "on").lower() == "on"
        and bool(space_name)
        and can_post
        and etype == "MESSAGE"
    )
    if not async_enabled:
        resp = await _run()
        return wrap_addon_response(resp) if is_addon else resp

    holder: dict = {}
    finished = asyncio.Event()

    async def _compute():
        holder["resp"] = await _run()
        finished.set()

    asyncio.create_task(_compute())
    try:
        await asyncio.wait_for(asyncio.shield(finished.wait()), timeout=_ACK_DEADLINE)
        resp = holder["resp"]
        return wrap_addon_response(resp) if is_addon else resp
    except asyncio.TimeoutError:
        pass

    log.info("[inv:gc:async] slow op (>%.0fs) — acking; will post result to %s",
             _ACK_DEADLINE, space_name)

    async def _post_when_ready():
        await finished.wait()
        body = {k: v for k, v in holder.get("resp", {}).items() if k == "text" and v}
        await _post_message_to_space(space_name, body)

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
            "Winefornia Invoicing\n\n"
            "Ask me about catalog pricing, change a price or tier, or send me an "
            "order (paste it or attach a PDF) and I'll draft the invoice. "
            "Anything that changes pricing or sends an invoice, I'll confirm with you first."
        )
    if etype == "REMOVED_FROM_SPACE":
        return {"text": ""}

    if etype != "MESSAGE":
        return _text_resp("Unknown event type.")

    if not _is_authorized(email):
        log.warning("[inv:gc] unauthorized actor %r blocked", email)
        return _text_resp("You're not authorized to act on invoicing.")

    message_name = (ev.get("message", {}) or {}).get("name", "")
    async with _lock_for(space_name):
        if _already_seen(message_name):
            log.info("[inv:gc] dropping duplicate/retried MESSAGE %s", message_name)
            return {"text": ""}
        return await _handle_message(ev, decided_by)


async def _handle_message(ev: dict, decided_by: str) -> dict:
    msg = ev.get("message", {}) or {}
    text = (msg.get("argumentText") or msg.get("text") or "").strip()
    sender_email = ((ev.get("user") or {}).get("email") or "").strip()

    # Digest any attached / linked order PDF into the input state the agent reads
    # (uploaded Chat file, Drive attachment, or a pasted Drive link). Drive
    # downloads impersonate the sender (the file owner).
    pdf_text = await asyncio.to_thread(_digest_pdfs, msg, text, sender_email)
    if pdf_text:
        text = f"{text}\n\n{pdf_text}".strip() if text else pdf_text

    if not text:
        return {"text": ""}

    from vertex_agent.invoice_chat_agent import discuss
    reply = await asyncio.to_thread(discuss, text, user=decided_by)
    return _text_resp(reply or "I couldn't work that out — try rephrasing?")
