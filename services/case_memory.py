"""Case memory — assembles a full CaseBundle from DB for LLM judgment."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from db.models import Reservation


def build_case_bundle(
    reservation: Reservation,
    claims: list[dict],
    events: list[dict],
) -> dict[str, Any]:
    """Assemble everything the judge needs to reason about a case.

    Args:
        reservation: the Reservation dataclass (already persisted)
        claims: list of availability_claims dicts from DB
        events: list of reservation_events dicts from DB (unused — pulled fresh)

    Returns:
        A plain dict — serialisable, loggable, inspectable.
    """
    from db import repository

    # Pull fresh state from DB so the bundle is always current.
    db_claims = repository.list_availability_claims(reservation.reservation_id, limit=50)
    db_events = repository.list_reservation_events(reservation.reservation_id, limit=40)

    # Merge in-memory claims (just built) with anything already in DB.
    seen_ids: set[str] = {c.get("source_message_id", "") for c in db_claims}
    for c in claims:
        mid = c.get("source_message_id", "")
        if mid not in seen_ids:
            db_claims.append(c)

    # Pending (unapproved) actions for this reservation.
    pending_actions: list[dict] = []
    try:
        rows = (
            repository._get_client()
            .table("reservation_action_requests")
            .select("*")
            .eq("reservation_id", reservation.reservation_id)
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        pending_actions = rows.data or []
    except Exception:
        pass

    # Include the previous judgment so the LLM can detect trend and avoid drift.
    latest_judgment: dict = {}
    try:
        latest_judgment = repository.get_latest_case_judgment(reservation.reservation_id) or {}
    except Exception:
        pass

    return {
        "case_id": reservation.reservation_id,
        "client": {
            "name": reservation.client_name,
            "email": reservation.client_email,
            "phone": reservation.phone,
            "guest_count": reservation.guest_count,
        },
        "reservation_facts": {
            "requested_date": reservation.requested_date,
            "requested_time": reservation.requested_time,
            "active_slot": reservation.active_slot,
            "candidate_slots": reservation.candidate_slots,
            "experience_type": reservation.experience_type,
            "price_per_person_cents": reservation.price_per_person_cents,
        },
        "current_state": reservation.current_state,
        "recommended_action": reservation.recommended_action,
        "payment_status": reservation.payment_status,
        "booking_status": reservation.booking_status,
        "messages": db_events,
        "claims": db_claims,
        "pending_actions": pending_actions,
        "latest_judgment": latest_judgment,
    }
