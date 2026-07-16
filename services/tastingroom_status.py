"""Deterministic status fast-path for the tasting-room staff chat.

When staff ask a plain status question — "status", "what's open", "status of
Mira" — this module answers straight from the goal model with a fixed template:
no LLM round-trip, so the reply is near-instant and looks identical every time.
Anything it can't answer confidently (an unmatched phrase, a name that resolves
to nothing) returns None and the Google Chat adapter falls through to the
conversational assistant (vertex_agent.chat_agent.discuss) exactly as before.

Read-only by construction: it never writes, stages, or sends anything.

Three distinct status levels, all derived from vertex_agent.goal_model.GoalState:
  case level   🔴 blocked (someone said no → client must pick a new time)
               🟡 waiting on someone outside (client reply, Josh, payment)
               🟢 on track — the next move is ours
  party level  ✅ done · ⏳ in flight · 🔴 blocked · ◻️ not started
  next action  the first open gap, phrased via vertex_agent.tools._GAP_STAGE
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

# ── Query matching ────────────────────────────────────────────────────────────
# Deliberately narrow: only phrases that are unambiguously "give me the status"
# are claimed here. Questions with any nuance ("why is Mira waiting?", "show me
# the mail") keep going to the assistant, which has the full toolset.

_BOARD_PHRASES = {
    "status", "/status", "status update", "status board", "case status",
    "show status", "show the status", "give me a status", "give me the status",
    "what's the status", "what is the status",
    "what's open", "what is open", "open cases", "show open cases",
    "pending", "what's pending", "what is pending", "show pending",
    "where are things", "where are we",
}

_CASE_PATTERNS = [
    re.compile(r"^/status\s+(.+)$"),
    re.compile(r"^status\s+(?:of|for|on)\s+(.+)$"),
    re.compile(r"^what(?:'s| is)\s+the\s+status\s+(?:of|for|on)\s+(.+)$"),
]


def match_status_query(text: str) -> tuple[str, str] | None:
    """Classify a staff message: ("board", "") | ("case", query) | None."""
    t = " ".join((text or "").replace("’", "'").lower().split()).rstrip("?!. ")
    if not t:
        return None
    if t in _BOARD_PHRASES:
        return ("board", "")
    for pat in _CASE_PATTERNS:
        m = pat.match(t)
        if m:
            q = m.group(1).strip().strip("\"'").rstrip("?!. ")
            q = re.sub(r"^the\s+", "", q)
            q = re.sub(r"(?:'s)?\s+case$", "", q)
            return ("case", q) if q else ("board", "")
    return None


# ── Case-level traffic light ──────────────────────────────────────────────────
# Keyed by the case's first open gap (see GoalState.gaps / open_cases_status):
# 🟢 the next move is ours, 🟡 waiting on someone outside, 🔴 blocked.

_STAGE_EMOJI = {
    "ask_client_alternatives": "🔴",
    "need_cecil_availability": "🟢",
    "need_cecil_approval":     "🟢",
    "need_josh_availability":  "🟡",
    "offer_slot_to_customer":  "🟢",
    "offer_slot_to_client":    "🟢",
    "send_invoice":            "🟢",
    "await_or_check_payment":  "🟡",
    "send_final_confirmation": "🟢",
    "awaiting_reply":          "🟡",
}
_SEVERITY = {"🔴": 0, "🟡": 1, "🟢": 2}


def _fmt_date(date_str) -> str:
    if not date_str:
        return ""
    try:
        return datetime.fromisoformat(str(date_str)[:10]).strftime("%a %b %-d")
    except Exception:
        return str(date_str)


def _fmt_slot(date_str, time_str) -> str:
    parts = []
    if date_str:
        try:
            parts.append(datetime.fromisoformat(str(date_str)[:10]).strftime("%A, %B %-d"))
        except Exception:
            parts.append(str(date_str))
    if time_str:
        try:
            parts.append("at " + datetime.strptime(str(time_str)[:5], "%H:%M")
                          .strftime("%-I:%M %p").lower())
        except Exception:
            parts.append("at " + str(time_str))
    return " ".join(parts)


# ── Board view ────────────────────────────────────────────────────────────────

def render_board(cases: list[dict]) -> str:
    """One scannable line per open case, blocked first. Google Chat markup."""
    if not cases:
        return "No open tasting cases — all caught up. 🎉"
    lines: list[tuple[int, str]] = []
    for c in cases:
        emoji = _STAGE_EMOJI.get(c.get("stage") or "awaiting_reply", "🟡")
        bits = [_fmt_date(c.get("date")) or "no date yet"]
        if c.get("case_type") == "production_tour":
            bits.append("production tour")
        bits.append(c.get("waiting_on") or "")
        lines.append((_SEVERITY.get(emoji, 1),
                      f"{emoji} *{c.get('client_name') or '(no name yet)'}* — " + " · ".join(b for b in bits if b)))
    lines.sort(key=lambda p: p[0])  # stable: keeps recency order within a level
    return "\n".join([
        f"*Open tasting cases ({len(lines)})*",
        *[l for _, l in lines],
        "",
        "_Say \"status of <name>\" for the full picture of one case._",
    ])


# ── Case view ─────────────────────────────────────────────────────────────────

def _line_cecil(status: str, tour: bool) -> str:
    if status == "ok":
        return "✅ Winefornia (Cecil) — " + ("available for the slot" if tour else "approved")
    if status == "blocked":
        return "🔴 Winefornia (Cecil) — not available for the requested time"
    return "◻️ Winefornia (Cecil) — " + ("availability not confirmed yet" if tour else "approval not confirmed yet")


def _line_josh(status: str) -> str:
    if status == "confirmed":
        return "✅ Josh (facility) — confirmed the slot"
    if status == "unavailable":
        return "🔴 Josh (facility) — not available for the requested time"
    return "◻️ Josh (facility) — availability unknown"


def _line_customer(status: str) -> str:
    if status == "accepted":
        return "✅ Customer — accepted the slot"
    if status == "offered":
        return "⏳ Customer — slot offered, awaiting their reply"
    if status == "declined":
        return "🔴 Customer — declined, needs a new time"
    return "◻️ Customer — no slot offered yet"


def _line_invoice(status: str) -> str:
    if status == "paid":
        return "✅ Invoice — paid"
    if status == "sent":
        return "⏳ Invoice — sent, awaiting payment"
    return "◻️ Invoice — not sent yet"


def _line_confirmation(status: str) -> str:
    if status == "sent":
        return "✅ Final confirmation — sent"
    return "◻️ Final confirmation — not sent yet"


def render_case(case: dict) -> str:
    """Full status ladder for one reservation, from get_case() output."""
    r = case.get("reservation") or {}
    gs = case.get("goal_state") or {}
    gaps = case.get("gaps") or []
    tour = gs.get("case_type") == "production_tour"

    who = r.get("client_name") or "Unknown"
    rid = r.get("reservation_id") or ""
    header = f"*{who} — {rid}*" if rid else f"*{who}*"
    facts = " · ".join(x for x in (
        _fmt_slot(r.get("requested_date"), r.get("requested_time")),
        f"{r.get('guest_count')} guests" if r.get("guest_count") else "",
        "Production tour + tasting" if tour else "Standard tasting",
    ) if x)

    parties = [
        _line_cecil(gs.get("cecil_status") or "", tour),
        _line_josh(gs.get("josh_availability") or ""),
        _line_customer(gs.get("customer_commitment") or ""),
        _line_invoice(gs.get("invoice") or ""),
        _line_confirmation(gs.get("confirmation") or ""),
    ]

    if case.get("goal_met"):
        closing = "*Done:* fully confirmed — nothing left on this one. 🎉"
    elif gaps:
        from vertex_agent.tools import _GAP_STAGE
        label = "Blocked" if gaps[0] == "ask_client_alternatives" else "Next"
        closing = f"*{label}:* {_GAP_STAGE.get(gaps[0], gaps[0])}"
    else:
        closing = "*Waiting on:* a reply we already requested."

    return "\n".join(filter(None, [header, facts, "", *parties, "", closing]))


# ── Entry point (called by the Google Chat adapter) ───────────────────────────

def try_status_reply(text: str) -> str | None:
    """Return a formatted status reply, or None to let the assistant handle it.

    Never raises — any failure logs a warning and falls back to the LLM path.
    """
    try:
        matched = match_status_query(text)
        if not matched:
            return None
        view, query = matched

        if view == "board":
            from vertex_agent.tools import open_cases_status
            return render_board(open_cases_status())

        from vertex_agent.chat_agent import find_cases
        rows = find_cases(query)
        if not rows:
            # Unknown name/id — the assistant can still resolve dates, emails,
            # fuzzier phrasings, so don't answer "not found" here.
            return None
        if len(rows) > 1:
            lines = [f"A few cases match *{query}* — which one?"]
            for r in rows[:8]:
                lines.append(f"• {r.get('client_name')} — {r.get('reservation_id')}"
                             f" ({_fmt_date(r.get('requested_date')) or 'no date'})")
            lines.append("_Say \"status of <case id>\" to pick one._")
            return "\n".join(lines)

        from vertex_agent.tools import get_case
        case = get_case(rows[0]["reservation_id"])
        if case.get("error"):
            return None
        return render_case(case)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("[tr:status] fast-path failed (%s) — falling back to the assistant", e)
        return None
