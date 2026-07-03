"""Google Chat adapter → invoice graph, end to end.

Google Chat is now the ONLY interactive surface for the invoice wizard, so this
drives the REAL adapter entry point (handle_google_chat_event) against the real
compiled graph — exactly the two hops production traffic makes:

    typed reply "$30" at the shipping checkpoint  → approval card
    gc_approve card click                         → Square draft (with shipping)

Only the Square service layer and the checkpointer are stubbed. This is the
regression net for "the wizard works in Google Chat": a broken resume path,
mis-detected interrupt, or dropped shipping fee all fail here.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import app.adapters.google_chat_adapter as gca
from services.invoice_interrupts import current_invoice_interrupt

from tests.integration.test_shipping_flow import PRICED_STATE  # same seeded order


@pytest.fixture
def square_spy(mocker, mock_supabase):
    create_order = MagicMock(return_value={"order_id": "ord_gc_001"})
    mocker.patch("services.square_service.get_or_create_square_customer",
                 return_value={"customer_id": "cust_gc_001"})
    mocker.patch("services.square_service.create_order", create_order)
    mocker.patch("services.square_service.create_invoice_draft",
                 return_value={"invoice_id": "inv_gc_001", "invoice_version": 0,
                               "invoice_url": "https://squareup.com/pay-invoice/inv_gc_001"})
    mocker.patch("services.square_service.publish_invoice", return_value={"ok": True})
    return create_order


@pytest.fixture
def gchat(invoice_graph_mem, monkeypatch):
    """Point the adapter at the MemorySaver graph and force sync responses."""
    monkeypatch.setattr(gca, "invoice_graph", invoice_graph_mem)
    monkeypatch.setattr(gca, "checkpointer", MagicMock())
    monkeypatch.setenv("GCHAT_ASYNC", "off")
    return invoice_graph_mem


def _message(space: str, name: str, text: str) -> dict:
    return {"type": "MESSAGE", "space": {"name": f"spaces/{space}"},
            "message": {"name": name, "text": text}}

def _click(space: str, action: str) -> dict:
    return {"type": "CARD_CLICKED", "space": {"name": f"spaces/{space}"},
            "action": {"actionMethodName": action}}


def test_gchat_shipping_reply_and_approve_click(gchat, square_spy):
    space = "GCSHIPTEST"
    config = {"configurable": {"thread_id": f"gc_{space}"}}

    # Park the space's invoice at the shipping checkpoint (pricing just done).
    gchat.update_state(config, PRICED_STATE, as_node="resolve_products_and_prices")
    result = gchat.invoke(None, config)
    assert current_invoice_interrupt(result) == "shipping"

    # 1. Staff type "$30" in the space → adapter must resume (not restart) and
    #    come back with the approval card showing shipping-inclusive totals.
    resp = asyncio.run(gca.handle_google_chat_event(
        _message(space, "msg-ship-1", "$30")))
    blob = json.dumps(resp)
    assert "Create this draft in Square?" in blob
    assert "Wine total: $765.00" in blob
    assert "Shipping: $30.00" in blob
    assert "Total: $795.00" in blob

    # 2. Staff click Approve on the card → Square order carries the $30 line.
    resp = asyncio.run(gca.handle_google_chat_event(_click(space, "gc_approve")))
    assert square_spy.call_count == 1
    assert square_spy.call_args.kwargs["shipping_cents"] == 3000
    snapshot = gchat.get_state(config)
    assert snapshot.values.get("square_invoice_id") == "inv_gc_001"


def test_gchat_free_shipping_reply(gchat, square_spy):
    space = "GCSHIPFREE"
    config = {"configurable": {"thread_id": f"gc_{space}"}}

    gchat.update_state(config, PRICED_STATE, as_node="resolve_products_and_prices")
    gchat.invoke(None, config)

    resp = asyncio.run(gca.handle_google_chat_event(
        _message(space, "msg-ship-2", "free")))
    blob = json.dumps(resp)
    assert "Shipping: Waived" in blob
    assert "Total: $765.00" in blob


def test_gchat_stale_approve_click_is_dropped_at_shipping(gchat, square_spy):
    """An old Approve button clicked while the graph waits on shipping must be
    ignored — money actions only fire at the approval checkpoint."""
    space = "GCSHIPSTALE"
    config = {"configurable": {"thread_id": f"gc_{space}"}}

    gchat.update_state(config, PRICED_STATE, as_node="resolve_products_and_prices")
    gchat.invoke(None, config)

    resp = asyncio.run(gca.handle_google_chat_event(_click(space, "gc_approve")))
    assert "already been processed" in json.dumps(resp)
    assert square_spy.call_count == 0
    # and the shipping question is still the active checkpoint
    assert current_invoice_interrupt(gchat.get_state(config)) == "shipping"
