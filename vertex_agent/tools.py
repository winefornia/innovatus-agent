"""ADK tools for the tasting-room coordinator agent.

Each function is a tool the agent can call. They wrap the EXISTING repository and
service code — no business logic is reimplemented here — so the agent reads the
same canonical state and routes actions through the same human-approval card the
current pipeline uses. The agent never sends email directly: it can only
`propose_action`, which creates a ReservationActionRequest and posts the Google
Chat approval card. A human still taps the button.
"""

from __future__ import annotations

import dataclasses
import logging

from db.models import Reservation
from db.repository import (
    get_reservation,
    list_availability_claims,
    list_recent_reservations,
    list_reservation_events,
)
from vertex_agent.goal_model import derive_goal_state

log = logging.getLogger(__name__)

# Actions the agent is allowed to propose (mirrors the safe-action set the
# current pipeline + cards already understand).
ALLOWED_ACTIONS = {
    "ask_josh_availability",
    "ask_internal_availability",
    "offer_client_slot",
    "ask_client_alternatives",
    "send_tentative_invoice",
    "review_payment_status",
    "send_final_confirmation",
    "escalate",
}


def get_case(reservation_id: str) -> dict:
    """Return the full case for one reservation: facts, availability claims,
    recent events, the derived goal state, and the open gaps toward the goal.

    Args:
        reservation_id: the reservation to load.
    """
    reservation = get_reservation(reservation_id)
    if not reservation:
        return {"error": f"No reservation {reservation_id}"}
    claims = list_availability_claims(reservation_id)
    events = list_reservation_events(reservation_id, limit=30)
    gs = derive_goal_state(reservation, claims)
    return {
        "reservation": reservation,
        "claims": claims,
        "events": events,
        "goal_state": dataclasses.asdict(gs),
        "gaps": gs.gaps(),
        "goal_met": gs.is_goal_met(),
    }


def list_open_cases() -> list[dict]:
    """List recent reservations that have NOT yet reached the coordination goal,
    each with its open gaps — so the agent can pick what to work on next."""
    out: list[dict] = []
    for r in list_recent_reservations(limit=25):
        claims = list_availability_claims(r["reservation_id"])
        gs = derive_goal_state(r, claims)
        if not gs.is_goal_met():
            out.append({
                "reservation_id": r["reservation_id"],
                "client_name": r.get("client_name"),
                "requested_date": r.get("requested_date"),
                "goal_state": dataclasses.asdict(gs),
                "gaps": gs.gaps(),
            })
    return out


def propose_action(reservation_id: str, action: str, rationale: str) -> dict:
    """Propose the next coordination action. This DOES NOT send anything — it
    creates an approval request and posts the Google Chat card for a human to
    approve or reject. Use this for every facility/client/payment-facing step.

    Args:
        reservation_id: the reservation to act on.
        action: one of the allowed action types (e.g. "ask_josh_availability",
            "offer_client_slot", "send_tentative_invoice", "send_final_confirmation",
            "escalate").
        rationale: one sentence on why this is the next-best step toward the goal.
    """
    if action not in ALLOWED_ACTIONS:
        return {"ok": False, "error": f"action must be one of {sorted(ALLOWED_ACTIONS)}"}
    row = get_reservation(reservation_id)
    if not row:
        return {"ok": False, "error": f"No reservation {reservation_id}"}

    # Reconstruct the Reservation dataclass from the row (filter to known fields).
    fields = {f.name for f in dataclasses.fields(Reservation)}
    reservation = Reservation(**{k: v for k, v in row.items() if k in fields})

    # Reuse the existing approval path: drafts the email, persists the request,
    # and posts the Google Chat approval card (see services.tastingroom_service).
    from services.tastingroom_service import create_action_request
    action_id = create_action_request(reservation, action)
    log.info("[tr:agent] proposed %s for %s → action_id=%s (%s)",
             action, reservation_id, action_id, rationale)
    if not action_id:
        return {"ok": False, "error": f"action '{action}' produced no approval request"}
    return {"ok": True, "action_id": action_id,
            "note": "Approval card posted to Google Chat; awaiting a human decision."}
