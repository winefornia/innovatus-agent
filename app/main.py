"""
FastAPI server — background services and webhook endpoints.

Primary interface: Google Chat. The invoice wizard runs on
/webhooks/google-chat (graph adapter, card clicks + typed replies), and the
free-form invoicing assistant on /webhooks/google-chat/invoice-chat.

Endpoints:
  POST /webhooks/google-chat    — invoice wizard (Google Chat app)
  POST /webhooks/google-chat/invoice-chat — invoicing chat assistant
  POST /webhooks/google-chat/tastingroom  — tasting-room assistant
  POST /webhooks/gmail/tastingroom/poll   — poll Gmail for reservation emails
  GET  /invoices/recent         — recent invoice log
  GET  /reservations/recent     — recent reservation cases
  GET  /activity                — operator activity page
  GET  /health                  — health check

Gmail is used for the tasting room (reservation intake + replies) and for
sending invoice receipt emails to customers — NOT for invoice order intake;
orders come in through Google Chat (typed, pasted, or PDF attachment).
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# The web process runs under uvicorn, which only configures its own loggers.
# Without this, the root logger defaults to WARNING and every app-level INFO
# log (Google Chat adapter, gateway, control layer) is silently dropped.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from app.adapters.google_chat_adapter import handle_google_chat_event

app = FastAPI(title="Winefornia Invoice Agent", version="0.3.0")


def _jsonable_graph_result(result: dict) -> dict:
    """Convert LangGraph result objects to plain JSON-safe structures."""
    out = dict(result or {})
    interrupts = out.get("__interrupt__")
    if interrupts:
        normalized = []
        for item in interrupts:
            if isinstance(item, dict):
                normalized.append(item)
            elif hasattr(item, "value"):
                normalized.append({"value": item.value})
            else:
                normalized.append({"value": item})
        out["__interrupt__"] = normalized
    return out


# ---------------------------------------------------------------------------
# Tasting room — Gmail reservation intake
# ---------------------------------------------------------------------------

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
