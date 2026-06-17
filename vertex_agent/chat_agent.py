"""Conversational assistant for the tasting-room Google Chat space.

Staff can type freely — "what's the status of test?", "why is Mira waiting?",
"what's blocking the June 24 tour?" — and get a context-aware answer. It reads the
real case (parties, goal state, gaps, history) and explains.

READ-ONLY by design: it answers and explains but never sends email or changes a
booking. State changes + outbound mail still flow through the approval cards, so
this conversational layer can't bypass the human-approval safety model.
"""

from __future__ import annotations

import os

from vertex_agent.tools import get_case, list_open_cases


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
    return rows


_CHAT_INSTRUCTION = """\
You are the Winefornia tasting-room assistant in Google Chat. Staff type questions;
answer concisely with REAL case context.

Tools:
- find_cases(name) — resolve a name ("test", "Mira") to a reservation_id.
- get_case(reservation_id) — full detail: the three parties (client, Winefornia,
  Josh), the goal_state, the open `gaps`, claims, and event history.
- list_open_cases() — overview of everything in flight.

Answer what's asked: current status, who we're waiting on, what's blocking, the
next step, what's been done. Quote concrete facts (dates, names, states).

You are READ-ONLY. You do NOT send emails, reschedule, mark paid, or approve. If
asked to DO something, explain that it happens via the approval cards (a human
taps to act) and describe what the next card/step will be. Be brief and specific."""

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
            tools=[find_cases, get_case, list_open_cases],
        )
    return _chat_agent


def discuss(text: str, *, user: str = "") -> str:
    """Run the assistant on a staff message; return its text answer. Never raises."""
    try:
        import asyncio
        from google.adk.runners import InMemoryRunner

        async def _run():
            return await asyncio.wait_for(
                InMemoryRunner(agent=_get_chat_agent(), app_name="tr-chat").run_debug(text, quiet=True),
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
