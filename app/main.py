"""
FastAPI server — background services and webhook endpoints.

Primary interface: Telegram bot via `python bot.py` (long polling, 24/7).

Endpoints:
  POST /intake                  — generic text intake (email forward, Zapier, etc.)
  POST /intake/pdf              — direct PDF upload
  POST /webhooks/email          — Mailgun / SendGrid inbound parse webhook
  POST /webhooks/gmail/poll     — poll Gmail "To Invoice" label
  GET  /invoices/recent         — recent invoice log
  GET  /health                  — health check
"""

import uuid
from typing import Optional

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

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

@app.post("/intake")
def intake(req: IntakeRequest):
    """Feed any text (forwarded email, Zapier trigger, etc.) into the agent.

    Returns the agent's first response and a thread_id for follow-up resumes.
    """
    msg    = from_api(req.message, sender_id=req.sender_id, thread_id=req.thread_id)
    result = gateway.dispatch(msg)
    if result.get("error") and not result.get("final_response"):
        return JSONResponse(status_code=500, content=result)
    return result


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
        return JSONResponse(status_code=500, content=result)
    return {"extracted_message": message, **result}


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


@app.post("/webhooks/google-chat")
async def google_chat_webhook(request: Request):
    event = await request.json()
    return await handle_google_chat_event(event)


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


@app.get("/health")
def health():
    return {"status": "ok", "service": "winefornia-invoice-agent"}
