"""Full-graph wiring test for the shipping-fee confirmation step.

Drives the REAL compiled invoice graph (MemorySaver) from post-pricing state
through the interrupt chain an operator sees:

    pricing done → shipping question → "$30" → approval preview → "approved"
                 → Square order carries the $30 Shipping line

This is the test that catches a broken edge, an unmapped interrupt type, or a
node that drops shipping_cents between hops — things the per-node unit tests
in tests/unit/test_shipping_pipeline.py can't see.
"""

from unittest.mock import MagicMock

import pytest
from langgraph.types import Command

from services.invoice_interrupts import current_invoice_interrupt


PRICED_STATE = {
    "raw_message": "Invoice Christina Yoo — 1 case Viognier 2023, 15% off, ship to Tiburon",
    "sender_id": "test_ship",
    "_case_id": "case_ship_test",
    "intent": "invoice_request",
    "customer": {"full_name": "Christina Yoo", "email": "christina@chothompson.com"},
    "customer_confirmed": True,
    "tier_name": "Other",
    "payment_schedule": "NET_30",
    "line_items": [{
        "product_name": "Viognier 2023",
        "quantity": 1, "unit_type": "case", "bottles_per_case": 12,
        "final_unit_price_cents": 6375, "line_total_cents": 76500,
    }],
    "pricing_result": {
        "line_items": [{"product_name": "Viognier 2023", "line_total_cents": 76500}],
        "subtotal_cents": 90000, "discount_cents": 13500,
        "total_before_tax_cents": 76500,
        "warnings": [], "blocks": [],
    },
}


@pytest.fixture
def square_spy(mocker, mock_supabase):
    """Patch the Square service layer, capturing create_order kwargs."""
    create_order = MagicMock(return_value={"order_id": "ord_ship_001"})
    mocker.patch("services.square_service.get_or_create_square_customer",
                 return_value={"customer_id": "cust_ship_001"})
    mocker.patch("services.square_service.create_order", create_order)
    mocker.patch("services.square_service.create_invoice_draft",
                 return_value={"invoice_id": "inv_ship_001", "invoice_version": 0,
                               "invoice_url": "https://squareup.com/pay-invoice/inv_ship_001"})
    mocker.patch("services.square_service.publish_invoice", return_value={"ok": True})
    return create_order


def test_shipping_interrupt_chain_to_square(invoice_graph_mem, square_spy):
    g = invoice_graph_mem
    config = {"configurable": {"thread_id": "t_ship_e2e"}}

    # Land the thread exactly where pricing finishes, then let the graph route.
    g.update_state(config, PRICED_STATE, as_node="resolve_products_and_prices")
    result = g.invoke(None, config)

    # 1. It must park on the shipping question (payload-typed, text-answerable).
    assert current_invoice_interrupt(result) == "shipping"
    payload = result["__interrupt__"][0].value
    assert "free" in payload["question"] and "$30" in payload["question"]

    # 2. The operator's "$30" lands in state and the approval preview totals.
    result = g.invoke(Command(resume="$30"), config)
    assert current_invoice_interrupt(result) == "approval"
    preview = result["invoice_preview"]
    assert preview["shipping_cents"] == 3000
    assert preview["wine_total_cents"] == 76500
    assert preview["total_before_tax_cents"] == 79500
    assert "Shipping: $30.00" in preview["preview_text"]
    assert "Total before tax: $795.00" in preview["preview_text"]

    # 3. Approval creates the Square order WITH the confirmed shipping fee.
    result = g.invoke(Command(resume="approved"), config)
    assert result.get("square_invoice_id") == "inv_ship_001"
    assert square_spy.call_count == 1
    assert square_spy.call_args.kwargs["shipping_cents"] == 3000


def test_waived_shipping_reaches_square_as_no_line(invoice_graph_mem, square_spy):
    g = invoice_graph_mem
    config = {"configurable": {"thread_id": "t_ship_free"}}

    g.update_state(config, PRICED_STATE, as_node="resolve_products_and_prices")
    result = g.invoke(None, config)
    assert current_invoice_interrupt(result) == "shipping"

    result = g.invoke(Command(resume="free"), config)
    assert current_invoice_interrupt(result) == "approval"
    assert result["invoice_preview"]["shipping_cents"] == 0
    assert result["invoice_preview"]["total_before_tax_cents"] == 76500
    assert "Shipping: Waived" in result["invoice_preview"]["preview_text"]

    result = g.invoke(Command(resume="approved"), config)
    assert result.get("square_invoice_id") == "inv_ship_001"
    # 0 means "confirmed free" — create_order receives it and adds no line.
    assert square_spy.call_args.kwargs["shipping_cents"] == 0
