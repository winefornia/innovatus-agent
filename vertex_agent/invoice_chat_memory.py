"""Per-case conversation memory for the invoicing chat assistant.

A CASE is one conversation with staff — one Google Chat space per sender (see
case_key for why NOT per thread). Each chat turn runs a fresh, memory-less
agent (see invoice_chat_agent.discuss), so without this store a follow-up like
"2023, Other tier" arrives with zero context and the agent re-asks for facts the
staff already gave. The case stays live until it goes quiet for _CASE_TTL —
long enough to carry an order from first paste through clarifications to the
drafted invoice, and to still be there when staff come back to say "send it". The store holds a rolling transcript per case, server-side,
exactly like the pending-confirm store in invoice_chat_actions: process-local,
TTL'd, and bounded.

The adapter derives the case key (case_key) and passes it to discuss(), which
replays the transcript above the new message as "[conversation so far]" and
records both sides of the exchange afterwards.

Durability: the in-process store is a fast cache; every turn is ALSO appended
best-effort to Supabase invoice_chat_turns. On a cache miss (process restart,
LRU eviction) render_case rehydrates the recent window from the durable copy,
so a mid-order restart no longer loses the conversation. Months-old talk is
deliberately NOT rehydrated into the live window — that recall goes through
the past_conversations search tool instead. Persistence failures trip a
circuit breaker (_persist_broken) so a dead DB degrades to the old in-memory
behavior instead of stalling every turn.

Bounds, so a hot space can't grow the prompt or the process without limit:
  - per case:  the last _MAX_TURNS entries, each capped at _ENTRY_MAX_CHARS
    (an attached-PDF digest is consumed the turn it arrives; memory keeps only
    its head as a reminder of WHAT was sent, not the full text)
  - per store: _MAX_CASES live cases, LRU-evicted; a case expires _CASE_TTL
    after its last message
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_MAX_CASES = 200
_MAX_TURNS = 16          # entries (a staff+assistant exchange is 2)
_ENTRY_MAX_CHARS = 1500
_CASE_TTL = 48 * 3600    # seconds of silence before a case goes stale — long
                         # enough that "send Christina's invoice" the next day
                         # still lands in the same case; final invoice state is
                         # durable in Square/Supabase regardless

# case key -> {"ts": float, "turns": [{"role": "staff"|"assistant", "text": str}]}
_cases: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_lock = threading.Lock()

_ROLE_LABELS = {"staff": "Staff", "assistant": "You"}

# Circuit breaker for the durable copy: after this many consecutive failures we
# stop trying for the rest of the process (a dead DB must not slow every turn).
_PERSIST_MAX_FAILURES = 3
_persist_failures = 0
_persist_broken = False


def _persist_turn(key: str, role: str, text: str) -> None:
    """Best-effort append to the durable transcript (invoice_chat_turns)."""
    global _persist_failures, _persist_broken
    if _persist_broken:
        return
    try:
        from db.repository import insert_chat_turn
        insert_chat_turn(key, role, text)
        _persist_failures = 0
    except Exception as exc:
        _persist_failures += 1
        if _persist_failures >= _PERSIST_MAX_FAILURES:
            _persist_broken = True
            log.warning("[inv:chat-memory] persistence disabled after %d failures: %s",
                        _persist_failures, exc)
        else:
            log.info("[inv:chat-memory] durable append failed: %s", exc)


def _rehydrate(key: str) -> dict | None:
    """Rebuild a case's rolling window from the durable copy after a cache miss.

    Only turns inside the live-case window (_CASE_TTL) are restored — anything
    older belongs to a finished conversation and is reachable via the
    past_conversations search tool, not the live prompt."""
    if _persist_broken:
        return None
    try:
        from datetime import datetime, timezone
        from db.repository import list_chat_turns_for_case
        rows = list_chat_turns_for_case(key, limit=_MAX_TURNS)
    except Exception as exc:
        log.info("[inv:chat-memory] rehydrate failed: %s", exc)
        return None
    turns = []
    now = time.time()
    for r in rows:
        role, text = r.get("role"), (r.get("text") or "").strip()
        if role not in _ROLE_LABELS or not text:
            continue
        try:
            created = datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00"))
            age = now - created.timestamp()
        except Exception:
            age = 0.0
        if age > _CASE_TTL:
            continue
        turns.append({"role": role, "text": text[:_ENTRY_MAX_CHARS]})
    if not turns:
        return None
    return {"ts": now, "turns": turns[-_MAX_TURNS:]}


def case_key(thread: str = "", space: str = "", user: str = "") -> str:
    """Identify the case a message belongs to: SPACE + SENDER.

    NOT the thread: in Google Chat's default flat ("conversation view") spaces
    every message arrives with a brand-new thread.name, so keying by thread
    starts an empty case on each message — exactly the amnesia this store
    exists to prevent (learned in production). The space is the stable identity
    of a conversation (the invoicing space / DM), scoped per sender so two
    staff working in one space don't cross wires. The thread is only a last
    resort when no space is present. Returns "" when there is nothing to key
    on (memory is then skipped entirely).
    """
    space = (space or "").strip()
    user = (user or "").strip().lower()
    if space and user:
        return f"{space}|{user}"
    if space:
        return space
    return (thread or "").strip()


def record_turn(key: str, role: str, text: str) -> None:
    """Append one side of an exchange to the case transcript. No-ops on blanks."""
    text = (text or "").strip()
    if not key or not text or role not in _ROLE_LABELS:
        return
    if len(text) > _ENTRY_MAX_CHARS:
        text = text[:_ENTRY_MAX_CHARS].rstrip() + " … [truncated]"
    _persist_turn(key, role, text)
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
        # Cache miss (restart / LRU eviction) — try the durable copy.
        entry = _rehydrate(key)
        if entry:
            with _lock:
                _cases[key] = entry
                _cases.move_to_end(key)
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
