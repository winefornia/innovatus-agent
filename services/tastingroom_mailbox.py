"""Gmail ingestion for tasting room reservation coordination."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import (
    GMAIL_TASTING_LABEL,
    GMAIL_TASTING_PROCESSED_LABEL,
    GMAIL_TASTING_QUERY,
    GMAIL_TASTING_ROOT_LABEL,
    GMAIL_TASTING_SOURCE_LABELS,
)


def list_candidate_messages(max_results: int = 10) -> list[dict[str, Any]]:
    from services.gmail_service import list_emails_multi, list_thread_messages

    intake = list_emails_multi(
        label_names=GMAIL_TASTING_SOURCE_LABELS,
        query=GMAIL_TASTING_QUERY,
        max_results=max_results,
    )
    candidates: dict[str, dict[str, Any]] = {}
    for msg in intake.get("messages", []):
        if _looks_like_tastingroom_message(msg):
            candidates[msg["message_id"]] = msg

    for thread_id in _active_reservation_thread_ids(limit=75):
        try:
            for msg in list_thread_messages(thread_id, max_results=20):
                candidates.setdefault(msg["message_id"], msg)
        except Exception as exc:
            logging.warning("[tastingroom mailbox] Failed to inspect thread %s: %s", thread_id, exc)

    return [msg for msg in candidates.values() if not _is_outbound_only(msg)][:max_results]


def _active_reservation_thread_ids(limit: int = 50) -> list[str]:
    """Return Gmail thread IDs from unresolved tasting-room cases.

    This is the continuity layer: once a thread is attached to a reservation,
    every later reply in that thread is eligible for processing even if Gmail
    labels or sender-specific search terms do not catch it.
    """
    try:
        from db.repository import list_recent_reservations
    except Exception as exc:
        logging.warning("[tastingroom mailbox] Reservation repo unavailable: %s", exc)
        return []

    terminal = {"FINAL_CONFIRMED", "CANCELLED_OR_DEFERRED"}
    thread_ids: list[str] = []
    for row in list_recent_reservations(limit=limit):
        if row.get("current_state") in terminal:
            continue
        for thread_id in row.get("gmail_thread_ids") or []:
            if _is_gmail_thread_id(thread_id) and thread_id not in thread_ids:
                thread_ids.append(thread_id)
    return thread_ids


def _is_gmail_thread_id(thread_id: str | None) -> bool:
    return bool(thread_id and re.fullmatch(r"[0-9a-f]{12,32}", str(thread_id)))


def _is_outbound_only(msg: dict[str, Any]) -> bool:
    labels = set(msg.get("labels") or [])
    return "SENT" in labels and "INBOX" not in labels


def _looks_like_tastingroom_message(msg: dict[str, Any]) -> bool:
    haystack = " ".join([
        msg.get("subject", ""),
        msg.get("from", ""),
        msg.get("to", ""),
    ]).lower()
    needles = (
        "form submission - wine tasting booking",
        "form-submission@squarespace.info",
        "availability check",
        "josh@thecavesatsodacanyon.com",
        "josh uran",
        "cecil.park@winefornia.com",
        "invoicing@messaging.squareup.com",
        "new invoice was created",
        "invoice was paid",
        "winery visit",
        "tasting request",
        "tasting availability",
        "innovatus tasting",
        "innovatuswine.com",
        "reservation",
    )
    return any(needle in haystack for needle in needles)


def message_already_processed(message_id: str, labels: list[str] | None = None) -> bool:
    if GMAIL_TASTING_PROCESSED_LABEL in (labels or []):
        return True
    try:
        from db.repository import list_reservation_events_by_source

        return bool(list_reservation_events_by_source(message_id, limit=1))
    except Exception as exc:
        logging.warning("[tastingroom mailbox] DB processed check failed for %s: %s", message_id, exc)
    return False


def _label_part(value: str | None) -> str:
    value = (value or "unknown").strip().replace("_", " ").replace("-", " ")
    value = " ".join(part.capitalize() for part in value.split())
    return value or "Unknown"


def labels_for_result(message_type: str | None, state: str | None) -> list[str]:
    labels = [
        GMAIL_TASTING_ROOT_LABEL,
        GMAIL_TASTING_PROCESSED_LABEL,
    ]
    state_value = state or ""
    type_value = message_type or ""
    if type_value == "squarespace_form":
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/New Requests")
    if type_value in {"josh_reply", "josh_availability_reply", "josh_booking_confirmation"}:
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/Facility")
    if state_value in {"NEEDS_INTERNAL_CHECK", "HUMAN_REVIEW_REQUIRED"}:
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/Needs Review")
    if state_value in {"WAITING_FOR_JOSH", "NEEDS_FACILITY_CHECK"}:
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/Awaiting Reply")
    if state_value in {"WAITING_FOR_CLIENT_REPLY", "SLOT_OFFERED_TO_CLIENT"}:
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/Awaiting Reply")
    if state_value in {"READY_TO_OFFER_CLIENT", "CLIENT_ACCEPTED_SLOT"}:
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/Action Needed")
    if state_value in {"INVOICE_SENT", "WAITING_FOR_PAYMENT", "PAYMENT_RECEIVED"}:
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/Payment")
    if state_value == "FINAL_CONFIRMED":
        labels.append(f"{GMAIL_TASTING_ROOT_LABEL}/Confirmed")
    return list(dict.fromkeys(labels))


def process_gmail_message(message_id: str, *, labels: list[str] | None = None) -> dict[str, Any]:
    if message_already_processed(message_id, labels=labels):
        return {"message_id": message_id, "status": "skipped", "reason": "already_processed"}

    from services.gmail_service import apply_message_labels, read_email

    msg = read_email(message_id)
    body = msg.get("body", "")
    subject = msg.get("subject", "")
    sender = msg.get("from", "email")
    to_email = msg.get("to", "")
    thread = msg.get("thread_id", "")
    full_text = f"Subject: {subject}\nFrom: {sender}\nTo: {to_email}\n\n{body}".strip()
    if not full_text:
        return {"message_id": message_id, "status": "skipped", "reason": "empty_message"}

    thread_id = f"tasting_{thread or message_id[:12]}"

    # Goal-oriented Vertex ADK agent — the sole coordination engine (the legacy
    # LangGraph case_desk_graph was removed). Reuses the same Gmail/Chat/Supabase
    # endpoints; the agent decides the next step and routes it through the
    # human-approval card.
    from vertex_agent.intake import coordinate_email

    agent_result = coordinate_email(
        subject=subject, sender=sender, body=body, to_email=to_email,
        gmail_message_id=message_id, gmail_thread_id=thread,
    )
    message_type = agent_result.get("message_type")
    reservation_id = agent_result.get("reservation_id")
    current_state = None
    if reservation_id:
        from db.repository import get_reservation
        current_state = (get_reservation(reservation_id) or {}).get("current_state")
    proposed = agent_result.get("proposed_action") or {}
    result_meta = {
        "reservation_id": reservation_id,
        "action_id": None,
        "response": agent_result.get("agent_summary"),
        "proposed_action": proposed.get("action"),
        "engine": "agent",
    }

    applied_labels = labels_for_result(message_type, current_state)
    apply_message_labels(
        message_id,
        remove_labels=[GMAIL_TASTING_LABEL, f"{GMAIL_TASTING_ROOT_LABEL}/Inbox"],
        add_labels=applied_labels,
    )

    return {
        "message_id": message_id,
        "status": "processed",
        "subject": subject,
        "thread_id": thread_id,
        "message_type": message_type,
        "state": current_state,
        "labels": applied_labels,
        **result_meta,
    }


def poll_once(max_results: int = 10) -> dict[str, Any]:
    processed = []
    for msg_meta in list_candidate_messages(max_results=max_results):
        mid = msg_meta["message_id"]
        try:
            processed.append(process_gmail_message(mid, labels=msg_meta.get("labels") or []))
        except Exception as exc:
            logging.exception("[tastingroom mailbox] Failed to process %s", mid)
            processed.append({"message_id": mid, "status": "error", "error": str(exc)})
    return {
        "processed": processed,
        "count": len(processed),
        "source_labels": GMAIL_TASTING_SOURCE_LABELS,
        "processed_label": GMAIL_TASTING_PROCESSED_LABEL,
        "query": GMAIL_TASTING_QUERY,
    }
