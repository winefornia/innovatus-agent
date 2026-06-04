"""
Integration tests for the tasting room flow with mocked external dependencies.

Tests process_action_decision() — the human-in-the-loop decision handler for
tasting room reservation actions (approve/reject/escalate/idempotent duplicate).
"""

import pytest

from services.tastingroom_service import process_action_decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pending_action(action_id: str = "act_001", reservation_id: str = "res_001",
                    action_type: str = "ask_josh_availability"):
    return {
        "action_id": action_id,
        "reservation_id": reservation_id,
        "action_type": action_type,
        "status": "pending",
        "risk_level": "medium",
        "recipient_email": "josh@example.com",
        "email_subject": "Availability Check",
        "email_body": "Hi Josh, are you available?",
        "recommendation": "Ask Josh for availability",
        "telegram_chat_id": None,
        "telegram_message_id": None,
        "decided_by": None,
        "decided_at": None,
    }


def _fake_reservation(reservation_id: str = "res_001"):
    return {
        "reservation_id": reservation_id,
        "client_name": "Smith Family",
        "client_email": "smith@example.com",
        "requested_date": "2026-06-15",
        "requested_time": "14:00",
        "guest_count": 4,
        "experience_type": "cave_experience",
        "current_state": "NEEDS_FACILITY_CHECK",
        "payment_status": "not_sent",
        "booking_status": "not_booked",
        "gmail_thread_ids": [],
        "active_slot": {},
        "candidate_slots": [],
        "recommended_action": "ask_josh_availability",
        "confidence": 1.0,
        "notes": None,
    }


# ---------------------------------------------------------------------------
# Scenario 7 — rejection: action marked rejected, no email sent
# ---------------------------------------------------------------------------

class TestActionRejection:
    def test_reject_returns_ok(self, mocker):
        mocker.patch("db.repository.get_reservation_action", return_value=_pending_action())
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        mocker.patch("db.repository.get_reservation", return_value=_fake_reservation())

        result = process_action_decision("act_001", "reject", decided_by="tg_123")

        assert result["ok"] is True
        assert result["status"] == "rejected"

    def test_reject_does_not_send_email(self, mocker):
        mocker.patch("db.repository.get_reservation_action", return_value=_pending_action())
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        mocker.patch("db.repository.get_reservation", return_value=_fake_reservation())
        mock_send = mocker.patch("services.gmail_service.send_email")

        process_action_decision("act_001", "reject", decided_by="tg_123")

        mock_send.assert_not_called()

    def test_reject_returns_reservation_id(self, mocker):
        mocker.patch("db.repository.get_reservation_action", return_value=_pending_action())
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        mocker.patch("db.repository.get_reservation", return_value=_fake_reservation())

        result = process_action_decision("act_001", "reject", decided_by="tg_123")

        assert result.get("reservation_id") == "res_001"


# ---------------------------------------------------------------------------
# Scenario 8 — duplicate approve: idempotent (second call returns error)
# ---------------------------------------------------------------------------

class TestDuplicateApprove:
    def test_second_approve_returns_error(self, mocker):
        """Once an action is no longer 'pending', process_action_decision returns ok=False."""
        # Simulate action already processed (status="sent")
        already_sent_action = {**_pending_action(), "status": "sent"}
        mocker.patch("db.repository.get_reservation_action", return_value=already_sent_action)

        result = process_action_decision("act_001", "approve", decided_by="tg_123")

        assert result["ok"] is False
        assert "already" in result.get("error", "").lower()

    def test_already_rejected_returns_error(self, mocker):
        already_rejected = {**_pending_action(), "status": "rejected"}
        mocker.patch("db.repository.get_reservation_action", return_value=already_rejected)

        result = process_action_decision("act_001", "approve", decided_by="tg_123")

        assert result["ok"] is False

    def test_unknown_action_id_returns_error(self, mocker):
        mocker.patch("db.repository.get_reservation_action", return_value=None)

        result = process_action_decision("nonexistent_id", "approve", decided_by="tg_123")

        assert result["ok"] is False
        assert "not found" in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# Scenario 9 — escalate: reservation marked HUMAN_REVIEW_REQUIRED
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_escalate_returns_ok(self, mocker):
        mocker.patch("db.repository.get_reservation_action", return_value=_pending_action())
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        mock_update_res = mocker.patch("db.repository.update_reservation", return_value=None)
        mocker.patch("db.repository.get_reservation", return_value=_fake_reservation())

        result = process_action_decision("act_001", "escalate", decided_by="tg_123")

        assert result["ok"] is True
        assert result["status"] == "escalated"

    def test_escalate_sets_human_review_required(self, mocker):
        mocker.patch("db.repository.get_reservation_action", return_value=_pending_action())
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        mock_update_res = mocker.patch("db.repository.update_reservation", return_value=None)
        mocker.patch("db.repository.get_reservation", return_value=_fake_reservation())

        process_action_decision("act_001", "escalate", decided_by="tg_123")

        mock_update_res.assert_called_once_with(
            "res_001",
            current_state="HUMAN_REVIEW_REQUIRED",
            recommended_action="escalate",
        )

    def test_escalate_does_not_send_email(self, mocker):
        mocker.patch("db.repository.get_reservation_action", return_value=_pending_action())
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        mocker.patch("db.repository.update_reservation", return_value=None)
        mocker.patch("db.repository.get_reservation", return_value=_fake_reservation())
        mock_send = mocker.patch("services.gmail_service.send_email")

        process_action_decision("act_001", "escalate", decided_by="tg_123")

        mock_send.assert_not_called()
