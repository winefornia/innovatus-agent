"""
FastAPI server — background services and webhook endpoints.

Primary interface: Telegram bot via `python bot.py` (long polling, 24/7).

Endpoints:
  POST /intake                  — generic text intake (email forward, Zapier, etc.)
  POST /intake/pdf              — direct PDF upload
  POST /agents/invoice/run      — dashboard chat turn (same path as /intake)
  POST /agents/invoice/resume   — answer a pending interrupt (approval, shipping, ...)
  GET  /                        — operator dashboard (app/static/index.html)
  POST /webhooks/email          — Mailgun / SendGrid inbound parse webhook
  POST /webhooks/gmail/poll     — poll Gmail "To Invoice" label
  GET  /invoices/recent         — recent invoice log
  GET  /health                  — health check
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

# The web process runs under uvicorn, which only configures its own loggers.
# Without this, the root logger defaults to WARNING and every app-level INFO
# log (Google Chat adapter, gateway, control layer) is silently dropped — the
# invoice bot (bot.py) sets this up itself; the web process didn't.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from services.gateway import NormalizedMessage, gateway, from_api, from_pdf
from services.pdf_service import extract_invoice_fields_from_pdf
from app.adapters.google_chat_adapter import handle_google_chat_event

app = FastAPI(title="Winefornia Invoice Agent", version="0.3.0")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class IntakeRequest(BaseModel):
    message: str                      # raw text from forwarded email, Zapier, etc.
    sender_id: str = "email"          # identifies the source channel
    thread_id: Optional[str] = None   # auto-generated if omitted


# ---------------------------------------------------------------------------
# Generic intake — email forwards, Zapier, Make, n8n, manual paste
# ---------------------------------------------------------------------------

def _jsonable_graph_result(result: dict) -> dict:
    """Make a graph invoke() result JSON-safe for API/dashboard consumers.

    A pending interrupt arrives under "__interrupt__" as langgraph Interrupt
    objects; clients (app/static/index.html) read the .value payload — whose
    "type" field decides which step to render (shipping question, approval
    card, ...). Normalize to plain [{"value": {...}}] regardless of
    langgraph/checkpointer version.
    """
    out = dict(result or {})
    raw = out.pop("__interrupt__", None)
    if raw:
        vals = []
        for i in raw:
            v = getattr(i, "value", i)
            vals.append({"value": v if isinstance(v, dict) else {}})
        out["__interrupt__"] = vals
    return out


@app.post("/intake")
def intake(req: IntakeRequest):
    """Feed any text (forwarded email, Zapier trigger, etc.) into the agent.

    Returns the agent's first response and a thread_id for follow-up resumes.
    """
    msg    = from_api(req.message, sender_id=req.sender_id, thread_id=req.thread_id)
    result = gateway.dispatch(msg)
    if result.get("error") and not result.get("final_response"):
        return JSONResponse(status_code=500, content=_jsonable_graph_result(result))
    return _jsonable_graph_result(result)


# ---------------------------------------------------------------------------
# PDF upload endpoint — direct HTTP upload
# ---------------------------------------------------------------------------

@app.post("/intake/pdf")
async def intake_pdf(
    file: UploadFile = File(...),
    sender_id: str = Form("pdf_upload"),
    thread_id: Optional[str] = Form(None),
):
    """Upload a PDF directly. Extracts invoice fields and starts the agent."""
    pdf_bytes = await file.read()
    message   = extract_invoice_fields_from_pdf(pdf_bytes)
    msg       = from_pdf(message, sender_id=sender_id, thread_id=thread_id)
    result    = gateway.dispatch(msg)
    if result.get("error") and not result.get("final_response"):
        return JSONResponse(status_code=500, content=_jsonable_graph_result(result))
    return _jsonable_graph_result({"extracted_message": message, **result})


# ---------------------------------------------------------------------------
# Web dashboard (app/static/index.html) — chat turns + interrupt resumes
# ---------------------------------------------------------------------------

class InvoiceRunRequest(BaseModel):
    message: str
    sender_id: str = "web_ui"
    thread_id: Optional[str] = None   # auto-generated if omitted


class InvoiceResumeRequest(BaseModel):
    thread_id: str
    decision: Any   # typed text for text interrupts; token/JSON for button clicks


@app.post("/agents/invoice/run")
def invoice_run(req: InvoiceRunRequest):
    """A dashboard chat turn. Same normalized path as /intake (guardrails,
    control-layer case records), returned with the pending interrupt payload so
    the UI can render the right step (shipping question, approval card, ...)."""
    msg    = from_api(req.message, sender_id=req.sender_id, thread_id=req.thread_id)
    result = gateway.dispatch(msg)
    if result.get("error") and not result.get("final_response"):
        return JSONResponse(status_code=500, content=_jsonable_graph_result(result))
    return _jsonable_graph_result(result)


@app.post("/agents/invoice/resume")
def invoice_resume(req: InvoiceResumeRequest):
    """Answer the invoice graph's pending human-input checkpoint — an approval
    click, a shipping reply ('free' / '$30'), a missing-info reply — and return
    the resulting state. 409 when nothing is waiting (e.g. stale browser tab)."""
    from langgraph.types import Command
    from agents.invoice_graph import invoice_graph

    config = {"configurable": {"thread_id": req.thread_id}}
    try:
        snapshot = invoice_graph.get_state(config)
        if not (snapshot and snapshot.next):
            return JSONResponse(status_code=409, content={
                "thread_id": req.thread_id,
                "final_response": ("Nothing is waiting on a reply for this conversation — "
                                   "start a new request."),
            })
        result = invoice_graph.invoke(Command(resume=req.decision), config=config)
    except Exception as e:
        logging.error("[api:resume] resume failed thread=%s: %s", req.thread_id, e, exc_info=True)
        return JSONResponse(status_code=500, content={
            "thread_id": req.thread_id,
            "final_response": f"Something went wrong resuming: {e}",
            "error": str(e),
        })
    return _jsonable_graph_result({"thread_id": req.thread_id, **result})


@app.get("/", include_in_schema=False)
def dashboard():
    """Serve the operator dashboard (chat UI over /agents/invoice/*)."""
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")


# ---------------------------------------------------------------------------
# Email inbound webhook — Mailgun or SendGrid inbound parse
# ---------------------------------------------------------------------------

@app.post("/webhooks/email")
async def email_webhook(request: Request):
    """Receive inbound emails from Mailgun or SendGrid inbound parse.

    Mailgun sends multipart/form-data with fields: sender, subject, body-plain,
    attachment-1 ... attachment-N.

    SendGrid sends multipart/form-data with fields: from, subject, text,
    attachment1 ... attachmentN.

    Both paths are handled here.
    """
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()

        # Normalise across Mailgun / SendGrid field names
        sender = form.get("sender") or form.get("from") or "unknown@email"
        subject = form.get("subject") or ""
        body = form.get("body-plain") or form.get("text") or form.get("body-html") or ""

        # Collect any PDF attachments
        pdf_texts = []
        for key in form:
            field = form[key]
            if hasattr(field, "filename") and field.filename:
                mt = getattr(field, "content_type", "") or ""
                if "pdf" in mt or (field.filename or "").lower().endswith(".pdf"):
                    pdf_bytes = await field.read()
                    extracted = extract_invoice_fields_from_pdf(pdf_bytes)
                    pdf_texts.append(f"[Attachment: {field.filename}]\n{extracted}")

        # Build message
        parts = []
        if subject:
            parts.append(f"Subject: {subject}")
        if body:
            parts.append(body)
        parts.extend(pdf_texts)
        message = "\n\n".join(parts).strip()

    else:
        # JSON body fallback
        data = await request.json()
        sender = data.get("sender") or data.get("from") or "unknown@email"
        subject = data.get("subject") or ""
        body = data.get("text") or data.get("body") or ""
        message = f"Subject: {subject}\n\n{body}".strip() if subject else body

    if not message:
        return {"ok": True, "skipped": "empty message"}

    thread_id = f"email_{uuid.uuid4().hex[:8]}"
    msg = NormalizedMessage(
        user_id=f"email_{sender}",
        channel="email",
        session_id=thread_id,
        text=message,
        raw={"sender": sender, "subject": subject},
        attachments=[],
        sender_id=sender,
    )
    result = gateway.dispatch(msg)
    if result.get("error") and not result.get("final_response"):
        return JSONResponse(status_code=500, content={"ok": False, **result})
    return {"ok": True, "thread_id": thread_id, "response": result.get("final_response"), **result}


# ---------------------------------------------------------------------------
# Gmail intake — poll for emails labeled "To Invoice" and feed into agent
# ---------------------------------------------------------------------------

@app.post("/webhooks/gmail/poll")
def gmail_poll():
    """Poll Gmail for emails labeled 'To Invoice' and ingest each into the agent."""
    try:
        from services.gmail_service import list_intake_emails, read_email, mark_processed
    except Exception as e:
        return {"error": f"Gmail not configured: {e}"}

    intake = list_intake_emails(max_results=5)
    processed = []

    for msg_meta in intake.get("messages", []):
        mid = msg_meta["message_id"]
        try:
            msg = read_email(mid)
            body = msg.get("body", "")
            subject = msg.get("subject", "")
            sender = msg.get("from", "email")

            full_text = f"Subject: {subject}\nFrom: {sender}\n\n{body}".strip()
            if not full_text:
                continue

            thread_id = f"gmail_{mid[:12]}"
            result = gateway.dispatch(
                NormalizedMessage(
                    user_id=f"gmail_{mid}",
                    channel="gmail",
                    session_id=thread_id,
                    text=full_text,
                    raw={"message_id": mid, "subject": subject, "from": sender},
                    attachments=[],
                    sender_id=sender,
                )
            )
            mark_processed(mid)
            processed.append({
                "message_id": mid,
                "subject": subject,
                "thread_id": thread_id,
                "response": result.get("final_response"),
            })
        except Exception as e:
            processed.append({"message_id": mid, "error": str(e)})

    return {"processed": processed, "count": len(processed)}


@app.post("/webhooks/gmail/tastingroom/poll")
def gmail_tastingroom_poll():
    """Poll Gmail for tasting room reservation emails and feed the tastingroom agent."""
    try:
        from services.tastingroom_mailbox import poll_once
    except Exception as e:
        return {"error": f"Gmail not configured: {e}"}
    return poll_once(max_results=10)

# ---------------------------------------------------------------------------
# Recent invoices / Health
# ---------------------------------------------------------------------------

@app.get("/invoices/recent")
def recent_invoices(limit: int = 20):
    """List recent invoice logs from Supabase."""
    try:
        from db.repository import list_recent_invoices
        return {"invoices": list_recent_invoices(limit=limit)}
    except Exception as e:
        return {"invoices": [], "error": str(e)}


@app.get("/reservations/recent")
def recent_reservations(limit: int = 20):
    """List recent tasting room reservation cases from Supabase."""
    try:
        from db.repository import list_recent_reservations
        return {"reservations": list_recent_reservations(limit=limit)}
    except Exception as e:
        return {"reservations": [], "error": str(e)}


# ── Google Chat webhook authentication ───────────────────────────────────────
# Google signs every webhook with a JWT (Authorization: Bearer …) issued by
# chat@system.gserviceaccount.com, with audience = your GCP project number.
# We verify signature + issuer (+ audience when configured) so forged POSTs are
# rejected.
#
# Rollout via GCHAT_VERIFY:
#   "observe" (default) — verify + log pass/fail, but STILL process (safe to
#                         deploy live; can't lock out real traffic).
#   "enforce"           — reject unverified requests with 401.
#   "off"               — skip entirely.
# Audience check is applied only when GOOGLE_CHAT_PROJECT_NUMBER is set.
# Workspace Add-on webhooks carry a standard Google ID token:
#   iss   = https://accounts.google.com
#   aud   = the webhook endpoint URL
#   email = the add-on's gcp-sa-gsuiteaddons service agent (binds it to our project)
_CHAT_AUDIENCE = os.getenv(
    "GOOGLE_CHAT_AUDIENCE", "https://winefornia-agent.fly.dev/webhooks/google-chat"
)
_CHAT_SIGNER_EMAIL = os.getenv(
    "GOOGLE_CHAT_SIGNER_EMAIL",
    "service-338702309220@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
)


def _peek_jwt_claims(token: str) -> dict:
    """Decode a JWT's claims WITHOUT verifying (diagnostic only)."""
    import base64
    import json as _json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        return _json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _verify_google_chat_token(
    auth_header: str,
    audience: str | None = None,
    signer_email: str | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). Verifies the Google Chat Bearer JWT (sync — call via
    a thread).

    audience/signer_email default to the invoice app's values; the tasting-room
    Chat app (a separate GCP project, so a different aud URL and signer SA) passes
    its own. A signer of "" disables the binding check (only aud is enforced).
    """
    audience = audience or _CHAT_AUDIENCE
    signer_email = _CHAT_SIGNER_EMAIL if signer_email is None else signer_email
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return False, "no bearer token"
    token = auth_header.split(" ", 1)[1].strip()
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as g_requests
        # verify_oauth2_token validates the signature against Google's standard
        # certs, that iss is accounts.google.com, expiry, and aud == audience.
        claims = id_token.verify_oauth2_token(
            token, g_requests.Request(), audience=audience
        )
    except Exception as e:
        c = _peek_jwt_claims(token)
        return False, (f"verify failed: {e} | unverified iss={c.get('iss')!r} "
                       f"aud={c.get('aud')!r} email={c.get('email')!r}")
    # Bind to OUR add-on: the token must be signed by our project's add-on SA.
    if signer_email and (claims.get("email") != signer_email
                         or not claims.get("email_verified", False)):
        return False, f"unexpected signer: {claims.get('email')!r}"
    return True, "ok (addon JWT: aud + signer verified)"


async def _safe_event_json(request: Request) -> Optional[dict]:
    """Parse a webhook body, returning None instead of raising on bad/empty JSON.

    Google Chat retries any non-2xx response, so a malformed body must not 500 —
    we ack with 200 and drop it rather than invite a retry storm.
    """
    try:
        data = await request.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


@app.post("/webhooks/google-chat")
async def google_chat_webhook(request: Request):
    _log = logging.getLogger("winefornia.main")
    mode = (os.getenv("GCHAT_VERIFY", "observe") or "observe").lower()

    if mode != "off":
        ok, reason = await asyncio.to_thread(
            _verify_google_chat_token, request.headers.get("authorization", "")
        )
        _log.info("[gc:auth] %s — %s (mode=%s)", "ok" if ok else "FAILED", reason, mode)
        if mode == "enforce" and not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    event = await _safe_event_json(request)
    if event is None:
        _log.warning("[gc:webhook] unparseable body — acking empty")
        return {}
    # Diagnostic: surface the raw event shape so we can tell why messages
    # may not be routed (legacy vs newer Chat payload formats).
    try:
        _log.info(
            "[gc:webhook:raw] type=%r top_keys=%s msg_keys=%s chat_keys=%s",
            event.get("type"),
            list(event.keys()),
            list((event.get("message") or {}).keys()),
            list((event.get("chat") or {}).keys()),
        )
    except Exception:
        pass
    # Front door: the intent-routing chat agent. It extracts any attached/linked
    # doc to full text, keeps the user's message, and routes by intent (answer a
    # question, look up/edit pricing, or create an invoice). The deterministic
    # order wizard is preserved at /webhooks/google-chat/graph.
    from app.adapters.google_chat_invoice_chat import handle_invoice_chat_event
    return await handle_invoice_chat_event(event)


@app.post("/webhooks/google-chat/graph")
async def google_chat_graph_webhook(request: Request):
    """The original deterministic invoice-graph wizard (order-only), kept as a
    fallback. The default /webhooks/google-chat now routes to the chat agent."""
    _log = logging.getLogger("winefornia.main")
    mode = (os.getenv("GCHAT_VERIFY", "observe") or "observe").lower()
    if mode != "off":
        ok, reason = await asyncio.to_thread(
            _verify_google_chat_token, request.headers.get("authorization", "")
        )
        _log.info("[gc:auth:graph] %s — %s (mode=%s)", "ok" if ok else "FAILED", reason, mode)
        if mode == "enforce" and not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    event = await _safe_event_json(request)
    if event is None:
        return {}
    return await handle_google_chat_event(event)


@app.post("/webhooks/google-chat/tastingroom")
async def google_chat_tastingroom_webhook(request: Request):
    """Approval channel for the tasting room — a SEPARATE Chat app/bot identity.

    Verified against its own audience + signer (its GCP project differs from the
    invoice app's), then dispatched to the tasting-room adapter where card clicks
    resume process_action_decision().
    """
    from app.config import GOOGLE_CHAT_TR_AUDIENCE, GOOGLE_CHAT_TR_SIGNER_EMAIL
    from app.adapters.google_chat_tastingroom import handle_tastingroom_event
    _log = logging.getLogger("winefornia.main")
    mode = (os.getenv("GCHAT_VERIFY", "observe") or "observe").lower()

    if mode != "off":
        ok, reason = await asyncio.to_thread(
            _verify_google_chat_token,
            request.headers.get("authorization", ""),
            GOOGLE_CHAT_TR_AUDIENCE,
            GOOGLE_CHAT_TR_SIGNER_EMAIL,
        )
        _log.info("[tr:gc:auth] %s — %s (mode=%s)", "ok" if ok else "FAILED", reason, mode)
        if mode == "enforce" and not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    event = await _safe_event_json(request)
    if event is None:
        _log.warning("[tr:gc:webhook] unparseable body — acking empty")
        return {}
    return await handle_tastingroom_event(event)


@app.post("/webhooks/google-chat/invoice-chat")
async def google_chat_invoice_chat_webhook(request: Request):
    """Conversational invoicing assistant — a SIBLING to /webhooks/google-chat.

    The invoice graph keeps running on the main route; this route is the free-form
    chat brain (vertex_agent.invoice_chat_agent) that understands intent and acts
    through confirm-first tools. Verified against its own audience + signer when
    configured, then dispatched to the invoice-chat adapter.
    """
    from app.config import GOOGLE_CHAT_INVCHAT_AUDIENCE, GOOGLE_CHAT_INVCHAT_SIGNER_EMAIL
    from app.adapters.google_chat_invoice_chat import handle_invoice_chat_event
    _log = logging.getLogger("winefornia.main")
    mode = (os.getenv("GCHAT_VERIFY", "observe") or "observe").lower()

    if mode != "off":
        ok, reason = await asyncio.to_thread(
            _verify_google_chat_token,
            request.headers.get("authorization", ""),
            GOOGLE_CHAT_INVCHAT_AUDIENCE,
            GOOGLE_CHAT_INVCHAT_SIGNER_EMAIL or None,
        )
        _log.info("[inv:gc:auth] %s — %s (mode=%s)", "ok" if ok else "FAILED", reason, mode)
        if mode == "enforce" and not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    event = await _safe_event_json(request)
    if event is None:
        _log.warning("[inv:gc:webhook] unparseable body — acking empty")
        return {}
    return await handle_invoice_chat_event(event)


@app.get("/webhooks/google-chat")
@app.get("/webhooks/google-chat/tastingroom")
@app.get("/webhooks/google-chat/invoice-chat")
async def google_chat_webhook_healthcheck():
    """200 for Google's Workspace Add-on endpoint reachability pings (GET), which
    would otherwise log as 405 noise. Real Chat events always arrive via POST."""
    return {"status": "ok"}


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, limit: int = 20, key: str = ""):
    """Unified activity history — invoices + tasting room reservations.

    Set ACTIVITY_API_KEY env var to require ?key=VALUE authentication.
    If ACTIVITY_API_KEY is unset, the page is open (dev mode).
    """
    from app.config import ACTIVITY_API_KEY
    if ACTIVITY_API_KEY and key != ACTIVITY_API_KEY:
        return HTMLResponse(
            content="<html><body style='font-family:sans-serif;padding:48px'>"
                    "<h2>403 — Access denied</h2>"
                    "<p>Append <code>?key=YOUR_KEY</code> to the URL.</p></body></html>",
            status_code=403,
        )
    limit = min(max(1, limit), 50)
    from services.activity_service import render_html_activity_page
    return HTMLResponse(content=render_html_activity_page(limit=limit))


@app.on_event("startup")
async def _start_heartbeat_monitor():
    """Launch the tasting-room watcher-liveness monitor in the web process so a
    silent/dead watcher gets surfaced as a Google Chat alert. Disable with
    TR_HEARTBEAT_MONITOR=off."""
    if (os.getenv("TR_HEARTBEAT_MONITOR", "on") or "on").lower() == "off":
        return
    from services.heartbeat_monitor import run_monitor
    asyncio.create_task(run_monitor())


@app.get("/health")
def health():
    """Liveness + watcher freshness. Returns 503 when the tasting-room watcher's
    heartbeat is stale, so an external uptime monitor can also catch it."""
    from services.heartbeat_monitor import _STALE_SECONDS, heartbeat_age_seconds

    age = heartbeat_age_seconds()
    watcher = "unknown" if age is None else ("ok" if age <= _STALE_SECONDS else "stale")
    body = {
        "status": "ok",
        "service": "winefornia-invoice-agent",
        "tastingroom_watcher": watcher,
        "watcher_heartbeat_age_seconds": None if age is None else round(age, 1),
    }
    if watcher == "stale":
        return JSONResponse(status_code=503, content={**body, "status": "degraded"})
    return body
