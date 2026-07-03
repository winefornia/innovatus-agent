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
# Scenario 7b — approve records the outbound thread on the reservation so the
# recipient's reply (e.g. Josh) attaches to THIS case, not a new nameless one.
# ---------------------------------------------------------------------------

class TestOutboundThreadRecorded:
    def test_approve_records_sent_thread_on_reservation(self, mocker, monkeypatch):
        import services.tastingroom_service as trs
        monkeypatch.setattr(trs, "TASTINGROOM_SAFE_MODE", False, raising=False)

        mocker.patch("db.repository.get_reservation_action", return_value=_pending_action())
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        mocker.patch("db.repository.get_reservation", return_value=_fake_reservation())
        mocker.patch("db.repository.insert_reservation_event", return_value=None)
        mocker.patch("services.gmail_service.apply_message_labels", return_value=None)
        mocker.patch("services.tastingroom_mailbox.labels_for_result", return_value=[])
        mocker.patch.object(trs, "_apply_post_send_state", return_value=None)
        mocker.patch("services.gmail_service.send_email",
                     return_value={"message_id": "m1", "thread_id": "THREAD_JOSH", "to": "josh@example.com"})
        upd = mocker.patch("db.repository.update_reservation", return_value=None)

        result = process_action_decision("act_001", "approve", decided_by="tg_123")

        assert result["ok"] is True
        # the sent thread was appended to gmail_thread_ids
        calls = [c for c in upd.call_args_list if "gmail_thread_ids" in c.kwargs]
        assert calls, "update_reservation should be called with gmail_thread_ids"
        assert "THREAD_JOSH" in calls[-1].kwargs["gmail_thread_ids"]


# ---------------------------------------------------------------------------
# Scenario 7c — Square invoice is created/published before payment email sends.
# ---------------------------------------------------------------------------

class TestSquareInvoiceCreation:
    def test_tentative_invoice_creates_square_invoice_and_inserts_link(self, mocker, monkeypatch):
        import services.tastingroom_service as trs
        monkeypatch.setattr(trs, "TASTINGROOM_SAFE_MODE", False, raising=False)

        action = _pending_action(action_type="send_tentative_invoice")
        action["recipient_email"] = "smith@example.com"
        action["email_body"] = "Please pay here:\n{{SQUARE_INVOICE_URL}}"
        reservation = {
            **_fake_reservation(),
            "client_email": "smith@example.com",
            "price_per_person_cents": 11000,
            "current_state": "CLIENT_ACCEPTED_SLOT",
        }
        mocker.patch("db.repository.get_reservation_action", return_value=action)
        mocker.patch("db.repository.get_reservation", return_value=reservation)
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        upd = mocker.patch("db.repository.update_reservation", return_value=None)
        mocker.patch("db.repository.insert_reservation_event", return_value=None)
        mocker.patch("services.gmail_service.apply_message_labels", return_value=None)
        mocker.patch("services.tastingroom_mailbox.labels_for_result", return_value=[])
        mocker.patch("services.square_service.get_or_create_square_customer",
                     return_value={"status": "created", "customer_id": "cust_1"})
        mocker.patch("services.square_service.create_order",
                     return_value={"status": "created", "order_id": "ord_1",
                                   "total_money": {"amount": 44000, "currency": "USD"}})
        mocker.patch("services.square_service.create_invoice_draft",
                     return_value={"status": "draft_created", "invoice_id": "inv_1",
                                   "invoice_version": 0, "invoice_number": "1001"})
        mocker.patch("services.square_service.publish_invoice",
                     return_value={"status": "published", "invoice_id": "inv_1",
                                   "public_url": "https://square.test/inv_1"})
        verify = mocker.patch("services.square_service.verify_invoice",
                              return_value={"ok": True, "invoice_id": "inv_1",
                                            "invoice_number": "1001",
                                            "order_id": "ord_1",
                                            "customer_id": "cust_1",
                                            "status": "UNPAID",
                                            "public_url": "https://square.test/inv_1",
                                            "total_money_cents": 44000})
        send = mocker.patch("services.gmail_service.send_email",
                            return_value={"message_id": "m1", "thread_id": "THREAD_CLIENT"})
        mocker.patch.object(trs, "create_action_request", return_value="pay_act")

        result = process_action_decision("act_001", "approve", decided_by="gchat_lisa")

        assert result["ok"] is True
        verify.assert_called_once()
        assert "https://square.test/inv_1" in send.call_args.kwargs["plain"]
        square_updates = [c for c in upd.call_args_list if c.kwargs.get("square_invoice_id") == "inv_1"]
        assert square_updates
        assert square_updates[-1].kwargs["square_invoice_url"] == "https://square.test/inv_1"
        assert square_updates[-1].kwargs["square_invoice_status"] == "UNPAID"

    def test_square_failure_blocks_payment_email(self, mocker, monkeypatch):
        import services.tastingroom_service as trs
        monkeypatch.setattr(trs, "TASTINGROOM_SAFE_MODE", False, raising=False)

        action = _pending_action(action_type="send_tentative_invoice")
        action["recipient_email"] = "smith@example.com"
        action["email_body"] = "Please pay here:\n{{SQUARE_INVOICE_URL}}"
        reservation = {**_fake_reservation(), "client_email": "smith@example.com", "price_per_person_cents": 11000}
        mocker.patch("db.repository.get_reservation_action", return_value=action)
        mocker.patch("db.repository.get_reservation", return_value=reservation)
        update_action = mocker.patch("db.repository.update_reservation_action", return_value=None)
        mocker.patch("services.square_service.get_or_create_square_customer",
                     return_value={"error": "Square down"})
        send = mocker.patch("services.gmail_service.send_email")

        result = process_action_decision("act_001", "approve", decided_by="gchat_lisa")

        assert result["ok"] is False
        assert "Square customer failed" in result["error"]
        send.assert_not_called()
        assert any(c.kwargs.get("status") == "failed" for c in update_action.call_args_list)

    def test_square_verification_failure_blocks_payment_email(self, mocker, monkeypatch):
        import services.tastingroom_service as trs
        monkeypatch.setattr(trs, "TASTINGROOM_SAFE_MODE", False, raising=False)

        action = _pending_action(action_type="send_tentative_invoice")
        action["recipient_email"] = "smith@example.com"
        action["email_body"] = "Please pay here:\n{{SQUARE_INVOICE_URL}}"
        reservation = {**_fake_reservation(), "client_email": "smith@example.com", "price_per_person_cents": 11000}
        mocker.patch("db.repository.get_reservation_action", return_value=action)
        mocker.patch("db.repository.get_reservation", return_value=reservation)
        update_action = mocker.patch("db.repository.update_reservation_action", return_value=None)
        mocker.patch("services.square_service.get_or_create_square_customer",
                     return_value={"status": "created", "customer_id": "cust_1"})
        mocker.patch("services.square_service.create_order",
                     return_value={"status": "created", "order_id": "ord_1",
                                   "total_money": {"amount": 44000, "currency": "USD"}})
        mocker.patch("services.square_service.create_invoice_draft",
                     return_value={"status": "draft_created", "invoice_id": "inv_1",
                                   "invoice_version": 0, "invoice_number": "1001"})
        mocker.patch("services.square_service.publish_invoice",
                     return_value={"status": "published", "invoice_id": "inv_1",
                                   "public_url": "https://square.test/inv_1"})
        mocker.patch("services.square_service.verify_invoice",
                     return_value={"ok": False, "error": "status is DRAFT"})
        send = mocker.patch("services.gmail_service.send_email")

        result = process_action_decision("act_001", "approve", decided_by="gchat_lisa")

        assert result["ok"] is False
        assert "Square invoice verification failed" in result["error"]
        send.assert_not_called()
        assert any(c.kwargs.get("status") == "failed" for c in update_action.call_args_list)


