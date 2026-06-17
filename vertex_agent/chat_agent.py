"""Conversational assistant for the tasting-room Google Chat space.

Staff can type freely — "what's the status of test?", "why is Mira waiting?",
"what's blocking the June 24 tour?" — and get a context-aware answer. It reads the
real case (parties, goal state, gaps, history) and explains.

It can also ACT on a case on request — send/revise the drafted email, mark
payment, hand a case to a human, end/remove a case, revoke a decision — by
routing through the SAME service primitives the approval cards use (see
vertex_agent.chat_actions). Anything that touches the outside world (sending an
email, ending/removing a case, revoking) is CONFIRM-FIRST: the assistant stages
it and only acts on the user's affirming reply. Only allow-listed approvers
(Cecil/Lisa) ever reach this layer — the Google Chat adapter gates it upstream.
"""

from __future__ import annotations

import os

from vertex_agent.chat_actions import (
    WRITE_TOOLS,
    peek_pending,
    set_current_user,
)
from vertex_agent.tools import get_case, list_open_cases, open_cases_status


def find_cases(query: str) -> list[dict]:
    """Find reservations by client name or reservation-id substring.

    Use this to turn a name like "test" or "Mira" into a reservation_id, then call
    get_case() for detail.

    Args:
        query: a client name or part of a reservation id.
    """
    from db.repository import _get_client
    c = _get_client()
    q = (query or "").strip()
    if not q:
        return []
    rows = (c.table("reservations")
            .select("reservation_id,client_name,client_email,current_state,requested_date")
            .ilike("client_name", f"%{q}%").limit(8).execute().data) or []
    if not rows:
        rows = (c.table("reservations")
                .select("reservation_id,client_name,client_email,current_state,requested_date")
                .ilike("reservation_id", f"%{q.upper()}%").limit(8).execute().data) or []
    return [r for r in rows if not str(r.get("reservation_id", "")).startswith("TASTING-SMOKE-")]


_CHAT_INSTRUCTION = """\
You are the Winefornia tasting-room assistant in Google Chat. Staff type questions;
you answer like a sharp colleague who knows every case — warm, plain-spoken, brief.

Tools:
- open_cases_status() — the STATUS board: every open case by client name + case id,
  who's confirmed, and what each is waiting on. Use for "status" / "what's open" /
  "where are things" questions.
- find_cases(name) — resolve a name ("test", "Mira") to a reservation_id.
- get_case(reservation_id) — full detail: the three parties (client, Winefornia,
  Josh), the goal_state, the open `gaps`, claims, and event history.
- list_open_cases() — raw open-case list with goal_state.

Answer what's asked: current status, who we're waiting on, what's blocking, the
next step, what's been done. Quote concrete facts (dates, names, states).

HOW TO TALK — this is a chat message, not a report. Write like you'd text a coworker:
- Lead with a one-line takeaway, then the details. Sound human, not like a database dump.
- Group naturally and keep it scannable, but DON'T over-format. A short answer is a
  sentence or two — no headers, no bullets needed.

FORMATTING — Google Chat does NOT render Markdown tables, "###" headings, or "**" bold.
Those show up as literal junk characters. Use ONLY Google Chat's syntax:
- *bold* uses SINGLE asterisks (never **double**)
- _italic_ with underscores
- bullet lines start with "• " or "- "
- NEVER use tables (no "|" columns), NEVER use "#"/"###" headings, NEVER use "**".
For a multi-case rundown, use short bolded group labels followed by simple bullet
lines — e.g.  *Ready to send (3)*  then a "• Name — case — what's left" bullet each.
Use emoji sparingly (one per group label at most), not on every line.

ACTING ON A CASE — you can also DO things when asked, via these tools:
- stage_send_email(case) — send the email currently drafted for a case (Josh
  request, client offer, invoice note, final confirmation, etc.).
- revise_draft(case, instruction) — edit the not-yet-sent draft (e.g. "warmer",
  "mention parking"); shows the new version, doesn't send.
- mark_invoice_sent(case) / mark_paid(case) — record payment progress.
- manual_handle(case) — hand a case to a human; stops auto-advancing it.
- stage_cancel_case(case) — end/remove a case (soft-cancel; reversible).
- stage_revoke_decision(case) — revoke the last decision and reopen a case.

CONFIRM-FIRST PROTOCOL (critical):
- stage_send_email, stage_cancel_case, and stage_revoke_decision DO NOT act — they
  return a one-line "reply yes to confirm" question. After calling one, STOP and
  show that question. Do NOT call confirm_pending_action() in the same turn.
- When the user's NEXT message confirms ("yes", "send it", "go ahead"), call
  confirm_pending_action(). When they decline ("no", "never mind"), call
  cancel_pending_action(). If a "[pending confirmation]" note is present below,
  the user is replying to exactly that staged action.
- revise_draft, mark_invoice_sent, mark_paid, and manual_handle are reversible —
  just do them and report back; no confirmation needed.
- Only act when the user clearly asks you to. When they're just asking a
  question, answer it. Always say which case (name + id) you acted on."""

_chat_agent = None


def _get_chat_agent():
    """Build the ADK assistant lazily so this module imports without google-adk."""
    global _chat_agent
    if _chat_agent is None:
        from google.adk.agents import LlmAgent
        from google.adk.models.lite_llm import LiteLlm
        _chat_agent = LlmAgent(
            model=LiteLlm(model=os.getenv("TR_AGENT_MODEL", "anthropic/claude-sonnet-4-6")),
            name="tasting_room_assistant",
            description="Conversational, read-only assistant for tasting-room cases.",
            instruction=_CHAT_INSTRUCTION,
            tools=[open_cases_status, find_cases, get_case, list_open_cases, *WRITE_TOOLS],
        )
    return _chat_agent


def discuss(text: str, *, user: str = "") -> str:
    """Run the assistant on a staff message; return its text answer. Never raises."""
    try:
        import asyncio
        from google.adk.runners import InMemoryRunner

        # Identify the acting approver for this turn (audit trail + keys the
        # per-user confirm store). Each turn is a fresh, memory-less agent, so if
        # the user has a staged confirm-first action, re-inject it into the prompt
        # so "yes" resolves to the right thing.
        set_current_user(user)
        prompt = text
        pending = peek_pending(user)
        if pending:
            prompt = (
                f"[pending confirmation] The user has a staged action awaiting their yes/no: "
                f"\"{pending['summary']}\". Their message below is their reply to it.\n\n{text}"
            )

        async def _run():
            return await asyncio.wait_for(
                InMemoryRunner(agent=_get_chat_agent(), app_name="tr-chat").run_debug(prompt, quiet=True),
                timeout=float(os.getenv("TR_AGENT_TIMEOUT", "120")),
            )

        events = asyncio.run(_run())
        out = ""
        for e in events:
            c = getattr(e, "content", None)
            if not c:
                continue
            for p in (c.parts or []):
                if getattr(p, "text", None):
                    out = p.text
        return out or "I couldn't find anything on that."
    except Exception as e:  # pragma: no cover - defensive
        return f"Sorry — I hit an error answering that: {e}"
