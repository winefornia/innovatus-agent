"""
MCP (Model Context Protocol) console for the INVOICE pipeline — Phase 1, read-only.

Exposes a stateless MCP Streamable-HTTP endpoint so Claude (a claude.ai custom
connector, or Claude Code via `claude mcp add --transport http`) can act as an
operator console over the live system: recent invoices, open cases, per-case
traces, staged-but-unconfirmed chat actions, watcher health.

Every tool here is READ-ONLY. Nothing creates, sends, or approves anything —
money and outbound email stay behind the Google Chat confirm-first flow
(Hard Rule 3). Tasting-room data is deliberately out of scope (Hard Rule 2);
a separate server can be added for it later.

Auth — fail-closed, same philosophy as the AUTHORIZED_EMAILS lists:
  - the secret comes from the MCP_INVOICE_SECRET env var (a Fly secret),
    read at request time (like GCHAT_VERIFY, it is not listed in config.py)
  - unset / blank / shorter than 16 chars → every request is denied (403)
  - clients present it either as `Authorization: Bearer <secret>` (Claude
    Code) or as a path segment /mcp/invoice/<secret> (claude.ai custom
    connectors cannot set custom headers); both compared constant-time

Protocol: JSON-RPC 2.0 over POST (MCP Streamable HTTP, stateless). We always
answer with plain JSON (never SSE), issue no session id, and reject batches;
GET/DELETE answer 405. The methods Claude actually uses: initialize,
notifications/*, ping, tools/list, tools/call.
"""

import hmac
import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger(__name__)

router = APIRouter()

# Protocol revisions we know how to speak; echo the client's if recognized.
_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_SERVER_INFO = {"name": "winefornia-invoice-ops", "version": "0.1.0"}

_MIN_SECRET_LEN = 16


# ── auth (fail-closed) ────────────────────────────────────────────────────────

def _configured_secret() -> str:
    """The shared secret, or "" when the endpoint must stay closed."""
    secret = (os.getenv("MCP_INVOICE_SECRET") or "").strip()
    if len(secret) < _MIN_SECRET_LEN:
        return ""
    return secret


def _authorized(request: Request, path_secret: str) -> bool:
    secret = _configured_secret()
    if not secret:
        return False
    supplied = path_secret
    if not supplied:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, secret)


# ── tools (all read-only) ─────────────────────────────────────────────────────

def _clamp_limit(value, default: int, lo: int = 1, hi: int = 50) -> int:
    try:
        return min(max(int(value), lo), hi)
    except (TypeError, ValueError):
        return default


def _tool_list_recent_invoices(args: dict):
    from db.repository import list_recent_invoices
    return list_recent_invoices(limit=_clamp_limit(args.get("limit"), 20))


def _tool_list_unverified_invoices(args: dict):
    from db.repository import list_unverified_invoices
    return list_unverified_invoices(limit=_clamp_limit(args.get("limit"), 25))


def _tool_list_recent_cases(args: dict):
    from db.repository import list_recent_cases
    return list_recent_cases(
        limit=_clamp_limit(args.get("limit"), 20),
        status=str(args.get("status") or ""),
    )


