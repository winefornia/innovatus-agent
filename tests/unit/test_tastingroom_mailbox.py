from services import tastingroom_mailbox


def test_process_gmail_message_uses_reservation_state_for_labels(monkeypatch):
    """The mailbox routes inbound mail to the Vertex agent (coordinate_email) and
    labels from the resolved reservation's state."""
    applied = {}

    monkeypatch.setattr(tastingroom_mailbox, "message_already_processed", lambda *args, **kwargs: False)

    def fake_read_email(message_id):
        return {
            "body": "Client accepted the offered slot.",
            "subject": "Availability Check",
            "from": "josh@thecavesatsodacanyon.com",
            "to": "cecil.park@winefornia.com",
            "thread_id": "19e123abc456",
        }

    def fake_apply_message_labels(message_id, *, remove_labels, add_labels):
        applied["message_id"] = message_id
        applied["remove_labels"] = remove_labels
        applied["add_labels"] = add_labels

    import services.gmail_service as gmail_service
    import vertex_agent.intake as intake
    import db.repository as repository

    monkeypatch.setattr(gmail_service, "read_email", fake_read_email)
    monkeypatch.setattr(gmail_service, "apply_message_labels", fake_apply_message_labels)
    # The agent path: coordinate_email resolves the case; current_state comes from the reservation.
    monkeypatch.setattr(intake, "coordinate_email", lambda **kwargs: {
        "status": "coordinated",
        "message_type": "josh_availability_reply",
        "reservation_id": "res_1",
        "proposed_action": {"action": "offer_client_slot"},
        "agent_summary": "Ready to offer client.",
    })
    monkeypatch.setattr(repository, "get_reservation", lambda rid: {"current_state": "READY_TO_OFFER_CLIENT"})

    result = tastingroom_mailbox.process_gmail_message("msg_1", labels=[])

    assert result["state"] == "READY_TO_OFFER_CLIENT"
    assert result["engine"] == "agent"
    assert "Tasting Room/Action Needed" in applied["add_labels"]


# ── intake-root separation: invoice pipeline vs tasting room ─────────────────

class TestSquareMailIsNotTastingIntake:
    """Tasting cases are born and fed ONLY through the Squarespace-form pipeline
    (the Mira Park path) + their own case threads. Square invoice notifications
    belong to the invoice pipeline and must be rejected at the candidate filter,
    whatever their subject says."""

    def _square(self, subject):
        return {"message_id": "m1", "subject": subject,
                "from": "Square <invoicing@messaging.squareup.com>", "to": "lisa@winefornia.com"}

    def test_square_invoice_created_is_rejected(self):
        from services.tastingroom_mailbox import _looks_like_tastingroom_message
        assert not _looks_like_tastingroom_message(
            self._square("A new invoice was created for Christina Yoo (#202468)"))

    def test_square_invoice_paid_is_rejected(self):
        from services.tastingroom_mailbox import _looks_like_tastingroom_message
        assert not _looks_like_tastingroom_message(
            self._square("An invoice was paid by Christina Yoo! (#202468)"))

    def test_square_mail_rejected_even_with_tasting_words(self):
        # A Square notification about the tasting DEPOSIT still isn't intake —
        # payment state is card-confirmed by staff / verified via Square API.
        from services.tastingroom_mailbox import _looks_like_tastingroom_message
        assert not _looks_like_tastingroom_message(
            self._square("Invoice paid — Innovatus tasting reservation deposit"))

    def test_squarespace_form_is_still_intake(self):
        from services.tastingroom_mailbox import _looks_like_tastingroom_message
        assert _looks_like_tastingroom_message({
            "message_id": "m2",
            "subject": "Form Submission - Wine tasting Booking",
            "from": "form-submission@squarespace.info", "to": "lisa@winefornia.com"})

    def test_default_gmail_query_has_no_square_terms(self):
        from app.config import GMAIL_TASTING_QUERY
        q = GMAIL_TASTING_QUERY.lower()
        assert "squareup" not in q
        assert "invoice" not in q
