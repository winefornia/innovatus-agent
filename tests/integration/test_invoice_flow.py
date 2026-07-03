"""
Integration tests for the invoice flow with mocked external APIs.

These tests call individual graph nodes (and the full graph for simple paths)
with mocked Square / Supabase / LLM dependencies. They verify:
  - the right external calls are made (or not made)
  - terminal state is set correctly
  - reconciliation_needed is flagged on partial failure
  - rejection produces no Square side effects

All fixtures come from tests/conftest.py.
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agents.invoice_graph import (
    classify_intent,
    create_square_invoice_draft,
    _parse_approval,
    _route_after_approval,
    respond,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_approved_state():
    """Minimal InvoiceState dict that has passed approval."""
    return {
        "raw_message": "Invoice Oak Barrel for 3 cases Cab Sauv",
        "sender_id": "tg_test",
        "_case_id": "case_test_001",
        "intent": "invoice_request",
        "approval": "approved",
        "customer": {
            "id": "cust_db_001",
            "full_name": "Oak Barrel Restaurant",
            "company": "Oak Barrel",
            "email": "orders@oakbarrel.com",
            "phone": None,
            "tier_name": "wholesale",
            "square_customer_id": None,
        },
        "customer_confirmed": True,
        "tier_name": "wholesale",
        "payment_schedule": "NET_30",
        "payment_methods": ["CARD", "BANK_ACCOUNT"],
        "line_items": [
            {
                "product_name": "Cabernet Sauvignon",
                "sku": "CAB",
                "quantity": 3,
                "unit_price_cents": 14400,
                "line_total_cents": 43200,
                "vintage": 2022,
            }
        ],
        "pricing_result": {
            "subtotal_cents": 43200,
            "discount_cents": 12960,
            "total_before_tax_cents": 30240,
            "shipping_cents": 0,
            "line_items": [],
            "blocks": [],
            "warnings": [],
        },
        "invoice_preview": {
            "customer": {"full_name": "Oak Barrel Restaurant"},
            "tier_name": "wholesale",
            "line_items": [],
            "subtotal_cents": 43200,
            "discount_cents": 12960,
            "total_before_tax_cents": 30240,
            "shipping_cents": 0,
            "payment_schedule": "NET_30",
            "payment_methods": ["CARD", "BANK_ACCOUNT"],
            "preview_text": "Draft invoice for Oak Barrel Restaurant...",
        },
    }


# ---------------------------------------------------------------------------
# Scenario 1 — golden path: approved → Square draft created
# ---------------------------------------------------------------------------

class TestGoldenPathDraft:
    def test_square_customer_created(self, mocker, mock_supabase):
        mock_create_customer = mocker.patch(
            "services.square_service.get_or_create_square_customer",
            return_value={"customer_id": "cust_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_order",
            return_value={"order_id": "ord_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_invoice_draft",
            return_value={
                "invoice_id": "inv_sq_001",
                "invoice_version": 0,
                "invoice_url": "https://squareup.com/pay-invoice/inv_sq_001",
            },
        )
        mocker.patch("services.approval_service.log_approval_event", return_value=None)

        state = _base_approved_state()
        result = create_square_invoice_draft(state)

        assert result.get("square_invoice_id") == "inv_sq_001"
        mock_create_customer.assert_called_once()

    def test_square_order_created(self, mocker, mock_supabase):
        mocker.patch(
            "services.square_service.get_or_create_square_customer",
            return_value={"customer_id": "cust_sq_001"},
        )
        mock_create_order = mocker.patch(
            "services.square_service.create_order",
            return_value={"order_id": "ord_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_invoice_draft",
            return_value={
                "invoice_id": "inv_sq_001",
                "invoice_version": 0,
                "invoice_url": "https://squareup.com/pay-invoice/inv_sq_001",
            },
        )
        mocker.patch("services.approval_service.log_approval_event", return_value=None)

        result = create_square_invoice_draft(_base_approved_state())
        assert result.get("square_invoice_id") == "inv_sq_001"
        mock_create_order.assert_called_once()

    def test_draft_respond_message_contains_invoice_id(self, mocker, mock_supabase):
        mocker.patch(
            "services.square_service.get_or_create_square_customer",
            return_value={"customer_id": "cust_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_order",
            return_value={"order_id": "ord_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_invoice_draft",
            return_value={
                "invoice_id": "inv_sq_001",
                "invoice_version": 0,
                "invoice_url": "https://squareup.com/pay-invoice/inv_sq_001",
            },
        )
        mocker.patch("services.approval_service.log_approval_event", return_value=None)

        # create_square_invoice_draft then respond
        state = {**_base_approved_state(), "send_decision": "draft"}
        state.update(create_square_invoice_draft(state))
        state.update({"send_decision": "draft"})
        final = respond(state)

        assert "inv_sq_001" in final.get("final_response", "")


# ---------------------------------------------------------------------------
# Scenario 2 — rejection: no Square calls made
# ---------------------------------------------------------------------------

class TestRejection:
    def test_no_square_calls_on_rejection(self, mocker):
        mock_create_customer = mocker.patch(
            "services.square_service.get_or_create_square_customer"
        )
        mock_create_order = mocker.patch("services.square_service.create_order")
        mock_create_invoice = mocker.patch("services.square_service.create_invoice_draft")
        mocker.patch("services.approval_service.log_approval_event", return_value=None)

        state = {**_base_approved_state(), "approval": "rejected"}
        result = create_square_invoice_draft(state)

        mock_create_customer.assert_not_called()
        mock_create_order.assert_not_called()
        mock_create_invoice.assert_not_called()

    def test_rejection_returns_no_invoice_id(self, mocker):
        mocker.patch("services.approval_service.log_approval_event", return_value=None)

        state = {**_base_approved_state(), "approval": "rejected"}
        result = create_square_invoice_draft(state)

        assert not result.get("square_invoice_id")

    def test_rejection_final_response_mentions_rejected(self, mocker):
        mocker.patch("services.approval_service.log_approval_event", return_value=None)

        state = {**_base_approved_state(), "approval": "rejected"}
        result = create_square_invoice_draft(state)

        assert "reject" in result.get("final_response", "").lower()


# ---------------------------------------------------------------------------
# Scenario 3 — routing: approval routes correctly
# ---------------------------------------------------------------------------

class TestApprovalRouting:
    def test_approved_routes_to_create_draft(self):
        state = {"approval": "approved"}
        assert _route_after_approval(state) == "create_square_invoice_draft"

    def test_edit_routes_to_interpret_edit(self):
        state = {"approval": "edit_requested"}
        assert _route_after_approval(state) == "interpret_edit"

    def test_rejected_routes_to_create_draft(self):
        # create_square_invoice_draft handles rejection internally
        state = {"approval": "rejected"}
        assert _route_after_approval(state) == "create_square_invoice_draft"

    def test_missing_approval_defaults_to_create_draft(self):
        state = {}
        assert _route_after_approval(state) == "create_square_invoice_draft"


# ---------------------------------------------------------------------------
# Scenario 4 — missing fields: intent=invoice_request, missing_fields set
# ---------------------------------------------------------------------------

class TestMissingFields:
    def test_classify_intent_invoice_keywords(self):
        """Short message with 'invoice' keyword → invoice_request (no missing fields yet)."""
        result = classify_intent({"raw_message": "invoice john"})
        assert result["intent"] == "invoice_request"

    def test_classify_intent_short_is_chat(self):
        result = classify_intent({"raw_message": "hi"})
        assert result["intent"] == "chat"


# ---------------------------------------------------------------------------
# Scenario 5 — Square partial failure: Supabase log fails → reconciliation_needed
# ---------------------------------------------------------------------------

class TestSquarePartialFailure:
    def test_reconciliation_needed_when_supabase_fails(self, mocker):
        """If Square draft is created but Supabase log fails, reconciliation_needed=True."""
        mocker.patch(
            "services.square_service.get_or_create_square_customer",
            return_value={"customer_id": "cust_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_order",
            return_value={"order_id": "ord_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_invoice_draft",
            return_value={
                "invoice_id": "inv_sq_001",
                "invoice_version": 0,
                "invoice_url": "https://squareup.com/pay-invoice/inv_sq_001",
            },
        )
        mocker.patch("services.approval_service.log_approval_event", return_value=None)
        # Make Supabase log fail
        mocker.patch(
            "db.repository.upsert_invoice",
            side_effect=Exception("Supabase connection refused"),
        )
        mocker.patch(
            "db.repository.log_invoice",
            side_effect=Exception("Supabase connection refused"),
        )

        result = create_square_invoice_draft(_base_approved_state())

        assert result.get("square_invoice_id") == "inv_sq_001"
        assert result.get("reconciliation_needed") is True

    def test_reconciliation_reason_set(self, mocker):
        mocker.patch(
            "services.square_service.get_or_create_square_customer",
            return_value={"customer_id": "cust_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_order",
            return_value={"order_id": "ord_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_invoice_draft",
            return_value={
                "invoice_id": "inv_sq_001",
                "invoice_version": 0,
                "invoice_url": "https://squareup.com/pay-invoice/inv_sq_001",
            },
        )
        mocker.patch("services.approval_service.log_approval_event", return_value=None)
        mocker.patch(
            "db.repository.upsert_invoice",
            side_effect=Exception("DB offline"),
        )
        mocker.patch("db.repository.log_invoice", side_effect=Exception("DB offline"))

        result = create_square_invoice_draft(_base_approved_state())

        assert result.get("reconciliation_reason")

    def test_reconciliation_surfaces_in_respond(self, mocker):
        mocker.patch(
            "services.square_service.get_or_create_square_customer",
            return_value={"customer_id": "cust_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_order",
            return_value={"order_id": "ord_sq_001"},
        )
        mocker.patch(
            "services.square_service.create_invoice_draft",
            return_value={
                "invoice_id": "inv_sq_001",
                "invoice_version": 0,
                "invoice_url": "https://squareup.com/pay-invoice/inv_sq_001",
            },
        )
        mocker.patch("services.approval_service.log_approval_event", return_value=None)
        mocker.patch(
            "db.repository.upsert_invoice",
            side_effect=Exception("DB offline"),
        )
        mocker.patch("db.repository.log_invoice", side_effect=Exception("DB offline"))

        draft_result = create_square_invoice_draft(_base_approved_state())
        full_state = {**_base_approved_state(), "send_decision": "draft", **draft_result}
        final = respond(full_state)

        assert "RECONCILIATION" in final.get("final_response", "")


# ---------------------------------------------------------------------------
# Scenario 6 — terminal status derivation
# ---------------------------------------------------------------------------

class TestTerminalStatusDerivation:
    def test_invoice_creation_is_pending_until_square_email_verifies(self):
        # Draft or sent, the case stays OPEN until the Square notification email
        # confirms it (services/invoice_mail_validator.py closes it).
        from services.gateway import _derive_terminal_status
        for result in ({"square_invoice_id": "inv_1", "send_decision": "draft"},
                       {"square_invoice_id": "inv_1", "send_decision": "send"}):
            assert _derive_terminal_status(result) == "pending_verification"

    def test_cancelled_status(self):
        from services.gateway import _derive_terminal_status
        result = {"approval": "rejected"}
        assert _derive_terminal_status(result) == "cancelled"

    def test_needs_review_on_reconciliation(self):
        from services.gateway import _derive_terminal_status
        result = {"square_invoice_id": "inv_1", "reconciliation_needed": True}
        assert _derive_terminal_status(result) == "needs_manual_review"
