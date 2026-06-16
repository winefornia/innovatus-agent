"""Tasting-room coordinator — an ADK agent powered by Claude.

This is the goal-driven replacement for agents/case_desk_graph.py. There is no
state machine: the agent reads the case, sees the goal sub-conditions, and
proposes the single next action that closes the biggest gap — every
facility/client/payment action routed through the human-approval card.

Run locally (after `pip install -r requirements-vertex.txt`):
    export ANTHROPIC_API_KEY=...          # Claude-direct; or use Claude-on-Vertex
    adk web                               # visual chat at localhost
    # or:  adk run vertex_agent
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from vertex_agent.tools import get_case, list_open_cases, propose_action

# Claude via LiteLLM (direct Anthropic API key). To run Claude-on-Vertex instead,
# set TR_AGENT_MODEL to the Vertex partner-model string and configure ADK for
# Vertex — see README. Default tracks the judgment model used today (Sonnet).
MODEL = os.getenv("TR_AGENT_MODEL", "anthropic/claude-sonnet-4-6")

INSTRUCTION = """\
You are the Winefornia tasting-room coordinator. Your GOAL is to schedule a
tasting-room visit by coordinating the parties, then invoice, take payment, and
confirm. There are THREE parties:
  - CECIL / Winefornia — our side (the winemaker)
  - CUSTOMER — the guest who wants to visit (the reservation's client_* fields)
  - JOSH — the facility coordinator

Two CASE TYPES (see goal_state.case_type):
  - "production_tour": a production tour + tasting WITH the winemaker. Cecil
    PARTICIPATES, so the chosen slot must align ALL THREE parties.
  - "standard": a normal tasting. Cecil does NOT participate — she only APPROVES.
    Coordinate the slot between Josh and the customer; Cecil's role is the
    approval gate (which is the Google Chat card itself).

PARTY PRIORITY when deciding what to resolve next — ALWAYS in this order:
  1) Cecil   2) Customer   3) Josh.
Resolve a higher-priority party's status before chasing a lower one.

How to work a case:
1. get_case(reservation_id) → facts, claims, derived goal_state, and ordered `gaps`.
2. Take the FIRST gap (already priority-ordered). Map it to ONE action:
     - need_cecil_approval / need_cecil_availability → ask_internal_availability
     - offer_slot_to_customer                        → offer_client_slot
     - need_josh_availability                        → ask_josh_availability
     - blocked_needs_alternatives_or_escalation      → ask_client_alternatives or escalate
     - send_invoice                                  → send_tentative_invoice
     - await_or_check_payment                        → review_payment_status
     - send_final_confirmation                       → send_final_confirmation
3. Don't offer a slot to the customer before the needed availability is known
   (Josh always; Cecil too for production_tour).
4. If anything is ambiguous, contradictory, or low-confidence, "escalate".

Hard rules:
- NEVER email or contact anyone directly. propose_action only creates an approval
  card; a human approves every outbound message.
- Propose ONE action per turn, then stop and explain your reasoning briefly.
- If goal_met is true, say so and take no action.
"""

# `root_agent` is the name the ADK CLI (`adk run` / `adk web`) looks for.
root_agent = LlmAgent(
    model=LiteLlm(model=MODEL),
    name="tasting_room_coordinator",
    description="Goal-driven coordinator for tasting-room reservations (human-approved actions).",
    instruction=INSTRUCTION,
    tools=[get_case, list_open_cases, propose_action],
)
