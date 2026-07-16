"""Tests for the tasting-room Google Chat approval adapter.

Covers the migration-critical behaviors:
  - approval cards reuse the Telegram-style "tr:{action_id}:{decision}" callbacks
    and rewrite them to the tasting-room endpoint (separate Chat app)
  - a Workspace Add-on button click parses back to (action_id, decision) and
    resumes the channel-agnostic process_action_decision()
  - the channel is config-gated (dormant until GOOGLE_CHAT_TR_SPACE is set)
  - malformed / non-tr actions fail safe

Plus the production hardening ported from google_chat_adapter.py:
  - webhook-retry dedup (#2)
  - ack-then-post deadline race: fast = sync, slow = ack + post (#1)
  - outbound post retry on transient failure (#3)
  - duplicate/retried click on an already-decided action stays silent
"""
import asyncio

import app.adapters.google_chat_tastingroom as tr


def _rows(action_id="abc123"):
    return [
        [("Send it", f"tr:{action_id}:approve"), ("Don't send", f"tr:{action_id}:reject")],
        [("I'll handle it", f"tr:{action_id}:escalate")],
    ]


def _buttons(card):
    out = []
    for w in card["cardsV2"][0]["card"]["sections"][0]["widgets"]:
        out += (w.get("buttonList") or {}).get("buttons", [])
    return out


# ── card construction ────────────────────────────────────────────────────────
def test_card_rewrites_buttons_to_tastingroom_endpoint():
    card = tr._approval_card("abc123", "Approve?", _rows())
    btns = _buttons(card)
    assert len(btns) == 3
    for b in btns:
        action = b["onClick"]["action"]
        # add-on form: function = TR endpoint URL, real callback rides in params
        assert action["function"].endswith("/webhooks/google-chat/tastingroom")
        assert action["parameters"][0]["key"] == "action"
        assert action["parameters"][0]["value"].startswith("tr:abc123:")


def test_card_preserves_every_decision_label():
    card = tr._approval_card("xyz", "Approve?", _rows("xyz"))
    values = {b["onClick"]["action"]["parameters"][0]["value"] for b in _buttons(card)}
    assert values == {"tr:xyz:approve", "tr:xyz:reject", "tr:xyz:escalate"}


# ── inbound click round-trip ──────────────────────────────────────────────────
def _addon_click(action_value):
    return {
        "chat": {
            "user": {"email": "cecil.park@winefornia.com"},
            "buttonClickedPayload": {"space": {"name": "spaces/AAAA"},
                                     "message": {"name": "spaces/AAAA/messages/1"}},
        },
        "commonEventObject": {"parameters": {"action": action_value}},
    }