def _tool_get_case(args: dict):
    from db.repository import get_case_row
    case_id = str(args.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("case_id is required")
    return get_case_row(case_id) or {"error": f"no case with case_id {case_id!r}"}


def _tool_get_case_trace(args: dict):
    from db.repository import list_trace_events_for_case
    case_id = str(args.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("case_id is required")
    return list_trace_events_for_case(case_id, limit=_clamp_limit(args.get("limit"), 100, hi=200))


def _tool_list_pending_confirmations(args: dict):
    from db.repository import list_chat_pending
    return list_chat_pending(limit=_clamp_limit(args.get("limit"), 20))


def _tool_check_health(args: dict):
    from services.heartbeat_monitor import _STALE_SECONDS, heartbeat_age_seconds
    age = heartbeat_age_seconds()
    watcher = "unknown" if age is None else ("ok" if age <= _STALE_SECONDS else "stale")
    return {
        "service": "winefornia-invoice-agent",
        "tastingroom_watcher": watcher,
        "watcher_heartbeat_age_seconds": None if age is None else round(age, 1),
    }


_LIMIT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 50,
                 "description": "Max rows to return"}
_CASE_ID_SCHEMA = {"type": "string", "description": "The agent_cases case_id"}

# name → (description, inputSchema, handler)
_TOOLS: dict = {
    "list_recent_invoices": (
        "Recent invoice logs, newest first: customer, tier, total, approval and "
        "Square-verification status.",
        {"type": "object", "properties": {"limit": _LIMIT_SCHEMA}},
        _tool_list_recent_invoices,
    ),
    "list_unverified_invoices": (
        "Invoices still awaiting Square's own confirmation email (open "
        "verification loop) — the backlog to chase if a case never closed.",
        {"type": "object", "properties": {"limit": _LIMIT_SCHEMA}},
        _tool_list_unverified_invoices,
    ),
    "list_recent_cases": (
        "Recent agent cases (intent → outcome lifecycles), newest first. "
        "Optionally filter by status: running | completed | failed | escalated "
        "| abandoned.",
        {"type": "object", "properties": {"limit": _LIMIT_SCHEMA,
                                          "status": {"type": "string",
                                                     "description": "Optional status filter"}}},
        _tool_list_recent_cases,
    ),
    "get_case": (
        "Full agent_cases row for one case: input, intent, risk, status, "
        "outcome, error summary.",
        {"type": "object", "properties": {"case_id": _CASE_ID_SCHEMA},
         "required": ["case_id"]},
        _tool_get_case,
    ),
    "get_case_trace": (
        "Step-by-step trace of one case (oldest first): guardrail checks, tool "
        "calls, interrupts, human decisions, failures.",
        {"type": "object", "properties": {"case_id": _CASE_ID_SCHEMA,
                                          "limit": {**_LIMIT_SCHEMA, "maximum": 200}},
         "required": ["case_id"]},
        _tool_get_case_trace,
    ),
    "list_pending_confirmations": (
        "Staged-but-unconfirmed chat actions (send email / cancel / revoke) "
        "waiting for a human 'yes' in Google Chat. Read-only: approving still "
        "happens in Chat.",
        {"type": "object", "properties": {"limit": _LIMIT_SCHEMA}},
        _tool_list_pending_confirmations,
    ),
    "check_health": (
        "Service liveness and mail-watcher heartbeat freshness (same source "
        "as GET /health).",
        {"type": "object", "properties": {}},
        _tool_check_health,
    ),
}


# ── JSON-RPC plumbing ─────────────────────────────────────────────────────────

def _rpc_result(msg_id, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                         "error": {"code": code, "message": message}})


def _handle_tools_call(msg_id, params: dict) -> JSONResponse:
    name = str(params.get("name") or "")
    tool = _TOOLS.get(name)
    if tool is None:
        return _rpc_error(msg_id, -32602, f"Unknown tool: {name!r}")
    _, _, handler = tool
    arguments = params.get("arguments") or {}
    try:
        data = handler(arguments if isinstance(arguments, dict) else {})
        text = json.dumps(data, default=str, ensure_ascii=False)
        is_error = False
    except Exception as exc:  # surfaced to Claude as a tool error, not a crash
        log.warning("[mcp:invoice] tool %s failed: %s", name, exc)
        text, is_error = f"Tool {name} failed: {exc}", True
    return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}],
                                "isError": is_error})


def _handle_rpc(message: dict) -> Response:
    method = message.get("method")
    msg_id = message.get("id")
    if not isinstance(method, str):
        return _rpc_error(msg_id, -32600, "Invalid request: missing method")

    # Notifications (no id expected) are acknowledged and ignored.
    if method.startswith("notifications/") or msg_id is None:
        return Response(status_code=202)

    if method == "initialize":
        requested = str((message.get("params") or {}).get("protocolVersion") or "")
        version = requested if requested in _PROTOCOL_VERSIONS else _DEFAULT_PROTOCOL_VERSION
        return _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": _SERVER_INFO,
        })

    if method == "ping":
        return _rpc_result(msg_id, {})

    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, (desc, schema, _) in _TOOLS.items()
        ]})

    if method == "tools/call":
        return _handle_tools_call(msg_id, message.get("params") or {})

    return _rpc_error(msg_id, -32601, f"Method not found: {method}")


async def _serve(request: Request, path_secret: str) -> Response:
    if not _authorized(request, path_secret):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    try:
        message = json.loads(await request.body())
    except Exception:
        return _rpc_error(None, -32700, "Parse error")
    if isinstance(message, list):
        return _rpc_error(None, -32600, "Batch requests are not supported")
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid request")
    return _handle_rpc(message)


# ── routes ────────────────────────────────────────────────────────────────────

@router.post("/mcp/invoice")
async def mcp_invoice(request: Request):
    return await _serve(request, path_secret="")


@router.post("/mcp/invoice/{secret}")
async def mcp_invoice_path_secret(secret: str, request: Request):
    return await _serve(request, path_secret=secret)


@router.get("/mcp/invoice")
@router.get("/mcp/invoice/{secret}")
@router.delete("/mcp/invoice")
@router.delete("/mcp/invoice/{secret}")
async def mcp_invoice_method_not_allowed(secret: str = ""):
    # Stateless server: no SSE stream to GET, no session to DELETE.
    return Response(status_code=405, headers={"Allow": "POST"})
