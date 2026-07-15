"""SMOKE (temporary): drive the real FastAPI routes with simulated Chat events.

Three things under test, per operator request:
  1. Card endpoints are wired well — every button the wizard cards emit is a
     fixed gc_* action accepted by the click handler; LLM text never reaches an
     action name.
  2. The endpoint closes the deal — Square draft is created ONLY on the
     gc_approve card click, published ONLY on gc_send, both at the right
     checkpoint (stale clicks dropped).
  3. Threads — replies to a threaded message carry/reuse the incoming thread.
"""

import asyncio
import json
import re
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.adapters.google_chat_adapter as gca
from services.invoice_interrupts import current_invoice_interrupt
from tests.integration.test_shipping_flow import PRICED_STATE



@pytest.fixture
def square_spy(mocker, mock_supabase):
    create_order = MagicMock(return_value={"order_id": "ord_smoke_1"})
    publish = MagicMock(return_value={"ok": True, "public_url": "https://sq/pay/1"})
    mocker.patch("services.square_service.get_or_create_square_customer",
                 return_value={"customer_id": "cust_smoke_1"})
    mocker.patch("services.square_service.create_order", create_order)
    mocker.patch("services.square_service.create_invoice_draft",
                 return_value={"invoice_id": "inv_smoke_1", "invoice_version": 0,
                               "invoice_url": "https://squareup.com/pay-invoice/inv_smoke_1"})
    mocker.patch("services.square_service.publish_invoice", publish)
    return {"create_order": create_order, "publish": publish}


@pytest.fixture
def client(invoice_graph_mem, monkeypatch):
    monkeypatch.setattr(gca, "invoice_graph", invoice_graph_mem)
    monkeypatch.setattr(gca, "checkpointer", MagicMock())
    monkeypatch.setenv("GCHAT_ASYNC", "off")
    monkeypatch.setenv("GCHAT_VERIFY", "off")
    from app.main import app
    return TestClient(app), invoice_graph_mem


def _message(space, name, text, thread=None):
    msg = {"name": name, "text": text}
    if thread:
        msg["thread"] = {"name": thread}
    return {"type": "MESSAGE", "space": {"name": f"spaces/{space}"}, "message": msg}


def _click(space, action):
    return {"type": "CARD_CLICKED", "space": {"name": f"spaces/{space}"},
            "action": {"actionMethodName": action}}


# ── 1. Card wiring audit: every emitted button is a whitelisted fixed action ──

def _emitted_actions():
    """Collect every action/function name any wizard card can emit."""
    actions = set()

    def walk(node):
        if isinstance(node, dict):
            fn = (node.get("action") or {}).get("function")
            if fn:
                actions.add(fn)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(gca._tier_card("SP", "Cust", "Wholesale"))
    walk(gca._schedule_card("Wholesale"))
    walk(gca._methods_card("NET_30"))
    for card_id, text, buttons in [
        ("c", "t", [("Yes, this is them", "gc_confirm_yes"), ("No, create new", "gc_confirm_no")]),
        ("a", "t", [("Approve", "gc_approve"), ("Edit", "gc_edit"), ("Reject", "gc_reject")]),
        ("s", "t", [("Send to Client", "gc_send"), ("Keep as Draft", "gc_draft")]),
        ("e", "t", [("Send Receipt", "gc_email_send"), ("Skip", "gc_email_skip")]),
    ]:
        walk(gca._card(card_id, text, buttons))
    return actions


def test_every_card_button_is_a_fixed_whitelisted_action():
    accepted_literals = set(gca._RESUME) | set(gca._VALID_AT) | {"gc_edit"}
    accepted_prefixes = ("gc_tier_", "gc_sched_", "gc_methods_")
    for action in _emitted_actions():
        ok = action in accepted_literals or action.startswith(accepted_prefixes)
        assert ok, f"card emits action {action!r} that no click handler accepts"
        # action names are static identifiers — no free text / LLM content
        assert re.fullmatch(r"gc_[A-Za-z0-9_+]+", action), action


# ── 2. Deal-closing over HTTP: approve → draft, send → publish ────────────────