def test_card_click_resumes_decision_engine(monkeypatch):
    captured = {}

    def fake(action_id, decision, decided_by):
        captured.update(action_id=action_id, decision=decision, decided_by=decided_by)
        return {"ok": True, "status": "escalated", "reservation_id": "r-42"}

    import services.tastingroom_service as svc
    monkeypatch.setattr(svc, "process_action_decision", fake)

    resp = asyncio.run(tr.handle_tastingroom_event(_addon_click("tr:abc123:escalate")))
    assert captured == {"action_id": "abc123", "decision": "escalate",
                        "decided_by": "gchat_cecil.park@winefornia.com"}
    # response is wrapped in the add-on envelope and updates the card in place
    msg = resp["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
    assert "r-42" in msg["text"]


def test_malformed_action_does_not_call_engine(monkeypatch):
    called = []
    import services.tastingroom_service as svc
    monkeypatch.setattr(svc, "process_action_decision",
                        lambda *a, **k: called.append(a) or {"ok": True})

    asyncio.run(tr.handle_tastingroom_event(_addon_click("not-a-tr-action")))
    asyncio.run(tr.handle_tastingroom_event(_addon_click("tr:onlytwo")))
    assert called == []  # neither malformed form reaches the decision engine


# ── config gating ─────────────────────────────────────────────────────────────
def test_disabled_without_space(monkeypatch):
    monkeypatch.setattr(tr.config, "GOOGLE_CHAT_TR_SPACE", "")
    assert tr.is_enabled() is False
    # post is a no-op when disabled (returns None, never hits the network)
    assert tr.post_action_card("abc", "Approve?", _rows()) is None


def test_disabled_without_service_account(monkeypatch):
    monkeypatch.setattr(tr.config, "GOOGLE_CHAT_TR_SPACE", "spaces/AAAA")
    monkeypatch.setattr(tr.config, "GOOGLE_CHAT_TR_SERVICE_ACCOUNT_JSON_B64", "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "")
    assert tr.is_enabled() is False


# ── #2 dedup ──────────────────────────────────────────────────────────────────
def test_message_dedup():
    tr._seen_messages.clear()
    assert tr._already_seen("spaces/S/messages/m1") is False  # first sight
    assert tr._already_seen("spaces/S/messages/m1") is True   # retry dropped
    assert tr._already_seen("") is False                       # empty never dedups


def test_retried_message_event_processed_once(monkeypatch):
    tr._seen_messages.clear()
    monkeypatch.setenv("GCHAT_ASYNC", "off")  # exercise the sync path
    calls = []
    import vertex_agent.chat_agent as chat_agent
    monkeypatch.setattr(chat_agent, "discuss",
                        lambda text, *, user="": calls.append(text) or "ok")

    # NOTE: not the word "status" — that now short-circuits deterministically
    # before discuss() (see services.tastingroom_status).
    ev = {
        "chat": {"user": {"email": "cecil.park@winefornia.com"},
                 "messagePayload": {"space": {"name": "spaces/S"},
                                    "message": {"name": "spaces/S/messages/dup", "text": "hello there"}}},
    }
    asyncio.run(tr.handle_tastingroom_event(ev))
    asyncio.run(tr.handle_tastingroom_event(ev))  # retry
    assert calls == ["hello there"]  # processed exactly once


# ── #1 ack-then-post deadline race ────────────────────────────────────────────
def _classic_message():
    return {"type": "MESSAGE", "space": {"name": "spaces/TEST"},
            "message": {"name": "spaces/TEST/messages/m", "text": "hi"}}


def test_fast_run_returns_sync_no_async_post(monkeypatch):
    posts = []

    async def fake_post(space, body):
        posts.append((space, body)); return True

    async def fast_route(ev):
        return {"text": "FAST"}

    monkeypatch.setattr(tr, "_post_message_to_space", fake_post)
    monkeypatch.setattr(tr, "_route", fast_route)
    monkeypatch.setattr(tr, "_ACK_DEADLINE", 0.5)

    resp = asyncio.run(tr.handle_tastingroom_event(_classic_message()))
    assert resp == {"text": "FAST"}   # classic (non-addon) returns the raw resp
    assert posts == []                # fast path never posts async


def test_slow_run_acks_then_posts_result(monkeypatch):
    posts = []

    async def fake_post(space, body):
        posts.append((space, body)); return True

    async def slow_route(ev):
        await asyncio.sleep(0.4)
        return {"text": "SLOW RESULT"}

    monkeypatch.setattr(tr, "_post_message_to_space", fake_post)
    monkeypatch.setattr(tr, "_route", slow_route)
    monkeypatch.setattr(tr, "_ACK_DEADLINE", 0.1)

    async def scenario():
        resp = await tr.handle_tastingroom_event(_classic_message())
        assert "Working on it" in resp["text"]   # acked synchronously
        await asyncio.sleep(0.6)                  # let work + post finish
        return resp

    asyncio.run(scenario())
    assert posts == [("spaces/TEST", {"text": "SLOW RESULT"})]  # delivered async, once


# ── #3 outbound post retry ────────────────────────────────────────────────────
def test_send_retries_then_succeeds(monkeypatch):
    attempts = []

    def flaky(space, body):
        attempts.append(1)
        if len(attempts) < 3:
            return False, "503: transient"
        return True, "spaces/S/messages/ok"

    monkeypatch.setattr(tr, "_send_message", flaky)
    monkeypatch.setattr(tr.time, "sleep", lambda *_: None)  # no real backoff in tests
    name = tr._send_with_retry("spaces/S", {"text": "x"}, attempts=3)
    assert name == "spaces/S/messages/ok"
    assert len(attempts) == 3


def test_send_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(tr, "_send_message", lambda s, b: (False, "500: boom"))
    monkeypatch.setattr(tr.time, "sleep", lambda *_: None)
    assert tr._send_with_retry("spaces/S", {"text": "x"}, attempts=2) is None


# ── approver allowlist (Cecil + Lisa) ─────────────────────────────────────────
def test_authorized_emails_default_includes_cecil_and_lisa():
    allow = tr.config.GOOGLE_CHAT_TR_AUTHORIZED_EMAILS
    assert "cecil.park@winefornia.com" in allow
    assert "lisa@innovatuswine.com" in allow


def test_empty_allowlist_fails_closed(monkeypatch):
    # An empty/malformed allowlist must deny everyone, not open to the workspace.
    monkeypatch.setattr(tr.config, "GOOGLE_CHAT_TR_AUTHORIZED_EMAILS", [])
    assert tr._is_authorized_approver("cecil.park@winefornia.com") is False
    assert tr._is_authorized_approver("") is False


def test_unauthorized_approver_is_blocked(monkeypatch):
    called = []
    import services.tastingroom_service as svc
    monkeypatch.setattr(svc, "process_action_decision",
                        lambda *a, **k: called.append(a) or {"ok": True})
    ev = {
        "chat": {"user": {"email": "stranger@nope.com"},
                 "buttonClickedPayload": {"space": {"name": "spaces/AAAA"},
                                          "message": {"name": "spaces/AAAA/messages/1"}}},
        "commonEventObject": {"parameters": {"action": "tr:abc:approve"}},
    }
    asyncio.run(tr.handle_tastingroom_event(ev))
    assert called == []  # unauthorized → decision engine never invoked


# ── duplicate click on an already-decided action stays silent ─────────────────
def test_retried_click_on_decided_action_is_silent(monkeypatch):
    import services.tastingroom_service as svc
    monkeypatch.setattr(svc, "process_action_decision",
                        lambda *a, **k: {"ok": False, "error": "Action already approved."})
    resp = asyncio.run(tr.handle_tastingroom_event(_addon_click("tr:abc:approve")))
    # no card update, no posted message
    assert resp == {} or resp.get("hostAppDataAction") is None
