"""Regression tests for the Google Chat add-on event-format adapter.

These lock in the fix for Google's migration to the new Chat app event format:
  - inbound events nest under `chat` (messagePayload / buttonClickedPayload)
    instead of a top-level `type`/`message`/`space`
  - the synchronous response must be wrapped in hostAppDataAction
  - card button actions must use the full endpoint URL, with the real action
    name carried in action.parameters (a bare function name is never delivered)

If any of these silently regress, Google Chat goes back to "not responding" or
"unable to process your request" with no error in our logs — which is exactly
the failure mode these tests exist to catch.
"""
from app.adapters import gchat_format as gca

# Back-compat aliases so the tests read against the public API.
gca._normalize_addon_event = gca.normalize_addon_event
gca._rewrite_card_buttons = gca.rewrite_card_buttons
gca._wrap_addon_response = gca.wrap_addon_response


def _addon_message(text, space="spaces/abc"):
    return {
        "commonEventObject": {"hostApp": "CHAT"},
        "chat": {
            "user": {"email": "x@y.com", "displayName": "X"},
            "messagePayload": {
                "space": {"name": space, "spaceType": "DIRECT_MESSAGE"},
                "message": {"name": f"{space}/messages/m1", "text": text},
            },
        },
    }


def test_normalize_addon_message():
    norm = gca._normalize_addon_event(_addon_message("hello"))
    assert norm["type"] == "MESSAGE"
    assert norm["message"]["text"] == "hello"
    assert norm["space"]["name"] == "spaces/abc"
    assert norm["user"]["email"] == "x@y.com"


def test_normalize_addon_click_reads_action_param():
    event = {
        "commonEventObject": {"parameters": {"action": "gc_confirm_yes"}},
        "chat": {
            "user": {"email": "x@y.com"},
            "buttonClickedPayload": {
                "space": {"name": "spaces/abc"},
                "message": {"name": "spaces/abc/messages/m1"},
            },
        },
    }
    norm = gca._normalize_addon_event(event)
    assert norm["type"] == "CARD_CLICKED"
    assert norm["action"]["actionMethodName"] == "gc_confirm_yes"
    assert norm["space"]["name"] == "spaces/abc"


def test_rewrite_card_buttons_to_full_url():
    cards = [{"cardId": "c", "card": {"sections": [{"widgets": [
        {"buttonList": {"buttons": [
            {"text": "Yes", "onClick": {"action": {"function": "gc_confirm_yes"}}},
        ]}},
    ]}]}}]
    gca._rewrite_card_buttons(cards)
    action = cards[0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0]["onClick"]["action"]
    assert action["function"].startswith("http")          # full URL, not a bare name
    assert action["parameters"] == [{"key": "action", "value": "gc_confirm_yes"}]


def test_wrap_addon_response_text():
    wrapped = gca._wrap_addon_response({"text": "hi"})
    msg = wrapped["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
    assert msg["text"] == "hi"


def test_wrap_addon_response_drops_empty():
    # dedup / no-op responses must not post an invalid empty message
    assert gca._wrap_addon_response({"text": ""}) == {}
    assert gca._wrap_addon_response({}) == {}


def test_wrap_addon_response_rewrites_card_buttons():
    resp = {"cardsV2": [{"cardId": "c", "card": {"sections": [{"widgets": [
        {"buttonList": {"buttons": [
            {"text": "Approve", "onClick": {"action": {"function": "gc_approve"}}},
        ]}},
    ]}]}}]}
    wrapped = gca._wrap_addon_response(resp)
    btn = (wrapped["hostAppDataAction"]["chatDataAction"]["createMessageAction"]
           ["message"]["cardsV2"][0]["card"]["sections"][0]["widgets"][0]
           ["buttonList"]["buttons"][0])
    assert btn["onClick"]["action"]["function"].startswith("http")
    assert btn["onClick"]["action"]["parameters"][0]["value"] == "gc_approve"
