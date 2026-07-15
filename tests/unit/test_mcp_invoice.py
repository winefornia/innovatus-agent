"""
Unit tests for the read-only invoice MCP console (app/mcp_invoice.py).

The router is mounted on a bare FastAPI app (not app.main) so these tests stay
light and hermetic. Repository reads are monkeypatched — no Supabase needed.

The auth tests are the important ones: the endpoint must be FAIL-CLOSED — a
missing, blank, or too-short MCP_INVOICE_SECRET denies every request.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.mcp_invoice import router

SECRET = "test-secret-0123456789abcdef"  # ≥16 chars, valid


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MCP_INVOICE_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _rpc(client, payload, secret=SECRET, in_path=False):
    if in_path:
        return client.post(f"/mcp/invoice/{secret}", json=payload)
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    return client.post("/mcp/invoice", json=payload, headers=headers)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def _call(name, arguments=None, msg_id=3):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


# ── auth: fail-closed ─────────────────────────────────────────────────────────

def test_denied_when_secret_env_unset(monkeypatch):
    monkeypatch.delenv("MCP_INVOICE_SECRET", raising=False)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    assert _rpc(c, INIT).status_code == 403
    assert _rpc(c, INIT, in_path=True).status_code == 403


def test_denied_when_secret_env_too_short(monkeypatch):
    monkeypatch.setenv("MCP_INVOICE_SECRET", "short")
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    # Even a client presenting the same short value is denied.
    assert _rpc(c, INIT, secret="short").status_code == 403


def test_denied_wrong_secret(client):
    assert _rpc(client, INIT, secret="wrong-secret-0123456789").status_code == 403
    assert _rpc(client, INIT, secret="wrong-secret-0123456789", in_path=True).status_code == 403


def test_denied_missing_credentials(client):
    assert client.post("/mcp/invoice", json=INIT).status_code == 403


def test_get_and_delete_are_405(client):
    assert client.get(f"/mcp/invoice/{SECRET}").status_code == 405
    assert client.delete(f"/mcp/invoice/{SECRET}").status_code == 405


# ── protocol handshake ────────────────────────────────────────────────────────

def test_initialize_handshake(client):
    resp = _rpc(client, INIT)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"] == "winefornia-invoice-ops"
    assert "tools" in body["result"]["capabilities"]


def test_initialize_unknown_version_falls_back(client):
    init = {**INIT, "params": {"protocolVersion": "1999-01-01"}}
    body = _rpc(client, init).json()
    assert body["result"]["protocolVersion"] == "2025-06-18"


def test_initialized_notification_gets_202(client):
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert _rpc(client, note).status_code == 202


def test_ping(client):
    body = _rpc(client, {"jsonrpc": "2.0", "id": 9, "method": "ping"}).json()
    assert body["result"] == {}


def test_unknown_method_is_32601(client):
    body = _rpc(client, {"jsonrpc": "2.0", "id": 9, "method": "resources/list"}).json()
    assert body["error"]["code"] == -32601


def test_batch_requests_rejected(client):
    resp = client.post("/mcp/invoice", json=[INIT, TOOLS_LIST],
                       headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.json()["error"]["code"] == -32600


def test_parse_error_is_32700(client):
    resp = client.post("/mcp/invoice", content=b"not json{{",
                       headers={"Authorization": f"Bearer {SECRET}",
                                "Content-Type": "application/json"})
    assert resp.json()["error"]["code"] == -32700


# ── tools/list ────────────────────────────────────────────────────────────────

def test_tools_list_names_and_schemas(client):
    body = _rpc(client, TOOLS_LIST).json()
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert set(tools) == {
        "list_recent_invoices", "list_unverified_invoices", "list_recent_cases",
        "get_case", "get_case_trace", "list_pending_confirmations", "check_health",
    }
    for t in tools.values():
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"
    assert tools["get_case"]["inputSchema"]["required"] == ["case_id"]


# ── tools/call ────────────────────────────────────────────────────────────────

def _text_payload(resp_json):
    result = resp_json["result"]
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def test_call_list_recent_invoices(client, monkeypatch):
    rows = [{"thread_id": "tg_1", "customer_name": "Oak Barrel", "total_before_tax_cents": 43200}]
    monkeypatch.setattr("db.repository.list_recent_invoices", lambda limit=20: rows)
    body = _rpc(client, _call("list_recent_invoices", {"limit": 5})).json()
    assert _text_payload(body) == rows


def test_call_get_case_trace_via_path_secret(client, monkeypatch):
    rows = [{"event_id": "ev1", "event_type": "tool_call", "layer": "square"}]
    monkeypatch.setattr("db.repository.list_trace_events_for_case",
                        lambda case_id, limit=100: rows if case_id == "case_1" else [])
    body = _rpc(client, _call("get_case_trace", {"case_id": "case_1"}), in_path=True).json()
    assert _text_payload(body) == rows


def test_call_list_pending_confirmations(client, monkeypatch):
    rows = [{"chat_user": "u1", "kind": "send_email", "summary": "receipt to Oak Barrel"}]
    monkeypatch.setattr("db.repository.list_chat_pending", lambda limit=20: rows)
    body = _rpc(client, _call("list_pending_confirmations")).json()
    assert _text_payload(body) == rows


def test_call_check_health(client, monkeypatch):
    monkeypatch.setattr("services.heartbeat_monitor.heartbeat_age_seconds", lambda: 5.0)
    body = _rpc(client, _call("check_health")).json()
    payload = _text_payload(body)
    assert payload["tastingroom_watcher"] == "ok"
    assert payload["watcher_heartbeat_age_seconds"] == 5.0


def test_call_get_case_missing_arg_is_tool_error(client):
    body = _rpc(client, _call("get_case")).json()
    result = body["result"]
    assert result["isError"] is True
    assert "case_id" in result["content"][0]["text"]


def test_call_unknown_tool_is_32602(client):
    body = _rpc(client, _call("approve_invoice", {"case_id": "x"})).json()
    assert body["error"]["code"] == -32602


def test_tool_exception_is_reported_not_raised(client, monkeypatch):
    def boom(limit=20):
        raise RuntimeError("supabase down")
    monkeypatch.setattr("db.repository.list_recent_invoices", boom)
    body = _rpc(client, _call("list_recent_invoices")).json()
    result = body["result"]
    assert result["isError"] is True
    assert "supabase down" in result["content"][0]["text"]
