"""Confirm-card for the conversational invoicing agent.

The agent's reply stays LLM-authored, but closing the deal is deterministic:
when a turn stages a confirm-first action, the adapter attaches a card with
FIXED invchat_confirm / invchat_cancel buttons, and a tap arrives back as
CARD_CLICKED on the default /webhooks/google-chat route where it calls
confirm/cancel_pending_action directly — no LLM parses the approval. Typing
"yes" keeps working as before; the card is an additional path to the SAME
executor.
"""

import asyncio
import time

import pytest

import app.adapters.google_chat_invoice_chat as gci
import vertex_agent.invoice_chat_actions as ica
from app import config

TESTER = "tester@winefornia.com"
DECIDED_BY = f"gchat_{TESTER}"
SPACE = "spaces/CARDTEST"


@pytest.fixture(autouse=True)
def _sync_and_authorized(monkeypatch):
    """Run handlers synchronously and authorize the tester; isolate the stores."""
    monkeypatch.setenv("GCHAT_ASYNC", "off")
    monkeypatch.setattr(config, "GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS", [TESTER])
    monkeypatch.setattr(ica, "_PENDING", {})
    monkeypatch.setattr(gci, "_seen_messages", type(gci._seen_messages)())


def _message_event(text: str, name: str = "msg-card-1") -> dict:
    return {
        "type": "MESSAGE",
        "space": {"name": SPACE},
        "user": {"email": TESTER},
        "message": {"name": name, "text": text},
    }


def _click_event(action: str, email: str = TESTER) -> dict:
    return {
        "type": "CARD_CLICKED",
        "space": {"name": SPACE},
        "user": {"email": email},
        "action": {"actionMethodName": action},
        "message": {"name": "msg-card-1"},
    }


def _card_functions(resp: dict) -> list[str]:
    fns = []
    for entry in resp.get("cardsV2") or []:
        for section in entry["card"]["sections"]:
            for widget in section["widgets"]:
                for btn in (widget.get("buttonList") or {}).get("buttons", []):
                    fns.append(btn["onClick"]["action"]["function"])
    return fns


def _stage_during_discuss(mocker, reply: str = "Draft ready — confirm?"):
    """Mock discuss() to stage a pending action for the acting user, the way a
    stage_* tool would."""
    def fake_discuss(text, *, user="", case=""):
        ica._PENDING[user] = {"kind": "invoice", "params": {},
                              "summary": "Invoice Oak Barrel $1,234", "ts": time.time()}
        return reply
    return mocker.patch("vertex_agent.invoice_chat_agent.discuss",
                        side_effect=fake_discuss)


# ── staging turns grow the card ───────────────────────────────────────────────

def test_staging_turn_attaches_confirm_card(mocker):
    _stage_during_discuss(mocker)
    resp = asyncio.run(gci.handle_invoice_chat_event(_message_event("invoice Oak Barrel")))
    assert resp["text"] == "Draft ready — confirm?"
    assert _card_functions(resp) == ["invchat_confirm", "invchat_cancel"]


def test_no_card_when_nothing_staged(mocker):
    mocker.patch("vertex_agent.invoice_chat_agent.discuss",
                 return_value="Wholesale on the Viognier is $41.")
    resp = asyncio.run(gci.handle_invoice_chat_event(_message_event("price?")))
    assert "cardsV2" not in resp


def test_no_card_when_pending_predates_the_turn(mocker):
    # A side question while something is already staged must not re-post buttons.
    ica._PENDING[DECIDED_BY] = {"kind": "invoice", "params": {},
                                "summary": "old", "ts": time.time() - 5}
    mocker.patch("vertex_agent.invoice_chat_agent.discuss",
                 return_value="Wholesale on the Viognier is $41.")
    resp = asyncio.run(gci.handle_invoice_chat_event(_message_event("price?", "msg-card-2")))
    assert "cardsV2" not in resp


# ── clicks close the deal deterministically ───────────────────────────────────

def test_confirm_click_executes_pending(mocker):
    confirm = mocker.patch("vertex_agent.invoice_chat_actions.confirm_pending_action",
                           return_value="✅ Invoice created and sent.")
    resp = asyncio.run(gci.handle_invoice_chat_event(_click_event("invchat_confirm")))
    confirm.assert_called_once()
    assert resp["text"] == "✅ Invoice created and sent."
    assert resp["actionResponse"]["type"] == "UPDATE_MESSAGE"


def test_confirm_click_acts_as_the_tapping_user():
    # End-to-end through the REAL pending store: the contextvar set in the click
    # thread must resolve to the tapper, so cancel pops exactly their entry.
    ica._PENDING[DECIDED_BY] = {"kind": "invoice", "params": {},
                                "summary": "s", "ts": time.time()}
    resp = asyncio.run(gci.handle_invoice_chat_event(_click_event("invchat_cancel")))
    assert ica._PENDING == {}
    assert "nothing changed" in resp["text"]


def test_click_with_nothing_pending_is_harmless():
    resp = asyncio.run(gci.handle_invoice_chat_event(_click_event("invchat_confirm")))
    assert "nothing waiting" in resp["text"].lower()


def test_unauthorized_click_blocked(mocker):
    confirm = mocker.patch("vertex_agent.invoice_chat_actions.confirm_pending_action")
    resp = asyncio.run(gci.handle_invoice_chat_event(
        _click_event("invchat_confirm", email="stranger@example.com")))
    confirm.assert_not_called()
    assert "not authorized" in resp["text"]
    assert resp["actionResponse"]["type"] == "UPDATE_MESSAGE"


def test_unknown_action_rejected(mocker):
    confirm = mocker.patch("vertex_agent.invoice_chat_actions.confirm_pending_action")
    resp = asyncio.run(gci.handle_invoice_chat_event(_click_event("tr:abc:approve")))
    confirm.assert_not_called()
    assert resp["text"] == "Unrecognized action."


def test_addon_click_normalizes_and_confirms(mocker):
    # New add-on format: the fixed action rides back in commonEventObject.parameters.
    mocker.patch("vertex_agent.invoice_chat_actions.confirm_pending_action",
                 return_value="✅ Done.")
    event = {
        "chat": {
            "user": {"email": TESTER},
            "buttonClickedPayload": {"space": {"name": SPACE},
                                     "message": {"name": "msg-card-1"}},
        },
        "commonEventObject": {"parameters": {"action": "invchat_confirm"}},
    }
    resp = asyncio.run(gci.handle_invoice_chat_event(event))
    msg = resp["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
    assert msg["text"] == "✅ Done."
