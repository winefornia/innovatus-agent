"""Per-case conversation memory for the invoicing chat assistant.

A CASE is one conversation with staff — one Google Chat thread (or, in a
threadless DM, one space+sender pair). Each chat turn runs a fresh, memory-less
agent (see invoice_chat_agent.discuss), so without this store a follow-up like
"2023, Other tier" arrives with zero context and the agent re-asks for facts the
staff already gave. The store holds a rolling transcript per case, server-side,
exactly like the pending-confirm store in invoice_chat_actions: process-local,
TTL'd, and bounded.

The adapter derives the case key (case_key) and passes it to discuss(), which
replays the transcript above the new message as "[conversation so far]" and
records both sides of the exchange afterwards.

Bounds, so a hot space can't grow the prompt or the process without limit:
  - per case:  the last _MAX_TURNS entries, each capped at _ENTRY_MAX_CHARS
    (an attached-PDF digest is consumed the turn it arrives; memory keeps only
    its head as a reminder of WHAT was sent, not the full text)
  - per store: _MAX_CASES live cases, LRU-evicted; a case expires _CASE_TTL
    after its last message
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

_MAX_CASES = 200
_MAX_TURNS = 12          # entries (a staff+assistant exchange is 2)
_ENTRY_MAX_CHARS = 1500
_CASE_TTL = 6 * 3600     # seconds of silence before a case goes stale

# case key -> {"ts": float, "turns": [{"role": "staff"|"assistant", "text": str}]}
_cases: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_lock = threading.Lock()

_ROLE_LABELS = {"staff": "Staff", "assistant": "You"}


def case_key(thread: str = "", space: str = "", user: str = "") -> str:
    """Identify the case a message belongs to.

    The Chat thread name is the case identity — every reply in the same thread
    lands in the same case. Threadless surfaces (DMs) fall back to space+sender,
    so one person's DM conversation is still a single case. Returns "" when
    there is nothing to key on (memory is then skipped entirely).
    """
    thread = (thread or "").strip()
    if thread:
        return thread
    space, user = (space or "").strip(), (user or "").strip().lower()
    if space and user:
        return f"{space}|{user}"
    return ""


def record_turn(key: str, role: str, text: str) -> None:
    """Append one side of an exchange to the case transcript. No-ops on blanks."""
    text = (text or "").strip()
    if not key or not text or role not in _ROLE_LABELS:
        return
    if len(text) > _ENTRY_MAX_CHARS:
        text = text[:_ENTRY_MAX_CHARS].rstrip() + " … [truncated]"
    with _lock:
        entry = _cases.get(key)
        if entry is None or time.time() - entry["ts"] > _CASE_TTL:
            entry = {"ts": time.time(), "turns": []}
        entry["ts"] = time.time()
        entry["turns"] = entry["turns"][-(_MAX_TURNS - 1):] + [{"role": role, "text": text}]
        _cases[key] = entry
        _cases.move_to_end(key)
        while len(_cases) > _MAX_CASES:
            _cases.popitem(last=False)


def render_case(key: str) -> str:
    """The case transcript as a prompt-ready block, oldest first. "" when empty."""
    if not key:
        return ""
    with _lock:
        entry = _cases.get(key)
        if not entry:
            return ""
        if time.time() - entry["ts"] > _CASE_TTL:
            _cases.pop(key, None)
            return ""
        _cases.move_to_end(key)
        turns = list(entry["turns"])
    return "\n".join(f"{_ROLE_LABELS[t['role']]}: {t['text']}" for t in turns)


def forget_case(key: str) -> None:
    """Drop a case's transcript (tests / explicit resets)."""
    with _lock:
        _cases.pop(key or "", None)
