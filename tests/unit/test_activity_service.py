"""Unit tests for the pure formatting functions in services/activity_service."""

import pytest

from services.activity_service import (
    _fmt_ts,
    _fmt_dollars,
    _fmt_invoice,
    _fmt_reservation,
)


class TestFmtTs:
    def test_iso_datetime(self):
        # Naive Supabase timestamps are treated as UTC and rendered in Pacific.
        # 14:14 UTC on 2026-06-03 is 07:14 PDT (UTC-7).
        result = _fmt_ts("2026-06-03T14:14:00")
        assert "Jun" in result
        assert "3" in result
        assert "7:14 am" in result

    def test_none_returns_empty(self):
        assert _fmt_ts(None) == ""

    def test_empty_returns_empty(self):
        assert _fmt_ts("") == ""

    def test_utc_z_suffix(self):
        result = _fmt_ts("2026-06-03T14:14:00Z")
        assert result != ""  # parses without raising

    def test_space_separator(self):
        result = _fmt_ts("2026-06-03 14:14:00")
        assert result != ""


class TestFmtDollars:
    def test_positive_cents(self):
        assert _fmt_dollars(43200) == "$432.00"

    def test_zero(self):
        assert _fmt_dollars(0) == "$0.00"

    def test_none_returns_empty(self):
        assert _fmt_dollars(None) == ""

    def test_fractional_dollars(self):
        assert _fmt_dollars(150) == "$1.50"

    def test_large_amount_formatted(self):
        result = _fmt_dollars(1_500_000)
        assert "15,000" in result


class TestFmtInvoice:
    def _row(self, **kwargs):
        base = {
            "customer_name": "Oak Barrel Restaurant",
            "tier_name": "wholesale",
            "total_before_tax_cents": 43200,
            "approval": "approved",
            "square_invoice_id": "inv_ABC123",
            "created_at": "2026-06-03T14:14:00",
        }
        base.update(kwargs)
        return base

    def test_approved_label(self):
        result = _fmt_invoice(self._row(approval="approved"))
        assert result["outcome_label"] == "Created in Square"
        assert result["outcome_class"] == "ok"

    def test_rejected_label(self):
        result = _fmt_invoice(self._row(approval="rejected"))
        assert result["outcome_label"] == "Rejected"
        assert result["outcome_class"] == "error"

    def test_edit_requested_label(self):
        result = _fmt_invoice(self._row(approval="edit_requested"))
        assert result["outcome_label"] == "Edit requested"
        assert result["outcome_class"] == "warn"

    def test_name_and_tier_present(self):
        result = _fmt_invoice(self._row())
        assert result["name"] == "Oak Barrel Restaurant"
        assert result["tier"] == "wholesale"

    def test_amount_formatted(self):
        result = _fmt_invoice(self._row(total_before_tax_cents=43200))
        assert result["amount"] == "$432.00"

    def test_square_id_passed_through(self):
        result = _fmt_invoice(self._row(square_invoice_id="inv_XYZ"))
        assert result["square_id"] == "inv_XYZ"

    def test_ts_formatted(self):
        result = _fmt_invoice(self._row(created_at="2026-06-03T14:14:00"))
        assert result["ts"] != ""


class TestFmtReservation:
    def _row(self, **kwargs):
        base = {
            "reservation_id": "res_001",
            "client_name": "Smith Family",
            "requested_date": "2026-06-15",
            "requested_time": "14:00",
            "guest_count": 4,
            "experience_type": "cave_experience",
            "current_state": "FINAL_CONFIRMED",
            "updated_at": "2026-06-03T09:22:00",
        }
        base.update(kwargs)
        return base

    def test_confirmed_state(self):
        result = _fmt_reservation(self._row(current_state="FINAL_CONFIRMED"))
        assert result["outcome_label"] == "Confirmed"
        assert result["outcome_class"] == "ok"

    def test_waiting_payment_state(self):
        result = _fmt_reservation(self._row(current_state="WAITING_FOR_PAYMENT"))
        assert result["outcome_class"] == "warn"

    def test_human_review_state(self):
        result = _fmt_reservation(self._row(current_state="HUMAN_REVIEW_REQUIRED"))
        assert result["outcome_class"] == "error"

    def test_unknown_state_defaults_to_neutral(self):
        result = _fmt_reservation(self._row(current_state="SOME_UNKNOWN_STATE"))
        assert result["outcome_class"] == "neutral"

    def test_guest_count_formatted(self):
        result = _fmt_reservation(self._row(guest_count=4))
        assert "4 guests" in result["guests"]

    def test_single_guest(self):
        result = _fmt_reservation(self._row(guest_count=1))
        assert result["guests"] == "1 guest"

    def test_name_present(self):
        result = _fmt_reservation(self._row())
        assert result["name"] == "Smith Family"
