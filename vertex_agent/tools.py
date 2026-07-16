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
import os
import re

from db.models import Reservation
from db.repository import (
    get_reservation,
    list_availability_claims,
    list_raw_email_events_by_thread,
    list_raw_email_events_for_case,
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


def _gmail_message_link(gmail_message_id: str) -> str:
    """Deep link that opens one message in the watched winery mailbox.

    Gmail's web UI accepts the API's hex message id in the #all/ fragment. The
    authuser hint routes multi-account users to the right mailbox; without it
    Gmail falls back to the default signed-in account.
    """
    mailbox = os.getenv("GOOGLE_DELEGATED_USER_EMAIL", "")
    if mailbox:
        return f"https://mail.google.com/mail/?authuser={mailbox}#all/{gmail_message_id}"
    return f"https://mail.google.com/mail/#all/{gmail_message_id}"


def get_request_email(reservation_id: str) -> dict:
    """Return the stored source email(s) behind a case, each with a Gmail link.

    Use when staff ask for the link to the request, want to see the original
    mail, or want proof of what the client actually wrote. Each email carries
    its subject, sender, received time, a short excerpt, and a gmail_link that
    opens the message in the winery mailbox (the viewer must have access to
    that mailbox). The oldest email is the original booking request.

    Args:
        reservation_id: the reservation whose source mail to fetch.
    """
    reservation = get_reservation(reservation_id)
    if not reservation:
        return {"error": f"No reservation {reservation_id}"}

    # Emails linked through the case's audit events, then anything else stored
    # for the reservation's Gmail threads (replies land there before an event
    # references them).
    rows = list(list_raw_email_events_for_case(reservation_id))
    seen = {r.get("gmail_message_id") for r in rows}
    for thread_id in reservation.get("gmail_thread_ids") or []:
        if not re.fullmatch(r"[0-9a-f]{12,32}", str(thread_id or "")):
            continue  # synthetic/manual ids never resolve in Gmail
        for r in list_raw_email_events_by_thread(thread_id):
            if r.get("gmail_message_id") not in seen:
                seen.add(r.get("gmail_message_id"))
                rows.append(r)
    rows.sort(key=lambda r: r.get("ingested_at") or "")

    emails = []
    for i, r in enumerate(rows):
        excerpt = " ".join((r.get("body") or "").split())
        emails.append({
            "role": "original request" if i == 0 else "follow-up",
            "subject": r.get("subject") or "(no subject)",
            "from": r.get("from_email") or "",
            "received": r.get("ingested_at") or "",
            "excerpt": excerpt[:300],
            "gmail_link": _gmail_message_link(r["gmail_message_id"]),
        })

    result = {
        "reservation_id": reservation_id,
        "client_name": reservation.get("client_name"),
        "mailbox": os.getenv("GOOGLE_DELEGATED_USER_EMAIL", "") or "the winery mailbox",
        "emails": emails,
    }
    if not emails:
        result["note"] = ("No stored source email for this case — it may have been "
                          "opened manually or from chat rather than from a mail.")
    return result


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


# Below this confidence the action is forced to "escalate" (staff review),
# regardless of what the agent chose — ports services/safety_guards.py's <0.6 rule
# into the tool layer so it's deterministic, not LLM-trusted.
_MIN_CONFIDENCE = 0.6


_GAP_STAGE = {
    "ask_client_alternatives":      "Going back to the client for a new time",
    "need_winefornia_availability": "Checking Winefornia (our) availability",
    "need_cecil_approval":          "Awaiting Winefornia approval",
    "need_cecil_availability":      "Checking Winefornia availability",
    "need_josh_availability":       "Checking Josh (facility) availability",
    "offer_slot_to_client":         "Ready to offer the client the slot",
    "offer_slot_to_customer":       "Ready to offer the client the slot",
    "send_invoice":                 "Ready to send the invoice",
    "await_or_check_payment":       "Waiting on payment",
    "send_final_confirmation":      "Ready to confirm + send calendar invites",
}


def open_cases_status() -> list[dict]:
    """Status of every OPEN case (not yet confirmed, not cancelled): the client name
    and case id, who's confirmed so far, and what each one is waiting on right now.
    Use this for 'status' / 'what's open' / overview questions. Smoke-test cases are
    excluded.
    """
    out: list[dict] = []
    for r in list_recent_reservations(limit=40):
        rid = r["reservation_id"]
        if rid.startswith("TASTING-SMOKE-"):
            continue
        if (r.get("current_state") or "") in ("FINAL_CONFIRMED", "CANCELLED_OR_DEFERRED"):
            continue
        gs = derive_goal_state(r, list_availability_claims(rid))
        if gs.is_goal_met():
            continue
        gaps = gs.gaps()
        confirmed = []
        if gs.cecil_status == "ok":
            confirmed.append("Winefornia")
        if gs.josh_availability == "confirmed":
            confirmed.append("Josh")
        if gs.customer_commitment == "accepted":
            confirmed.append("Customer")
        out.append({
            "case": rid,
            "client_name": r.get("client_name") or "(no name yet)",
            "case_type": gs.case_type,
            "date": r.get("requested_date"),
            "confirmed": confirmed,
            # Raw first-gap key — the deterministic status renderer maps it to a
            # traffic-light level (services.tastingroom_status).
            "stage": gaps[0] if gaps else "awaiting_reply",
            "waiting_on": (_GAP_STAGE.get(gaps[0], gaps[0]) if gaps
                          else "A reply we already requested"),
        })
    return out


def propose_action(reservation_id: str, action: str, rationale: str, confidence: float = 1.0) -> dict:
    """Propose the next coordination action. This DOES NOT send anything — it
    creates an approval request and posts the Google Chat card for a human to
    approve or reject. Use this for every facility/client/payment-facing step.

    Args:
        reservation_id: the reservation to act on.
        action: one of the allowed action types (e.g. "ask_josh_availability",
            "offer_client_slot", "send_tentative_invoice", "send_final_confirmation",
            "escalate").
        rationale: one sentence on why this is the next-best step toward the goal.
        confidence: 0–1 confidence in this action. Below 0.6 the action is forced
            to "escalate" for staff review (hard safety rule, not a suggestion).
    """
    if action not in ALLOWED_ACTIONS:
        return {"ok": False, "error": f"action must be one of {sorted(ALLOWED_ACTIONS)}"}

    # Confidence guard — low-confidence actions become staff escalations.
    if confidence < _MIN_CONFIDENCE and action != "escalate":
        log.info("[tr:agent] confidence %.2f < %.2f — downgrading %s → escalate (%s)",
                 confidence, _MIN_CONFIDENCE, action, reservation_id)
        action = "escalate"
        rationale = f"[low confidence {confidence:.2f}] {rationale}"
    row = get_reservation(reservation_id)
    if not row:
        return {"ok": False, "error": f"No reservation {reservation_id}"}

    # Dry-run guard (TR_AGENT_DRY_RUN=1): used during validation so a test run does
    # NOT post a real approval card to the live Chat space or write a DB row.
    if os.getenv("TR_AGENT_DRY_RUN", "").lower() in ("1", "true", "yes"):
        log.info("[tr:agent:dry-run] would propose %s for %s — %s", action, reservation_id, rationale)
        return {"ok": True, "dry_run": True, "action": action,
                "note": f"DRY RUN — would post an approval card for '{action}' (no card sent)."}

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
