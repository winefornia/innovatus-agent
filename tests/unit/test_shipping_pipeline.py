"""End-to-end wiring tests for the shipping-fee confirmation step.

The shipping fee is a human-confirmed value that must survive the whole
pipeline: the confirm_shipping_fee interrupt fires after pricing, the reply is
parsed into shipping_cents, the preview/approval totals include it, and the
Square order carries it as an explicit "Shipping" line item. These tests pin
each hop plus the adapter-facing plumbing (interrupt detection, the dashboard
resume endpoint) so a change to any one layer can't silently orphan the fee.
"""

from unittest.mock import MagicMock

import pytest

import agents.invoice_graph as ig
from services.invoice_interrupts import (
    TEXT_INPUT_INTERRUPTS,
    current_invoice_interrupt,
)


# ── reply parsing ────────────────────────────────────────────────────────────

class TestParseShippingToCents:
    def test_free_variants_mean_zero(self):
        for reply in ("free", "Free shipping", "waive it", "waived", "no shipping", "0", "$0"):
            assert ig._parse_shipping_to_cents(reply) == 0, reply

    def test_dollar_amounts(self):
        assert ig._parse_shipping_to_cents("$30") == 3000
        assert ig._parse_shipping_to_cents("30") == 3000
        assert ig._parse_shipping_to_cents("add on $12.50") == 1250

    def test_garbage_is_none(self):
        assert ig._parse_shipping_to_cents(None) is None
        assert ig._parse_shipping_to_cents("") is None
        assert ig._parse_shipping_to_cents("hmm let me check") is None


# ── the graph node ───────────────────────────────────────────────────────────

class TestConfirmShippingFee:
    def test_skips_when_already_confirmed(self, monkeypatch):
        called = []
        monkeypatch.setattr(ig, "interrupt", lambda payload: called.append(payload))
        out = ig.confirm_shipping_fee({"shipping_cents": 0, "pricing_result": {}})
        assert out == {} and not called

    def test_parses_reply_into_state_and_pricing(self, monkeypatch):
        monkeypatch.setattr(ig, "interrupt", lambda payload: "$30")
        out = ig.confirm_shipping_fee({"pricing_result": {"total_before_tax_cents": 76500}})
        assert out["shipping_cents"] == 3000
        assert out["pricing_result"]["shipping_cents"] == 3000

    def test_unparseable_reply_defaults_free_with_warning(self, monkeypatch):
        monkeypatch.setattr(ig, "interrupt", lambda payload: "ship it to tiburon")
        out = ig.confirm_shipping_fee({"pricing_result": {"total_before_tax_cents": 76500}})
        assert out["shipping_cents"] == 0
        assert any("defaulted to free" in w for w in out["pricing_result"]["warnings"])

    def test_interrupt_payload_type_is_the_wired_one(self, monkeypatch):
        seen = {}
        def fake_interrupt(payload):
            seen.update(payload)
            return "free"
        monkeypatch.setattr(ig, "interrupt", fake_interrupt)
        ig.confirm_shipping_fee({"pricing_result": {"total_before_tax_cents": 100_00}})
        assert seen["type"] == "shipping_fee_confirmation"
        # and that type maps to the canonical name every adapter renders on
        assert current_invoice_interrupt({"__interrupt__": [{"type": "shipping_fee_confirmation"}]}) == "shipping"


# ── routing ──────────────────────────────────────────────────────────────────

class TestRouting:
    def test_pricing_routes_to_shipping_confirm(self):
        state = {"pricing_result": {"line_items": [{"x": 1}]}}
        assert ig._route_after_pricing(state) == "confirm_shipping_fee"

    def test_edit_requiring_reprice_resets_shipping(self):
        # apply_patch must clear shipping_cents so the graph re-asks after
        # quantities/items change (the old fee may no longer make sense).
        state = {
            "edit_patch": {"field_changes": [
                {"field": "quantity", "new_value": 6, "confidence": 0.95},
            ], "requires_price_recalculation": True},
            "line_items": [{"product_name": "Viognier", "quantity": 12}],
            "extracted": {"items": [{"product_name": "Viognier", "quantity": 12}]},
            "shipping_cents": 3000,
        }
        out = ig.apply_patch(state)
        assert out["shipping_cents"] is None


# ── preview / approval totals ────────────────────────────────────────────────

