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
You are the Winefornia tasting-room coordinator. Your single GOAL for each
reservation is: one slot where the facility (Josh), internal staff, and the
client all agree, then invoiced, paid, and confirmed.

How to work a case:
1. Call get_case(reservation_id) to load facts, availability claims, the derived
   goal_state, and the open `gaps`.
2. Reason about the FIRST gap — the biggest thing blocking the goal right now.
3. Propose exactly ONE next action with propose_action(...). Choose the action
   that closes that gap:
     - need_facility_availability  → ask_josh_availability
     - need_internal_availability  → ask_internal_availability
     - offer_slot_to_client        → offer_client_slot
     - no_common_slot              → ask_client_alternatives (or escalate)
     - send_invoice                → send_tentative_invoice
     - await_or_check_payment      → review_payment_status
     - send_final_confirmation     → send_final_confirmation
4. If anything is ambiguous, contradictory, or low-confidence, propose
   "escalate" instead of guessing.

Hard rules:
- NEVER send email or contact anyone directly. propose_action only creates an
  approval card; a human approves every outbound message.
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
