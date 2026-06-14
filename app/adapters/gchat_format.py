"""Pure helpers for Google Chat's add-on event format.

No I/O, no heavy imports — just the translation between Google's new add-on
event/response shape and the classic shape the dispatcher understands. Kept
separate from google_chat_adapter so it can be unit-tested without pulling in
httpx, the invoice graph, or the database.

Google's new ("Workspace add-on") format:
  - inbound events nest under `chat` (messagePayload / buttonClickedPayload …)
    plus a top-level `commonEventObject`, instead of a top-level type/message/space
  - the synchronous response is wrapped in hostAppDataAction.chatDataAction
  - card button actions must use the full endpoint URL; the real action name
    rides in action.parameters and comes back in commonEventObject.parameters
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# In the add-on format, a card button's action.function must be the app's full
# HTTP endpoint URL (not a bare method name like "gc_confirm_yes"); otherwise
# Google has nowhere to deliver the click and shows "unable to process".
CHAT_ENDPOINT_URL = os.environ.get(
    "GOOGLE_CHAT_ENDPOINT_URL",
    "https://winefornia-agent.fly.dev/webhooks/google-chat",
)


def normalize_addon_event(event: dict) -> dict:
    """Convert a new-format (commonEventObject/chat) event to the classic shape."""
    chat = event.get("chat", {}) or {}
    common = event.get("commonEventObject", {}) or {}
    user = chat.get("user", {}) or {}

    # Button click: our cards inject the action name as a parameter "action".
    # commonEventObject.parameters carries it back; message/space context comes
    # in buttonClickedPayload (or messagePayload as a fallback).
    params = common.get("parameters", {}) or {}
    bcp = chat.get("buttonClickedPayload") or {}
    action_name = params.get("action") or params.get("__action_method_name__")
    if action_name or bcp:
        src = bcp or chat.get("messagePayload") or {}
        return {"type": "CARD_CLICKED",
                "action": {"actionMethodName": action_name or ""},
                "space": src.get("space", {}) or {},
                "message": src.get("message", {}) or {},
                "user": user, "_addon_payload": bcp}

    if "messagePayload" in chat:
        mp = chat["messagePayload"] or {}
        return {"type": "MESSAGE", "message": mp.get("message", {}) or {},
                "space": mp.get("space", {}) or {}, "user": user}
    if "addedToSpacePayload" in chat:
        asp = chat["addedToSpacePayload"] or {}
        return {"type": "ADDED_TO_SPACE", "space": asp.get("space", {}) or {}, "user": user}
    if "removedFromSpacePayload" in chat:
        rsp = chat["removedFromSpacePayload"] or {}
        return {"type": "REMOVED_FROM_SPACE", "space": rsp.get("space", {}) or {}, "user": user}
    log.warning("[gc:addon] unrecognized chat keys=%s common keys=%s",
                list(chat.keys()), list(common.keys()))
    return {"type": "", "user": user}


def rewrite_card_buttons(cardsv2: list) -> None:
    """In place: convert bare action.function names to the add-on full-URL form.

    {"onClick": {"action": {"function": "gc_confirm_yes"}}}
      becomes
    {"onClick": {"action": {"function": "<endpoint>",
                            "parameters": [{"key": "action", "value": "gc_confirm_yes"}]}}}
    """
    for entry in cardsv2 or []:
        card = (entry or {}).get("card", {}) or {}
        for section in card.get("sections", []) or []:
            for widget in section.get("widgets", []) or []:
                for btn in (widget.get("buttonList", {}) or {}).get("buttons", []) or []:
                    action = (btn.get("onClick", {}) or {}).get("action", {}) or {}
                    fn = action.get("function")
                    if fn and not fn.startswith("http"):
                        action["parameters"] = [{"key": "action", "value": fn}]
                        action["function"] = CHAT_ENDPOINT_URL


def wrap_addon_response(resp: dict) -> dict:
    """Wrap a classic Chat response in the add-on hostAppDataAction envelope."""
    if not isinstance(resp, dict):
        return {}
    # A Chat Message resource accepts text / cardsV2 / accessoryWidgets only.
    message = {k: v for k, v in resp.items() if k in ("text", "cardsV2", "accessoryWidgets")}
    # Drop empty/no-op responses (e.g. dedup returns {"text": ""}) so we don't
    # post an invalid empty message.
    if not message.get("text") and not message.get("cardsV2"):
        return {}
    if message.get("cardsV2"):
        rewrite_card_buttons(message["cardsV2"])
    return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": message}}}}
