"""Unit tests for terminal status derivation and WorkflowRecord model."""

import pytest

from services.gateway import _derive_terminal_status
from db.models import WorkflowRecord


class TestDeriveTerminalStatus:
    """_derive_terminal_status maps invoice_graph result dicts to terminal statuses."""

    def test_sent(self):
        result = {"square_invoice_id": "inv_1", "send_decision": "send"}
        assert _derive_terminal_status(result) == "completed_sent"

    def test_draft_saved(self):
        result = {"square_invoice_id": "inv_1", "send_decision": "draft"}
        assert _derive_terminal_status(result) == "completed_draft_saved"

    def test_draft_saved_missing_send_decision(self):
        """send_decision defaults to 'draft' when absent."""
        result = {"square_invoice_id": "inv_1"}
        assert _derive_terminal_status(result) == "completed_draft_saved"

    def test_cancelled_on_rejection(self):
        result = {"approval": "rejected"}
        assert _derive_terminal_status(result) == "cancelled"

    def test_failed_safely_on_error(self):
        result = {"error": "Square API timed out"}
        assert _derive_terminal_status(result) == "failed_safely"

    def test_needs_manual_review_on_reconciliation(self):
        result = {
            "square_invoice_id": "inv_1",
            "send_decision": "send",
            "reconciliation_needed": True,
        }
        assert _derive_terminal_status(result) == "needs_manual_review"

    def test_reconciliation_overrides_sent(self):
        """reconciliation_needed takes priority over any other status."""
        result = {
            "square_invoice_id": "inv_1",
            "send_decision": "send",
            "reconciliation_needed": True,
        }
        assert _derive_terminal_status(result) != "completed_sent"

    def test_empty_result_defaults_to_needs_review(self):
        assert _derive_terminal_status({}) == "needs_manual_review"

    def test_cancelled_overrides_square_id(self):
        """If rejected, status is cancelled even if square_invoice_id somehow set."""
        result = {"approval": "rejected", "square_invoice_id": "inv_1"}
        # reconciliation_needed=False, approval=rejected → cancelled
        assert _derive_terminal_status(result) == "cancelled"


class TestWorkflowRecordModel:
    def test_default_record_id_generated(self):
        rec = WorkflowRecord(
            case_id="case_1",
            bot_type="invoice",
            business_object_type="invoice",
            business_object_id="inv_1",
            status="completed_draft_saved",
            summary="Draft saved",
        )
        assert rec.record_id != ""
        assert len(rec.record_id) == 36  # UUID format

    def test_two_records_have_different_ids(self):
        def make():
            return WorkflowRecord(
                case_id="case_1",
                bot_type="invoice",
                business_object_type="invoice",
                business_object_id="inv_1",
                status="completed_draft_saved",
                summary="Draft saved",
            )
        assert make().record_id != make().record_id

    def test_needs_review_defaults_false(self):
        rec = WorkflowRecord(
            case_id="c1", bot_type="invoice",
            business_object_type="invoice", business_object_id="",
            status="failed_safely", summary="error",
        )
        assert rec.needs_review is False

    def test_all_terminal_statuses_are_valid_strings(self):
        statuses = [
            "completed_draft_saved",
            "completed_sent",
            "completed_reservation_approved",
            "completed_reservation_declined",
            "cancelled",
            "failed_safely",
            "needs_manual_review",
        ]
        for status in statuses:
            rec = WorkflowRecord(
                case_id="c1", bot_type="invoice",
                business_object_type="invoice", business_object_id="",
                status=status, summary="test",
            )
            assert rec.status == status
