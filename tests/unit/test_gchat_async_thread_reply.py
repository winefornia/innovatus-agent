"""Async ("slow op") replies must land in the originating Chat thread.

Both invoice adapters ack past _ACK_DEADLINE and later post the real result to
the space via the REST API. These tests pin the fix for the bug where that
late post carried no thread — the answer appeared as a new top-level message
instead of a reply. The incoming message.thread.name must be forwarded, and
the poster must ask for REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD so flat
("conversation view") spaces still work.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

import app.adapters.google_chat_adapter as gca
import app.adapters.google_chat_invoice_chat as gci
from app import config

THREAD = "spaces/ASYNCTEST/threads/Tabc"


def _message_event(text: str) -> dict:
    return {
        "type": "MESSAGE",
        "space": {"name": "spaces/ASYNCTEST"},
        "user": {"email": "tester@winefornia.com"},
        "message": {"name": "msg-async-1", "text": text, "thread": {"name": THREAD}},
    }


async def _drive_slow(handler, event, posted: dict, post_done: "asyncio.Event"):
    """Run a handler whose work outlives the ack deadline; return (ack, body)."""
    ack = await handler(event)
    await asyncio.wait_for(post_done.wait(), timeout=2)
    return ack, posted


def test_invoice_chat_slow_reply_threads(monkeypatch, mocker):
    monkeypatch.setattr(gci, "_ACK_DEADLINE", 0.05)
    monkeypatch.setenv("GCHAT_ASYNC", "on")
    monkeypatch.setattr(gci, "_service_account_info", lambda: {"sa": "yes"})
    monkeypatch.setattr(config, "GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS",
                        {"tester@winefornia.com"})

    def slow_discuss(text, *, user="", case=""):
        import time
        time.sleep(0.3)
        return "Here is the invoice draft."

    mocker.patch("vertex_agent.invoice_chat_agent.discuss", side_effect=slow_discuss)

    posted: dict = {}
    post_done = asyncio.Event()

    async def capture_post(space_name, body):
        posted.update(space=space_name, body=body)
        post_done.set()
        return True

    monkeypatch.setattr(gci, "_post_message_to_space", capture_post)

    ack, _ = asyncio.run(_drive_slow(
        gci.handle_invoice_chat_event, _message_event("invoice oak barrel 3 cases"),
        posted, post_done))

    assert "Working on it" in ack["text"]
    assert posted["space"] == "spaces/ASYNCTEST"
    assert posted["body"]["text"] == "Here is the invoice draft."
    assert posted["body"]["thread"] == {"name": THREAD}


def test_wizard_slow_reply_threads(monkeypatch):
    monkeypatch.setattr(gca, "_ACK_DEADLINE", 0.05)
    monkeypatch.setenv("GCHAT_ASYNC", "on")

    async def slow_route(ev):
        await asyncio.sleep(0.3)
        return {"text": "Approval card ready.", "cardsV2": []}

    monkeypatch.setattr(gca, "_route_event", slow_route)

    posted: dict = {}
    post_done = asyncio.Event()

    async def capture_post(space_name, body):
        posted.update(space=space_name, body=body)
        post_done.set()
        return True

    monkeypatch.setattr(gca, "_post_message_to_space", capture_post)

    ack, _ = asyncio.run(_drive_slow(
        gca.handle_google_chat_event, _message_event("3 cases cab sauv for Tom"),
        posted, post_done))

    assert "Working on it" in ack["text"]
    assert posted["body"]["text"] == "Approval card ready."
    assert posted["body"]["thread"] == {"name": THREAD}


def test_wizard_slow_reply_without_thread_stays_bare(monkeypatch):
    """No incoming thread (e.g. some card clicks) → body unchanged, no thread key."""
    monkeypatch.setattr(gca, "_ACK_DEADLINE", 0.05)
    monkeypatch.setenv("GCHAT_ASYNC", "on")

    async def slow_route(ev):
        await asyncio.sleep(0.3)
        return {"text": "done"}

    monkeypatch.setattr(gca, "_route_event", slow_route)

    posted: dict = {}
    post_done = asyncio.Event()

    async def capture_post(space_name, body):
        posted.update(space=space_name, body=body)
        post_done.set()
        return True

    monkeypatch.setattr(gca, "_post_message_to_space", capture_post)

    event = {"type": "MESSAGE", "space": {"name": "spaces/ASYNCTEST"},
             "message": {"name": "msg-async-2", "text": "hi"}}
    asyncio.run(_drive_slow(gca.handle_google_chat_event, event, posted, post_done))
    assert "thread" not in posted["body"]


def test_post_message_sync_sets_reply_option(monkeypatch, mocker):
    """The REST poster must request REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD when
    (and only when) the body targets a thread."""
    monkeypatch.setattr(gci, "_refresh_token", lambda: "tok")
    calls = []

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            calls.append(url)
            return MagicMock(status_code=200)

    mocker.patch.object(gci.httpx, "Client", FakeClient)

    assert gci._post_message_sync("spaces/X", {"text": "t", "thread": {"name": THREAD}})
    assert calls[-1].endswith("?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD")

    assert gci._post_message_sync("spaces/X", {"text": "t"})
    assert "messageReplyOption" not in calls[-1]
