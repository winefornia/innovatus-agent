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
    has_useful = bool(facts.get("client_email") or facts.get("requested_date") or facts.get("client_name"))
    if is_new and message_type == "unclassified" and not has_useful:
        try:
            repository.insert_unresolved_event(UnresolvedEvent(
                source_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id,
                subject=subject, from_email=sender, message_type=message_type,
                reason="Unclassified email with no useful facts and no matching reservation.",
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


def coordinate_email(*, subject: str, sender: str, body: str, to_email: str = "",
                     gmail_message_id: str = "", gmail_thread_id: str = "") -> dict:
    """Intake one email, then run the goal-oriented agent on the resolved case.

    Returns a result shaped like the legacy graph result so the mailbox can label
    and report uniformly.
    """
    info = intake_email(subject=subject, sender=sender, body=body, to_email=to_email,
                        gmail_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id)
    if info.get("unresolved") or not info.get("reservation_id"):
        return {"status": "unresolved", "message_type": info.get("message_type"),
                "reservation_id": None}

    import asyncio
    from google.adk.runners import InMemoryRunner
    from vertex_agent.agent import root_agent

    rid = info["reservation_id"]

    async def _run():
        runner = InMemoryRunner(agent=root_agent, app_name="tr-coordinate")
        return await runner.run_debug(
            f"Coordinate reservation {rid}. Decide and propose the single next step.",
            quiet=True,
        )

    events = asyncio.run(_run())

    proposed, summary = None, ""
    for e in events:
        c = getattr(e, "content", None)
        if not c:
            continue
        for p in (c.parts or []):
            fc = getattr(p, "function_call", None)
            if fc and fc.name == "propose_action":
                proposed = dict(fc.args)
            elif getattr(p, "text", None):
                summary = p.text
    return {
        "status": "coordinated",
        "reservation_id": rid,
        "message_type": info.get("message_type"),
        "experience_type": info.get("experience_type"),
        "proposed_action": proposed,
        "agent_summary": summary[:600],
    }
