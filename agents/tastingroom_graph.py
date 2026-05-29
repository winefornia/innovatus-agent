"""Tasting Room Agent — email-native reservation coordination.

This graph does not schedule from a calendar. It turns inbound emails into
reservation state, source-backed availability claims, draft replies, and
Telegram-gated action requests.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import POSTGRES_CONNECTION_STRING


class TastingRoomState(TypedDict, total=False):
    raw_email: str
    sender_id: str
    subject: str
    from_email: str
    to_email: str
    body: str
    gmail_message_id: str
    gmail_thread_id: str

    message_type: str
    extracted_facts: dict[str, Any]
    reservation_id: str
    current_state: str
    recommended_action: str
    claims_count: int
    action_id: str
    confidence: float
    final_response: str
    error: str
    disable_actions: bool
    _reservation: dict[str, Any]
    _claims: list[dict[str, Any]]


def classify_and_extract(state: TastingRoomState) -> TastingRoomState:
    from services.tastingroom_service import classify_email, extract_email_facts, merge_llm_facts, llm_extract_email

    subject = state.get("subject", "")
    sender = state.get("from_email") or state.get("sender_id", "")
    body = state.get("body") or state.get("raw_email", "")
    message_type = classify_email(subject, sender, body)
    facts = extract_email_facts(subject, sender, body, message_type)
    llm_facts = llm_extract_email(subject, sender, body, message_type)
    facts = merge_llm_facts(facts, llm_facts, message_type)
    facts = {**facts, "message_type": message_type, "sender_email": facts.get("sender_email")}
    return {"message_type": message_type, "extracted_facts": facts}


def match_and_update_case(state: TastingRoomState) -> TastingRoomState:
    from services.tastingroom_service import (
        apply_state,
        build_claims,
        find_or_create_reservation,
        merge_reservation,
    )
    from db.models import AvailabilityClaim, Reservation

    facts = state.get("extracted_facts", {})
    rid, existing = find_or_create_reservation(
        gmail_thread_id=state.get("gmail_thread_id", ""),
        subject=state.get("subject", ""),
        facts=facts,
    )
    reservation = merge_reservation(existing, rid, facts, state.get("gmail_thread_id", ""))
    claims = build_claims(
        reservation,
        facts,
        state.get("message_type", "unclassified"),
        state.get("body") or state.get("raw_email", ""),
        state.get("gmail_message_id", ""),
    )
    reservation = apply_state(reservation, state.get("message_type", "unclassified"), claims, facts)
    return {
        "reservation_id": reservation.reservation_id,
        "current_state": reservation.current_state,
        "recommended_action": reservation.recommended_action or "",
        "claims_count": len(claims),
        "_reservation": asdict(reservation),  # type: ignore[typeddict-unknown-key]
        "_claims": [asdict(claim) for claim in claims],  # type: ignore[typeddict-unknown-key]
    }


def persist_case_event(state: TastingRoomState) -> TastingRoomState:
    from services.tastingroom_service import persist_processed_email
    from db.models import AvailabilityClaim, Reservation

    reservation_data = state.get("_reservation")  # type: ignore[typeddict-item]
    claims_data = state.get("_claims", [])        # type: ignore[typeddict-item]
    if not reservation_data:
        return {"error": "No reservation produced."}
    reservation = Reservation(**reservation_data)
    claims = [AvailabilityClaim(**claim) for claim in claims_data]

    persist_processed_email(
        reservation=reservation,
        message_type=state.get("message_type", "unclassified"),
        facts=state.get("extracted_facts", {}),
        claims=claims,
        source_message_id=state.get("gmail_message_id", ""),
        raw_payload={
            "subject": state.get("subject"),
            "from": state.get("from_email"),
            "to": state.get("to_email"),
            "gmail_thread_id": state.get("gmail_thread_id"),
            "facts": state.get("extracted_facts", {}),
        },
    )
    return {}


def plan_case_action(state: TastingRoomState) -> TastingRoomState:
    from services.tastingroom_service import plan_next_action_from_timeline
    from db.models import Reservation

    reservation_data = state.get("_reservation")  # type: ignore[typeddict-item]
    if not reservation_data:
        return {}
    reservation = Reservation(**reservation_data)
    planned = plan_next_action_from_timeline(
        reservation,
        message_type=state.get("message_type", "unclassified"),
        fallback_action=state.get("recommended_action", ""),
    )
    if not planned:
        return {}
    reservation.recommended_action = planned.get("recommended_action") or reservation.recommended_action
    return {
        "recommended_action": reservation.recommended_action or "",
        "_reservation": asdict(reservation),  # type: ignore[typeddict-unknown-key]
    }


def create_human_approval(state: TastingRoomState) -> TastingRoomState:
    from services.tastingroom_service import create_action_request
    from db.models import Reservation

    reservation_data = state.get("_reservation")  # type: ignore[typeddict-item]
    action = state.get("recommended_action", "")
    reservation = Reservation(**reservation_data) if reservation_data else None
    if state.get("disable_actions"):
        msg = (
            f"Reservation {state.get('reservation_id')} moved to {state.get('current_state')}.\n"
            f"Recommended action: {action or 'none'}.\n"
            "Action creation disabled for this run."
        )
        return {"final_response": msg}
    if not reservation or not action or action in {"close_case"}:
        msg = (
            f"Reservation {state.get('reservation_id')} moved to {state.get('current_state')}.\n"
            f"Recommended action: {action or 'none'}."
        )
        return {"final_response": msg}

    action_id = create_action_request(
        reservation,
        action,
        source_message_id=state.get("gmail_message_id", ""),
    )
    msg = (
        f"Reservation {reservation.reservation_id} updated.\n"
        f"State: {reservation.current_state}\n"
        f"Recommended action: {action}\n"
        f"Claims stored: {state.get('claims_count', 0)}\n"
        f"Approval request: {action_id or 'not required'}"
    )
    return {"action_id": action_id or "", "final_response": msg}


def build_tastingroom_graph(checkpointer=None):
    g = StateGraph(TastingRoomState)
    g.add_node("classify_and_extract", classify_and_extract)
    g.add_node("match_and_update_case", match_and_update_case)
    g.add_node("persist_case_event", persist_case_event)
    g.add_node("plan_case_action", plan_case_action)
    g.add_node("create_human_approval", create_human_approval)

    g.add_edge(START, "classify_and_extract")
    g.add_edge("classify_and_extract", "match_and_update_case")
    g.add_edge("match_and_update_case", "persist_case_event")
    g.add_edge("persist_case_event", "plan_case_action")
    g.add_edge("plan_case_action", "create_human_approval")
    g.add_edge("create_human_approval", END)
    return g.compile(checkpointer=checkpointer)


def _make_checkpointer():
    if not POSTGRES_CONNECTION_STRING:
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    try:
        from agents.invoice_graph import checkpointer
        return checkpointer
    except Exception:
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


tastingroom_graph = build_tastingroom_graph(checkpointer=_make_checkpointer())
