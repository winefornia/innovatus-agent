"""Hardening tests for the tasting-room agent path (vertex_agent.intake).

Locks in the stability guarantees: coordinate_email NEVER raises, a failed intake
is recorded (so the watcher can't poison-loop), and an agent failure preserves the
already-persisted case instead of dropping it.
"""
import asyncio

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


def test_coordinate_agent_failure_preserves_case(monkeypatch):
    # intake succeeds and persists the case…
    monkeypatch.setattr(intake, "intake_email", lambda **k: {
        "unresolved": False, "reservation_id": "r1", "message_type": "client_reply"})
    # …but the agent run blows up — must degrade, not raise.
    monkeypatch.setattr(asyncio, "run", _raise(RuntimeError("agent boom")))

    res = intake.coordinate_email(subject="s", sender="x@y.com", body="b", gmail_message_id="m1")

    assert res["status"] == "agent_error"           # did not raise
    assert res["reservation_id"] == "r1"            # case preserved, not dropped


def test_coordinate_unresolved_passes_through(monkeypatch):
    monkeypatch.setattr(intake, "intake_email", lambda **k: {
        "unresolved": True, "reservation_id": None, "message_type": "unclassified"})

    res = intake.coordinate_email(subject="s", sender="x@y.com", body="b", gmail_message_id="m1")

    assert res["status"] == "unresolved"
    assert res["reservation_id"] is None