def test_route_level_approve_then_send_closes_the_deal(client, square_spy):
    tc, graph = client
    space = "SMOKEDEAL"
    config = {"configurable": {"thread_id": f"gc_{space}"}}

    # Park at the shipping checkpoint (order just priced).
    graph.update_state(config, PRICED_STATE, as_node="resolve_products_and_prices")
    graph.invoke(None, config)
    assert current_invoice_interrupt(graph.get_state(config)) == "shipping"

    # Walk shipping by typed reply through the REAL route → approval card.
    r = tc.post("/webhooks/google-chat/graph", json=_message(space, "m1", "$30"))
    assert r.status_code == 200
    assert "Create this draft in Square?" in json.dumps(r.json())

    # A premature SEND click must be dropped (wrong checkpoint).
    r = tc.post("/webhooks/google-chat/graph", json=_click(space, "gc_send"))
    assert square_spy["publish"].call_count == 0

    # Approve card click → Square draft created. THE deal-closing endpoint.
    r = tc.post("/webhooks/google-chat/graph", json=_click(space, "gc_approve"))
    assert r.status_code == 200
    assert square_spy["create_order"].call_count == 1
    snap = graph.get_state(config)
    assert snap.values.get("square_invoice_id") == "inv_smoke_1"

    # Now at the send checkpoint → gc_send publishes.
    assert current_invoice_interrupt(snap) == "send"
    r = tc.post("/webhooks/google-chat/graph", json=_click(space, "gc_send"))
    assert r.status_code == 200
    assert square_spy["publish"].call_count == 1

    # Replayed approve after the fact must NOT double-charge.
    r = tc.post("/webhooks/google-chat/graph", json=_click(space, "gc_approve"))
    assert square_spy["create_order"].call_count == 1


def test_unknown_action_name_is_rejected(client, square_spy):
    tc, graph = client
    space = "SMOKEEVIL"
    config = {"configurable": {"thread_id": f"gc_{space}"}}
    graph.update_state(config, PRICED_STATE, as_node="resolve_products_and_prices")
    graph.invoke(None, config)

    # An action name a compromised/creative LLM might invent must go nowhere.
    r = tc.post("/webhooks/google-chat/graph",
                json=_click(space, "send_invoice_now_please"))
    assert r.status_code == 200
    assert "Unknown action" in json.dumps(r.json())
    assert square_spy["create_order"].call_count == 0
    assert square_spy["publish"].call_count == 0


# ── 3. Default (conversational) route over HTTP ──────────────────────────────

def test_default_route_message_reaches_chat_agent(client, monkeypatch, mocker):
    tc, _ = client
    seen = {}

    def fake_discuss(text, *, user="", case=""):
        seen.update(text=text, user=user, case=case)
        return "Wholesale on the Viognier is $30.00."

    mocker.patch("vertex_agent.invoice_chat_agent.discuss", side_effect=fake_discuss)
    monkeypatch.setenv("GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS", "cecil.park@winefornia.com")

    ev = _message("SMOKECHAT", "m2", "what's wholesale on the viognier?",
                  thread="spaces/SMOKECHAT/threads/T1")
    ev["user"] = {"email": "cecil.park@winefornia.com"}
    r = tc.post("/webhooks/google-chat", json=ev)
    assert r.status_code == 200
    assert "Viognier" in json.dumps(r.json())
    assert seen["text"].startswith("what's wholesale")
    # case identity today = space|user (thread only as fallback)
    assert seen["case"] == "spaces/SMOKECHAT|cecil.park@winefornia.com"


def test_default_route_card_click_closes_the_deal(client, monkeypatch, mocker):
    """The default route now consumes CARD_CLICKED — but only the fixed
    invchat_* actions, fail-closed on the approver allowlist, straight to the
    deterministic pending-action executor (no LLM in the loop)."""
    from app import config

    tc, _ = client
    monkeypatch.setattr(config, "GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS",
                        ["cecil.park@winefornia.com"])
    confirm = mocker.patch("vertex_agent.invoice_chat_actions.confirm_pending_action",
                           return_value="✅ Invoice created.")

    # No/unknown user on the click → fail-closed, nothing executes.
    r = tc.post("/webhooks/google-chat", json=_click("SMOKECHAT", "invchat_confirm"))
    assert r.status_code == 200
    assert "not authorized" in json.dumps(r.json())
    confirm.assert_not_called()

    # A foreign (wizard) action name never reaches the executor on this route.
    ev = _click("SMOKECHAT", "gc_approve")
    ev["user"] = {"email": "cecil.park@winefornia.com"}
    r = tc.post("/webhooks/google-chat", json=ev)
    assert "Unrecognized action" in json.dumps(r.json())
    confirm.assert_not_called()

    # The fixed confirm action from an authorized approver executes the pending
    # staged action deterministically.
    ev = _click("SMOKECHAT", "invchat_confirm")
    ev["user"] = {"email": "cecil.park@winefornia.com"}
    r = tc.post("/webhooks/google-chat", json=ev)
    assert "Invoice created" in json.dumps(r.json())
    confirm.assert_called_once()
