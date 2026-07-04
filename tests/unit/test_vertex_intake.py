"""Hardening tests for the tasting-room intake/coordination (vertex_agent.intake).

Locks in: coordinate_email NEVER raises; a failed intake is recorded (no
poison-loop); coordination is DETERMINISTIC (gap → action, no LLM); and it never
acts on a terminal case.
"""
import vertex_agent.intake as intake


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


def test_coordinate_intake_failure_returns_intake_error_and_records(monkeypatch):
    recorded = {}
    monkeypatch.setattr(intake, "intake_email", _raise(ValueError("db down")))
    monkeypatch.setattr(intake, "_record_intake_failure",
                        lambda *a, **k: recorded.update(called=True))

    res = intake.coordinate_email(subject="s", sender="x@y.com", body="b", gmail_message_id="m1")

    assert res["status"] == "intake_error"          # did not raise
    assert res["reservation_id"] is None
    assert recorded.get("called") is True           # recorded → no infinite retry


def test_coordinate_hands_off_to_deterministic_coordinator(monkeypatch):
    # intake resolves a case, then coordinate_email delegates to the deterministic
    # coordinator (no LLM) and merges the case metadata.
    monkeypatch.setattr(intake, "intake_email", lambda **k: {
        "unresolved": False, "reservation_id": "r1",
        "message_type": "client_reply", "experience_type": "Tasting"})
    monkeypatch.setattr(intake, "coordinate_reservation", lambda rid: {
        "status": "coordinated", "reservation_id": rid,
        "proposed_action": {"action": "ask_josh_availability"}})

    res = intake.coordinate_email(subject="s", sender="x@y.com", body="b", gmail_message_id="m1")

    assert res["status"] == "coordinated"
    assert res["reservation_id"] == "r1"
    assert res["message_type"] == "client_reply"
    assert res["proposed_action"]["action"] == "ask_josh_availability"


def test_coordinate_reservation_skips_terminal(monkeypatch):
    import db.repository as repo
    monkeypatch.setattr(repo, "get_reservation",
                        lambda rid: {"reservation_id": rid, "current_state": "FINAL_CONFIRMED"})
    res = intake.coordinate_reservation("r1")
    assert res["status"] == "terminal"           # never re-acts on a finished/closed case
    assert res["proposed_action"] is None


def test_coordinate_reservation_never_raises(monkeypatch):
    import db.repository as repo
    monkeypatch.setattr(repo, "get_reservation", _raise(RuntimeError("db down")))
    res = intake.coordinate_reservation("r1")
    assert res["status"] == "error"              # degrades, doesn't raise


def test_coordinate_unresolved_passes_through(monkeypatch):
    monkeypatch.setattr(intake, "intake_email", lambda **k: {
        "unresolved": True, "reservation_id": None, "message_type": "unclassified"})

    res = intake.coordinate_email(subject="s", sender="x@y.com", body="b", gmail_message_id="m1")

    assert res["status"] == "unresolved"
    assert res["reservation_id"] is None


# ── new-case notification (Squarespace form → Chat space) ────────────────────

def _wire_intake(mocker, *, existing_case, posted):
    """Stub intake_email's collaborators so a squarespace form flows through."""
    from db.models import Reservation

    res = Reservation(
        reservation_id="TASTING-20260710-4G-MIRA-PARK",
        client_name="Mira Park", client_email="mirasopa@gmail.com",
        requested_date="2026-07-10", requested_time="14:00",
        guest_count=4, experience_type="Tasting",
    )
    mocker.patch("db.repository.insert_raw_email_event", return_value=None)
    trs = "services.tastingroom_service."
    mocker.patch(trs + "classify_email", return_value="squarespace_form")
    mocker.patch(trs + "extract_email_facts",
                 return_value={"client_name": "Mira Park", "client_email": "mirasopa@gmail.com"})
    mocker.patch(trs + "llm_extract_email", return_value={})
    mocker.patch(trs + "merge_llm_facts", side_effect=lambda f, llm, mt: f)
    mocker.patch(trs + "find_or_create_reservation",
                 return_value=(res.reservation_id, existing_case))
    mocker.patch(trs + "merge_reservation", return_value=res)
    mocker.patch(trs + "build_claims", return_value=[])
    mocker.patch(trs + "persist_processed_email", return_value=None)
    mocker.patch("app.adapters.google_chat_tastingroom.post_text",
                 side_effect=lambda text: posted.append(text) or "spaces/x/messages/1")
    return res


def test_new_form_case_posts_chat_notification(mocker):
    posted = []
    _wire_intake(mocker, existing_case=None, posted=posted)
    out = intake.intake_email(subject="Form Submission - Wine tasting Booking",
                              sender="form-submission@squarespace.info", body="...",
                              gmail_message_id="m1", gmail_thread_id="t1")
    assert out["unresolved"] is False
    assert len(posted) == 1
    note = posted[0]
    assert "New tasting request" in note and "Mira Park" in note
    assert "2026-07-10" in note and "TASTING-20260710-4G-MIRA-PARK" in note


def test_follow_up_on_existing_case_does_not_renotify(mocker):
    posted = []
    res = _wire_intake(mocker, existing_case={"reservation_id": "TASTING-20260710-4G-MIRA-PARK"},
                       posted=posted)
    intake.intake_email(subject="Form Submission - Wine tasting Booking",
                        sender="form-submission@squarespace.info", body="...",
                        gmail_message_id="m2", gmail_thread_id="t1")
    assert posted == []                      # only case BIRTH notifies


def test_notification_failure_never_blocks_intake(mocker):
    posted = []
    _wire_intake(mocker, existing_case=None, posted=posted)
    mocker.patch("app.adapters.google_chat_tastingroom.post_text",
                 side_effect=RuntimeError("chat down"))
    out = intake.intake_email(subject="Form Submission - Wine tasting Booking",
                              sender="form-submission@squarespace.info", body="...",
                              gmail_message_id="m3", gmail_thread_id="t3")
    assert out["unresolved"] is False        # intake unaffected
