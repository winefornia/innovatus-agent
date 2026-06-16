"""Tests for the tasting-room Google Chat approval adapter.

Covers the migration-critical behaviors:
  - approval cards reuse the Telegram-style "tr:{action_id}:{decision}" callbacks
    and rewrite them to the tasting-room endpoint (separate Chat app)
  - a Workspace Add-on button click parses back to (action_id, decision) and
    resumes the channel-agnostic process_action_decision()
  - the channel is config-gated (dormant until GOOGLE_CHAT_TR_SPACE is set)
  - malformed / non-tr actions fail safe
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
