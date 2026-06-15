"""Tests for the Google Chat Workspace Add-on format bridge.

Covers the bugs that broke the live invoice flow this session:
  - inbound add-on events not translated to the classic shape
  - card buttons not rewritten to the endpoint-URL callback form (clicks never
    reached the app → "unable to process")
  - responses not wrapped in the hostAppDataAction envelope
"""
from app.adapters.gchat_format import (
    normalize_addon_event,
    wrap_addon_response,
    rewrite_card_buttons,
    CHAT_ENDPOINT_URL,
)


def test_normalize_message_event():
    ev = {
        "commonEventObject": {},
        "chat": {
            "user": {"email": "cecil@winefornia.com"},
            "messagePayload": {
                "message": {"text": "invoice Acme 12 cab", "name": "spaces/S/messages/M"},
                "space": {"name": "spaces/S"},
            },
        },
    }
    out = normalize_addon_event(ev)
    assert out["type"] == "MESSAGE"
    assert out["message"]["text"] == "invoice Acme 12 cab"
    assert out["space"]["name"] == "spaces/S"
    assert out["user"]["email"] == "cecil@winefornia.com"


def test_normalize_button_click_reads_action_from_parameters():
    # The clicked action name rides in commonEventObject.parameters (because the
    # button function is rewritten to the endpoint URL).
    ev = {
        "commonEventObject": {"parameters": {"action": "gc_approve"}},
        "chat": {
            "user": {"email": "x@y.com"},
            "buttonClickedPayload": {
                "message": {"name": "spaces/S/messages/M"},
                "space": {"name": "spaces/S"},
            },
        },
    }
    out = normalize_addon_event(ev)
    assert out["type"] == "CARD_CLICKED"
    assert out["action"]["actionMethodName"] == "gc_approve"
    assert out["space"]["name"] == "spaces/S"


def test_normalize_added_to_space():
    ev = {"commonEventObject": {}, "chat": {"addedToSpacePayload": {"space": {"name": "spaces/S"}}}}
    assert normalize_addon_event(ev)["type"] == "ADDED_TO_SPACE"


def test_rewrite_card_buttons_to_endpoint_callback():
    cards = [{"card": {"sections": [{"widgets": [
        {"buttonList": {"buttons": [
            {"text": "Approve", "onClick": {"action": {"function": "gc_approve"}}}
        ]}}
    ]}]}}]
    rewrite_card_buttons(cards)
    action = cards[0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0]["onClick"]["action"]
    assert action["function"] == CHAT_ENDPOINT_URL
    assert action["parameters"] == [{"key": "action", "value": "gc_approve"}]


def test_wrap_text_response_in_envelope():
    out = wrap_addon_response({"text": "done"})
    assert out == {"hostAppDataAction": {"chatDataAction": {
        "createMessageAction": {"message": {"text": "done"}}}}}


def test_wrap_empty_response_is_noop():
    assert wrap_addon_response({"text": ""}) == {}
    assert wrap_addon_response({}) == {}


def test_wrap_card_response_rewrites_buttons():
    resp = {"cardsV2": [{"card": {"sections": [{"widgets": [
        {"buttonList": {"buttons": [
            {"text": "Yes", "onClick": {"action": {"function": "gc_confirm_yes"}}}
        ]}}
    ]}]}}]}
    out = wrap_addon_response(resp)
    btn = (out["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
           ["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0])
    assert btn["onClick"]["action"]["function"] == CHAT_ENDPOINT_URL
    assert btn["onClick"]["action"]["parameters"] == [{"key": "action", "value": "gc_confirm_yes"}]