# ---------------------------------------------------------------------------
# Scenario 7d — final confirmation requires persisted calendar invite.
# ---------------------------------------------------------------------------

class TestCalendarInvitePersistence:
    def test_final_confirmation_creates_calendar_and_persists_link(self, mocker, monkeypatch):
        import services.tastingroom_service as trs
        monkeypatch.setattr(trs, "TASTINGROOM_SAFE_MODE", False, raising=False)

        action = _pending_action(action_type="send_final_confirmation")
        action["recipient_email"] = "smith@example.com"
        action["email_body"] = "Your tasting is confirmed."
        reservation = {
            **_fake_reservation(),
            "current_state": "PAYMENT_RECEIVED",
            "payment_status": "paid",
            "price_per_person_cents": 11000,
        }
        mocker.patch("db.repository.get_reservation_action", return_value=action)
        mocker.patch("db.repository.get_reservation", return_value=reservation)
        mocker.patch("db.repository.update_reservation_action", return_value=None)
        upd = mocker.patch("db.repository.update_reservation", return_value=None)
        mocker.patch("db.repository.insert_reservation_event", return_value=None)
        mocker.patch("services.gmail_service.apply_message_labels", return_value=None)
        mocker.patch("services.tastingroom_mailbox.labels_for_result", return_value=[])
        mocker.patch("services.calendar_service.create_tasting_event",
                     return_value={"event_id": "evt_1", "html_link": "https://calendar.test/evt_1",
                                   "attendees": ["lisa@example.com", "smith@example.com", "josh@example.com"]})
        send = mocker.patch("services.gmail_service.send_email",
                            return_value={"message_id": "m1", "thread_id": "THREAD_CLIENT"})

        result = process_action_decision("act_001", "approve", decided_by="gchat_lisa")

        assert result["ok"] is True
        assert "https://calendar.test/evt_1" in send.call_args.kwargs["plain"]
        assert any(c.kwargs.get("calendar_event_url") == "https://calendar.test/evt_1"
                   for c in upd.call_args_list)
        assert any(c.kwargs.get("current_state") == "FINAL_CONFIRMED"
                   for c in upd.call_args_list)

    def test_calendar_failure_blocks_final_confirmation_email(self, mocker, monkeypatch):
        import services.tastingroom_service as trs
        monkeypatch.setattr(trs, "TASTINGROOM_SAFE_MODE", False, raising=False)

        action = _pending_action(action_type="send_final_confirmation")
        action["recipient_email"] = "smith@example.com"
        reservation = {**_fake_reservation(), "current_state": "PAYMENT_RECEIVED", "payment_status": "paid"}
        mocker.patch("db.repository.get_reservation_action", return_value=action)
        mocker.patch("db.repository.get_reservation", return_value=reservation)
        update_action = mocker.patch("db.repository.update_reservation_action", return_value=None)
        upd = mocker.patch("db.repository.update_reservation", return_value=None)
        mocker.patch("services.calendar_service.create_tasting_event", return_value=None)
        send = mocker.patch("services.gmail_service.send_email")

        result = process_action_decision("act_001", "approve", decided_by="gchat_lisa")

        assert result["ok"] is False
        assert "Calendar invite failed" in result["error"]
        send.assert_not_called()
        assert not any(c.kwargs.get("current_state") == "FINAL_CONFIRMED"
                       for c in upd.call_args_list)
        assert any(c.kwargs.get("status") == "failed" for c in update_action.call_args_list)


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
