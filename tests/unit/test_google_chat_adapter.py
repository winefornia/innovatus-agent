"""Tests for the Google Chat adapter's stability + routing logic.

Covers the failure modes hit this session:
  - interrupt detection from the real payload (invoke result AND snapshot,
    including the PostgresSaver case where only tasks[*].interrupts is populated)
  - webhook-retry dedup
  - the async ack-then-post deadline race (fast = sync, slow = ack + post)
"""
import asyncio

from services.invoice_interrupts import current_invoice_interrupt as which, interrupt_payload


# ── fakes for interrupt payloads / snapshots ────────────────────────────────
class _Interrupt:
    def __init__(self, value):
        self.value = value


class _Task:
    def __init__(self, interrupts):
        self.interrupts = interrupts


class _Snapshot:
    def __init__(self, interrupts=(), tasks=()):
        self.interrupts = interrupts
        self.tasks = tasks
        self.next = ("some_node",)


def test_interrupt_from_invoke_result_payload():
    result = {"__interrupt__": [_Interrupt({"type": "price_confirmation"})]}
    assert which(result) == "price_confirmation"


def test_interrupt_from_snapshot_interrupts():
    snap = _Snapshot(interrupts=(_Interrupt({"type": "tier_and_payment_confirmation"}),))
    assert which(snap) == "tier"


def test_interrupt_from_snapshot_tasks_only_postgres_case():
    # PostgresSaver populates tasks[*].interrupts but not .interrupts — the bug
    # that made the price reply fall through to a fresh chat run.
    snap = _Snapshot(interrupts=(), tasks=(_Task((_Interrupt({"type": "price_confirmation"}),)),))
    assert which(snap) == "price_confirmation"


def test_low_confidence_missing_fields_detected_via_payload():
    # ask_missing_fields can fire with missing_fields=[] (low confidence) — must
    # still be detected (this caused the "Done." dead-end).
    result = {"__interrupt__": [_Interrupt({"type": "missing_fields", "missing": []})]}
    assert which(result) == "missing"


def test_no_interrupt_returns_none():
    assert which({}) is None
    assert which(_Snapshot(interrupts=(), tasks=())) is None


def test_interrupt_payload_question_surfaced():
    result = {"__interrupt__": [_Interrupt({"type": "missing_fields", "question": "What email?"})]}
    assert interrupt_payload(result)["question"] == "What email?"


# ── dedup ────────────────────────────────────────────────────────────────────
def test_message_dedup():
    import app.adapters.google_chat_adapter as a
    a._seen_messages.clear()
    assert a._already_seen("spaces/S/messages/unique1") is False  # first sight
    assert a._already_seen("spaces/S/messages/unique1") is True   # retry dropped
    assert a._already_seen("") is False                            # empty never dedups


# ── async ack-then-post deadline race ────────────────────────────────────────
def _classic_event(etype="MESSAGE"):
    return {"type": etype, "space": {"name": "spaces/TEST"},
            "message": {"name": "spaces/TEST/messages/m", "text": "hi"}}


def test_fast_run_returns_sync_no_async_post(monkeypatch):
    import app.adapters.google_chat_adapter as a
    posts = []

    async def fake_post(space, body):
        posts.append((space, body)); return True

    async def fast_route(ev):
        return {"text": "FAST"}

    monkeypatch.setattr(a, "_post_message_to_space", fake_post)
    monkeypatch.setattr(a, "_route_event", fast_route)
    monkeypatch.setattr(a, "_ACK_DEADLINE", 0.5)

    resp = asyncio.run(a.handle_google_chat_event(_classic_event()))
    assert resp == {"text": "FAST"}
    assert posts == []  # fast path never posts async


def test_slow_run_acks_then_posts_result(monkeypatch):
    import app.adapters.google_chat_adapter as a
    posts = []

    async def fake_post(space, body):
        posts.append((space, body)); return True

    async def slow_route(ev):
        await asyncio.sleep(0.4)
        return {"text": "SLOW RESULT"}

    monkeypatch.setattr(a, "_post_message_to_space", fake_post)
    monkeypatch.setattr(a, "_route_event", slow_route)
    monkeypatch.setattr(a, "_ACK_DEADLINE", 0.1)

    async def scenario():
        resp = await a.handle_google_chat_event(_classic_event())
        assert "Working on it" in resp["text"]   # acked synchronously
        await asyncio.sleep(0.6)                  # let work + post finish
        return resp

    asyncio.run(scenario())
    assert posts == [("spaces/TEST", {"text": "SLOW RESULT"})]  # delivered async, once
