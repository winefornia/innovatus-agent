"""Staff manual mail (e.g. Audrey replying to a client directly in Gmail) must be
tracked structurally: classified as staff_manual_reply, attached to the case,
its observed facts (invoice sent) applied forward-only, and NEVER treated as a
client message or used to auto-queue next actions. Locks in the July 2026
Paige Kim failure mode (manual replies misread as client_deferred /
invoice_payment_message, then a stale offer_client_slot card was queued after
staff had already released the hold).
"""
import services.tastingroom_service as trs
import vertex_agent.intake as intake


WINERY = "INNOVATUS <contact@innovatuswine.com>"


# ---------- classification ----------

def test_winery_self_mail_is_staff_manual_reply_not_client_type():
    # "thank you" used to classify as client_deferred
    assert trs.classify_email("Re: booking", WINERY, "Thank you! See you soon.") == "staff_manual_reply"


def test_winery_self_invoice_mail_is_not_payment_notification():
    body = "I have attached an invoice. It can be paid through this link: INVOICE LINK"
    assert trs.classify_email("Re: booking", WINERY, body) == "staff_manual_reply"


def test_delegated_user_env_counts_as_winery_self(monkeypatch):
    monkeypatch.setenv("GOOGLE_DELEGATED_USER_EMAIL", "audrey@gmail.com")
    assert trs.classify_email("Re: visit", "Audrey <audrey@gmail.com>", "Thank you!") == "staff_manual_reply"


def test_structured_staff_types_still_win_over_catchall():
    # System-sent facility asks from the winery's own address keep their type.
    body = "Hi Josh,\n\nChecking availability for a party of 4."
    assert trs.classify_email("Availability check", WINERY, body) == "facility_availability_request"


def test_external_senders_unaffected():
    assert trs.classify_email(
        "Form Submission - Wine tasting Booking",
        "Squarespace <form-submission@squarespace.info>",
        "sent via form submission\ndate requested: July 16, 2026",
    ) == "squarespace_form"
    assert trs.classify_email(
        "Invoice paid", "Square <messenger@messaging.squareup.com>",
        "Paige Kim has paid invoice #202447",
    ) == "invoice_payment_message"


# ---------- fact extraction ----------

def test_staff_invoice_link_marks_payment_sent():
    body = ("Hi Paige, prepayment is required. The invoice can be accessed here: "
            "https://app.squareup.com/pay-invoice/invtmp:abc123")
    facts = trs.extract_email_facts("Re: booking", WINERY, body, "staff_manual_reply")
    assert facts["payment_status"] == "sent"
    # staff mail must never be mistaken for the client's identity
    assert not facts.get("client_email")


def test_staff_release_mail_records_hold_released():
    body = "Hi Paige, the July 16th reservation being tentatively held has been released."
    facts = trs.extract_email_facts("Re: booking", WINERY, body, "staff_manual_reply")
    assert facts.get("hold_released") is True


def test_plain_staff_reply_extracts_no_payment_fact():
    facts = trs.extract_email_facts("Re: booking", WINERY, "We love dogs, but cannot accommodate them.",
                                    "staff_manual_reply")
    assert "payment_status" not in facts
    assert not facts.get("hold_released")


# ---------- merge: observed payment status, forward-only ----------

def _existing(payment="not_sent"):
    return {"reservation_id": "TASTING-X", "payment_status": payment,
            "current_state": "READY_TO_OFFER_CLIENT", "gmail_thread_ids": ["t1"]}


def test_merge_applies_staff_observed_invoice_sent():
    facts = {"message_type": "staff_manual_reply", "payment_status": "sent", "sender_email": "contact@innovatuswine.com"}
    r = trs.merge_reservation(_existing("not_sent"), "TASTING-X", facts, "t1")
    assert r.payment_status == "sent"


def test_merge_applies_square_notification_payment_paid():
    # extract_email_facts has produced payment facts for Square notifications
    # since June 2026 but nothing consumed them — this locks in the fix.
    facts = {"message_type": "invoice_payment_message", "payment_status": "paid", "sender_email": "messenger@messaging.squareup.com"}
    r = trs.merge_reservation(_existing("sent"), "TASTING-X", facts, "t1")
    assert r.payment_status == "paid"


def test_merge_payment_status_never_regresses():
    facts = {"message_type": "staff_manual_reply", "payment_status": "sent", "sender_email": "contact@innovatuswine.com"}
    r = trs.merge_reservation(_existing("paid"), "TASTING-X", facts, "t1")
    assert r.payment_status == "paid"


def test_merge_ignores_payment_from_untrusted_types():
    facts = {"message_type": "client_acceptance", "payment_status": "paid", "sender_email": "someone@gmail.com"}
    r = trs.merge_reservation(_existing("not_sent"), "TASTING-X", facts, "t1")
    assert r.payment_status == "not_sent"


# ---------- coordination: staff mail never queues actions ----------

def test_coordinate_email_skips_coordination_for_staff_manual_reply(monkeypatch):
    monkeypatch.setattr(intake, "intake_email", lambda **k: {
        "unresolved": False, "reservation_id": "TASTING-X",
        "message_type": "staff_manual_reply", "experience_type": "Tasting"})

    def _boom(rid):
        raise AssertionError("coordinate_reservation must not run for staff manual mail")
    monkeypatch.setattr(intake, "coordinate_reservation", _boom)

    res = intake.coordinate_email(subject="Re: booking", sender=WINERY, body="released",
                                  gmail_message_id="m1")
    assert res["status"] == "staff_manual"
    assert res["reservation_id"] == "TASTING-X"
    assert res["proposed_action"] is None


# ---------- failed approved send is visible in the audit trail ----------

def test_failed_send_writes_reservation_event_and_execution_result(monkeypatch):
    events, exec_results = [], []
    action = {"action_id": "a1", "action_type": "offer_client_slot", "status": "pending",
              "reservation_id": "TASTING-X", "recipient_email": "client@example.com",
              "email_subject": "s", "email_body": "b", "risk_level": "medium"}

    import db.repository as repo
    monkeypatch.setattr(trs, "TASTINGROOM_SAFE_MODE", False)
    monkeypatch.setattr(repo, "get_reservation_action", lambda aid: dict(action))
    monkeypatch.setattr(repo, "get_reservation", lambda rid: _existing())
    monkeypatch.setattr(repo, "update_reservation_action", lambda aid, **kw: None)
    monkeypatch.setattr(repo, "insert_reservation_event", lambda ev: events.append(ev))
    monkeypatch.setattr(repo, "insert_execution_result", lambda rec: exec_results.append(rec))

    def _send_fail(**kwargs):
        raise RuntimeError("gmail down")
    monkeypatch.setattr("services.gmail_service.send_email", _send_fail)

    result = trs.process_action_decision("a1", "approve", decided_by="cecil")

    assert result["ok"] is False
    failure_events = [e for e in events if e.event_type == "approved_email_send_failed"]
    assert failure_events, "send failure must appear in reservation_events"
    assert "gmail down" in failure_events[0].summary
    failed = [r for r in exec_results if r.ok is False]
    assert failed and failed[0].tool_name == "offer_client_slot"
    assert failed[0].case_id == "TASTING-X"