class TestPreviewTotals:
    def test_preview_totals_include_shipping(self):
        state = {
            "customer": {"full_name": "Christina Yoo", "email": "christina@chothompson.com"},
            "tier_name": "Other",
            "line_items": [],
            "pricing_result": {"subtotal_cents": 90000, "discount_cents": 13500,
                               "total_before_tax_cents": 76500, "warnings": [], "blocks": []},
            "shipping_cents": 3000,
        }
        out = ig.create_invoice_preview(state)
        preview = out["invoice_preview"]
        assert preview["wine_total_cents"] == 76500
        assert preview["shipping_cents"] == 3000
        assert preview["total_with_shipping_cents"] == 79500
        assert preview["total_before_tax_cents"] == 79500   # what adapters show as Total
        assert "$795.00" in out["final_response"]

    def test_waived_shipping_shows_and_adds_nothing(self):
        state = {
            "customer": {"full_name": "X"}, "tier_name": "Wholesale", "line_items": [],
            "pricing_result": {"subtotal_cents": 1000, "discount_cents": 0,
                               "total_before_tax_cents": 1000},
            "shipping_cents": 0,
        }
        out = ig.create_invoice_preview(state)
        assert out["invoice_preview"]["total_before_tax_cents"] == 1000
        assert "Shipping: Waived" in out["final_response"]


# ── Square order ─────────────────────────────────────────────────────────────

class TestSquareOrderShippingLine:
    def _capture_order(self, monkeypatch, shipping_cents):
        import services.square_service as ss
        captured = {}

        def fake_create(order, idempotency_key):
            captured.update(order)
            o = MagicMock()
            o.id, o.total_money = "ord_1", None
            return MagicMock(order=o)

        client = MagicMock()
        client.orders.create = fake_create
        monkeypatch.setattr(ss, "_get_client", lambda: client)
        monkeypatch.setattr(ss, "_active_location", lambda: "loc_1")
        result = ss.create_order(
            "Christina Yoo",
            [{"product_name": "Viognier 2023", "quantity": 1, "unit_type": "case",
              "final_unit_price_cents": 6375, "bottles_per_case": 12}],
            shipping_cents=shipping_cents,
        )
        assert result.get("order_id") == "ord_1"
        return captured["line_items"]

    def test_custom_shipping_becomes_a_line_item(self, monkeypatch):
        lines = self._capture_order(monkeypatch, 3000)
        ship = [l for l in lines if l["name"] == "Shipping"]
        assert len(ship) == 1
        assert ship[0]["base_price_money"]["amount"] == 3000
        assert ship[0]["quantity"] == "1"

    def test_waived_shipping_adds_no_line(self, monkeypatch):
        lines = self._capture_order(monkeypatch, 0)
        assert not [l for l in lines if l["name"] == "Shipping"]

    def test_tool_registry_passes_shipping_through(self, monkeypatch):
        import services.square_service as ss
        from services.tool_registry import tool_registry
        seen = {}
        monkeypatch.setattr(ss, "create_order",
                            lambda *a, **kw: seen.update(kw) or {"order_id": "ord_1"})
        tool_registry.dispatch(
            "square_create_order",
            {"customer_name": "X", "line_items": [], "shipping_cents": 3000,
             "idempotency_key": "ik"},
            case_id="case_test",
        )
        assert seen.get("shipping_cents") == 3000


# ── adapter-facing detection ─────────────────────────────────────────────────

class TestInterruptDetection:
    def test_shipping_is_a_text_input_interrupt(self):
        assert "shipping" in TEXT_INPUT_INTERRUPTS

    def test_state_inference_fallback(self):
        state = {"pricing_result": {"total_before_tax_cents": 100}, "shipping_cents": None}
        assert current_invoice_interrupt(state) == "shipping"

    def test_no_shipping_inference_once_preview_exists(self):
        state = {"pricing_result": {"x": 1}, "shipping_cents": 3000,
                 "invoice_preview": {"y": 1}}
        assert current_invoice_interrupt(state) == "approval"


# ── dashboard resume endpoint ────────────────────────────────────────────────

class TestDashboardEndpoints:
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def test_resume_with_nothing_pending_is_409(self, client):
        r = client.post("/agents/invoice/resume",
                        json={"thread_id": "web_nonexistent", "decision": "free"})
        assert r.status_code == 409
        assert "Nothing is waiting" in r.json()["final_response"]

    def test_dashboard_served_at_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "/agents/invoice/run" in r.text

    def test_jsonable_graph_result_normalizes_interrupts(self):
        from app.main import _jsonable_graph_result

        class FakeInterrupt:
            value = {"type": "shipping_fee_confirmation", "question": "Shipping?"}

        out = _jsonable_graph_result({"a": 1, "__interrupt__": [FakeInterrupt()]})
        assert out["a"] == 1
        assert out["__interrupt__"] == [{"value": {"type": "shipping_fee_confirmation",
                                                   "question": "Shipping?"}}]
