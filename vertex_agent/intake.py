"""Email intake for the goal-oriented coordinator — no LangGraph.

Rebuilds the legacy graph's intake (store_raw_event → extract_claims →
resolve_case → persist_claims) as plain functions that reuse the SAME
services.tastingroom_service helpers the graph nodes called. The watcher feeds
each inbound form/reply email here; `coordinate_email` then runs the agent.

Also fixes the root cause the legacy pipeline had: the Squarespace experience
selection (Tasting vs Production Tour + Winemaker) was never persisted. We detect
it from the form body and store it on the reservation so case_type is reliable.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def intake_email(*, subject: str, sender: str, body: str, to_email: str = "",
                 gmail_message_id: str = "", gmail_thread_id: str = "") -> dict:
    """Classify → extract facts → resolve/create reservation → persist.

    Returns {unresolved, reservation_id, message_type, experience_type}. Faithful
    to the graph nodes; reuses tastingroom_service so behavior matches the legacy
    intake exactly.
    """
    from db import repository
    from db.models import RawEmailEvent, UnresolvedEvent
    from services.tastingroom_service import (
        build_claims,
        classify_email,
        extract_email_facts,
        find_or_create_reservation,
        llm_extract_email,
        merge_llm_facts,
        merge_reservation,
        persist_processed_email,
    )
    from vertex_agent.goal_model import classify_case_type, PRODUCTION_TOUR

    # 1) store raw event (idempotent, best-effort)
    if gmail_message_id:
        try:
            repository.insert_raw_email_event(RawEmailEvent(
                gmail_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id,
                subject=subject, from_email=sender, to_email=to_email, body=body,
                raw_payload={"subject": subject, "from": sender, "to": to_email,
                             "thread_id": gmail_thread_id},
            ))
        except Exception as exc:
            log.debug("[tr:intake] raw event insert best-effort failed: %s", exc)

    # 2) classify + extract facts
    message_type = classify_email(subject, sender, body)
    facts = extract_email_facts(subject, sender, body, message_type)
    facts = merge_llm_facts(facts, llm_extract_email(subject, sender, body, message_type), message_type)
    facts = {**facts, "message_type": message_type}

    # 3) resolve or create the reservation
    rid, existing = find_or_create_reservation(
        gmail_thread_id=gmail_thread_id, subject=subject, facts=facts,
    )
    is_new = existing is None
    # ONLY a genuine website form submission that names a client may CREATE a new
    # case. Everything else — Josh/client replies, Square reports, marketing blasts
    # — must attach to an EXISTING case (by thread or context). If it matches none,
    # quarantine it for human review instead of minting a nameless TASTING-…-UNKNOWN
    # case. (A bare parsed date is NOT enough: Square's "Sales Summary for June 12"
    # used to become a reservation.)
    form_with_identity = message_type == "squarespace_form" and bool(
        facts.get("client_name") or facts.get("client_email")
    )
    if is_new and not form_with_identity:
        if message_type == "squarespace_form":
            reason = "Website form submission with no client name or email — needs a human look."
        else:
            reason = (f"'{message_type}' email matched no existing reservation; only website form "
                      f"submissions open a new case, so this is quarantined for review.")
        try:
            repository.insert_unresolved_event(UnresolvedEvent(
                source_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id,
                subject=subject, from_email=sender, message_type=message_type,
                reason=reason,
                raw_payload={"subject": subject, "from": sender, "facts": facts},
            ))
        except Exception:
            pass
        return {"unresolved": True, "reservation_id": None, "message_type": message_type}

    reservation = merge_reservation(existing, rid, facts, gmail_thread_id)

    # 3b) ROOT-CAUSE FIX — persist the experience selection if intake didn't capture it.
    if not (reservation.experience_type or "").strip():
        if classify_case_type({}, source_text=f"{subject}\n{body}") == PRODUCTION_TOUR:
            reservation.experience_type = "Production Tour and Tasting with Winemaker"
        elif "tasting" in f"{subject}\n{body}".lower():
            reservation.experience_type = "Tasting"

    # 4) persist reservation + events + claims
    claims = build_claims(reservation, facts, message_type, body, gmail_message_id)
    persist_processed_email(
        reservation=reservation, message_type=message_type, facts=facts, claims=claims,
        source_message_id=gmail_message_id,
        raw_payload={"subject": subject, "from": sender, "to": to_email,
                     "gmail_thread_id": gmail_thread_id, "facts": facts},
    )
    return {"unresolved": False, "reservation_id": reservation.reservation_id,
            "message_type": message_type, "experience_type": reservation.experience_type}


_AGENT_TIMEOUT = float(os.getenv("TR_AGENT_TIMEOUT", "120"))


def _record_intake_failure(gmail_message_id: str, gmail_thread_id: str,
                           subject: str, sender: str, err: str) -> None:
    """Persist a failed-intake email as an unresolved event so the watcher does
    not reprocess it forever and a human can see it."""
    try:
        from db import repository
        from db.models import UnresolvedEvent
        repository.insert_unresolved_event(UnresolvedEvent(
            source_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id,
            subject=subject, from_email=sender, message_type="error",
            reason=f"intake failed: {err}"[:500],
            raw_payload={"subject": subject, "from": sender},
        ))
    except Exception:
        pass


# Deterministic gap → action map. goal_model.gaps() decides the next step; we map
# it to a SAFE_ACTION and post that approval card. NO LLM is in this decision — the
# email TEXT is still LLM-drafted inside create_action_request, but WHAT to do next
# (and every state change) is a pure function, gated by a human button tap.
_GAP_TO_ACTION = {
    "ask_client_alternatives":      "ask_client_alternatives",
    "need_winefornia_availability": "ask_internal_availability",
    "need_cecil_approval":          "ask_internal_availability",
    "need_cecil_availability":      "ask_internal_availability",
    "need_josh_availability":       "ask_josh_availability",
    "offer_slot_to_client":         "offer_client_slot",
    "offer_slot_to_customer":       "offer_client_slot",
    "send_invoice":                 "send_tentative_invoice",
    "await_or_check_payment":       "review_payment_status",
    "send_final_confirmation":      "send_final_confirmation",
}


def coordinate_reservation(reservation_id: str) -> dict:
    """DETERMINISTIC coordinator: derive the next gap from the goal model and post
    the approval card for it. No LLM decides here. Skips when the goal is met, when
    we're waiting on a reply already requested (no gap), or when a card of that type
    is already pending (no duplicates). Never raises."""
    try:
        import dataclasses
        from db.models import Reservation
        from db.repository import get_reservation, list_availability_claims
        from vertex_agent.goal_model import derive_goal_state

        row = get_reservation(reservation_id)
        if not row:
            return {"status": "error", "reservation_id": reservation_id, "error": "no such reservation"}
        if (row.get("current_state") or "") in ("FINAL_CONFIRMED", "CANCELLED_OR_DEFERRED"):
            return {"status": "terminal", "reservation_id": reservation_id, "proposed_action": None}
        gs = derive_goal_state(row, list_availability_claims(reservation_id))
        if gs.is_goal_met():
            return {"status": "goal_met", "reservation_id": reservation_id, "proposed_action": None}
        gaps = gs.gaps()
        action = _GAP_TO_ACTION.get(gaps[0]) if gaps else None
        if not action:
            return {"status": "waiting", "reservation_id": reservation_id, "proposed_action": None}

        from services.tastingroom_chat_service import _latest_pending_action
        existing = _latest_pending_action(reservation_id, preferred_type=action)
        if existing and existing.get("action_type") == action and existing.get("status") == "pending":
            return {"status": "already_pending", "reservation_id": reservation_id,
                    "proposed_action": {"action": action}}

        fields = {f.name for f in dataclasses.fields(Reservation)}
        reservation = Reservation(**{k: v for k, v in row.items() if k in fields})
        from services.tastingroom_service import create_action_request
        action_id = create_action_request(reservation, action)
        return {"status": "coordinated", "reservation_id": reservation_id,
                "proposed_action": {"action": action, "action_id": action_id}}
    except Exception as e:
        log.error("[tr:coordinate] coordinate failed for %s: %s", reservation_id, e, exc_info=True)
        return {"status": "error", "reservation_id": reservation_id, "error": str(e)}


def coordinate_email(*, subject: str, sender: str, body: str, to_email: str = "",
                     gmail_message_id: str = "", gmail_thread_id: str = "") -> dict:
    """Intake one email, then deterministically coordinate the resolved case.

    HARDENED: never raises. A failure is recorded and the email is still marked
    processed by the caller, so one bad email can neither crash the watcher nor be
    retried forever.
    """
    try:
        info = intake_email(subject=subject, sender=sender, body=body, to_email=to_email,
                            gmail_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id)
    except Exception as e:
        log.error("[tr:coordinate] intake failed for %s: %s", gmail_message_id, e, exc_info=True)
        _record_intake_failure(gmail_message_id, gmail_thread_id, subject, sender, str(e))
        return {"status": "intake_error", "reservation_id": None,
                "message_type": "error", "error": str(e)}

    if info.get("unresolved") or not info.get("reservation_id"):
        return {"status": "unresolved", "message_type": info.get("message_type"),
                "reservation_id": None}

    rid = info["reservation_id"]
    result = coordinate_reservation(rid)
    result["message_type"] = info.get("message_type")
    result["experience_type"] = info.get("experience_type")
    return result
