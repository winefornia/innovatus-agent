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
