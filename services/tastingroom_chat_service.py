"""Natural-language command layer for the tasting room Telegram bot."""

from __future__ import annotations

import json
import re
from typing import Any


def handle_tastingroom_chat(text: str, *, chat_id: int | str) -> str:
    """Interpret a staff Telegram message and apply the requested operation.

    External email sends still go through the same action-request controls as
    inline buttons. This layer only makes the staff control surface easier.
    """
    command = _classify_command(text)
    intent = command.get("intent") or "help"

    if intent == "help":
        return _help_text()
    if intent == "list_pending":
        return _list_pending()
    if intent in {"status", "show_case"}:
        reservation = _find_reservation(command)
        if not reservation:
            return "I could not find a matching tasting room case."
        return _format_case(reservation)
    if intent in {"mark_invoice_sent", "mark_paid", "queue_final", "approve_action", "reject_action", "escalate_action"}:
        return _apply_action_decision(command, decided_by=f"tg_{chat_id}")
    if intent == "revise_pending_email":
        return _revise_pending_email(command, text)
    return _help_text()


def _classify_command(text: str) -> dict[str, Any]:
    lowered = text.lower().strip()
    deterministic = _deterministic_command(lowered)
    if deterministic:
        deterministic["raw_text"] = text
        return deterministic

    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=(
                "Classify this tasting-room staff command. Return only JSON. "
                "Valid intents: help, list_pending, status, show_case, mark_invoice_sent, "
                "mark_paid, queue_final, approve_action, reject_action, escalate_action, "
                "revise_pending_email. Fields: intent, reservation_query, action_type, "
                "action_id, revision_instruction. Do not choose approve_action unless the "
                "message explicitly asks to approve/send a pending action."
            )),
            HumanMessage(content=text),
        ])
        parsed = _parse_json_text(result.content)
        if isinstance(parsed, dict):
            parsed["raw_text"] = text
            return parsed
    except Exception:
        pass
    return {"intent": "help", "raw_text": text}


def _deterministic_command(lowered: str) -> dict[str, Any] | None:
    if lowered in {"/help", "help", "commands"}:
        return {"intent": "help"}
    if lowered in {"/status", "status", "pending", "what is pending", "show pending"}:
        return {"intent": "list_pending"}
    if lowered.startswith("/status "):
        return {"intent": "show_case", "reservation_query": lowered.split(" ", 1)[1].strip()}
    if lowered.startswith("show ") or lowered.startswith("case "):
        return {"intent": "show_case", "reservation_query": lowered.split(" ", 1)[1].strip()}
    if lowered.startswith("revise ") or lowered.startswith("edit "):
        return {"intent": "revise_pending_email", "revision_instruction": lowered}

    action_id = _extract_action_id(lowered)
    if action_id:
        if "reject" in lowered:
            return {"intent": "reject_action", "action_id": action_id}
        if "escalate" in lowered:
            return {"intent": "escalate_action", "action_id": action_id}
        if "approve" in lowered or "send" in lowered:
            return {"intent": "approve_action", "action_id": action_id}

    intent = None
    if re.search(r"\b(mark|set)\b.*\binvoice\b.*\b(sent|created)\b", lowered):
        intent = "mark_invoice_sent"
    elif re.search(r"\b(mark|set)\b.*\bpaid\b", lowered) or "payment received" in lowered:
        intent = "mark_paid"
    elif "queue final" in lowered or "final confirmation" in lowered:
        intent = "queue_final"
    if intent:
        return {"intent": intent, "reservation_query": _strip_action_words(lowered)}
    return None


def _strip_action_words(value: str) -> str:
    value = re.sub(r"\b(mark|set|invoice|sent|created|paid|payment|received|queue|final|confirmation|for|case)\b", " ", value)
    return " ".join(value.split())


def _extract_action_id(value: str) -> str | None:
    match = re.search(r"\b[a-f0-9]{24,40}\b", value)
    return match.group(0) if match else None


def _list_pending(limit: int = 8) -> str:
    from db.repository import _get_client

    client = _get_client()
    actions = (
        client.table("reservation_action_requests")
        .select("action_id,reservation_id,action_type,status,email_subject,recipient_email,created_at")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    if not actions:
        return "No pending tasting room actions."
    lines = ["Pending tasting room actions:"]
    for action in actions:
        lines.append(
            f"- {action.get('action_type')} | {action.get('reservation_id')} | "
            f"{action.get('recipient_email') or 'internal'} | action {action.get('action_id')}"
        )
    return "\n".join(lines)


def _find_reservation(command: dict[str, Any]) -> dict | None:
    from db.repository import _get_client, find_recent_reservations, get_reservation

    query = (command.get("reservation_query") or command.get("reservation_id") or "").strip()
    if command.get("action_id"):
        action = _get_action(command["action_id"])
        if action:
            return get_reservation(action["reservation_id"])
    if query.startswith("TASTING-"):
        return get_reservation(query)

    rows = find_recent_reservations(limit=50)
    if not query:
        return rows[0] if rows else None
    needle = query.lower()
    for row in rows:
        haystack = " ".join(str(row.get(k) or "") for k in ("reservation_id", "client_name", "client_email", "current_state")).lower()
        if needle in haystack:
            return row
    client = _get_client()
    result = (
        client.table("reservations")
        .select("*")
        .or_(f"client_name.ilike.%{query}%,client_email.ilike.%{query}%")
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def _format_case(row: dict) -> str:
    return (
        f"Case: {row.get('reservation_id')}\n"
        f"Client: {row.get('client_name') or 'Unknown'} <{row.get('client_email') or 'no email'}>\n"
        f"Guests: {row.get('guest_count') or 'Unknown'}\n"
        f"Slot: {row.get('requested_date') or 'no date'} {row.get('requested_time') or ''}\n"
        f"State: {row.get('current_state')}\n"
        f"Payment: {row.get('payment_status')}\n"
        f"Booking: {row.get('booking_status')}\n"
        f"Next: {row.get('recommended_action') or 'none'}"
    )


def _apply_action_decision(command: dict[str, Any], *, decided_by: str) -> str:
    from services.tastingroom_service import process_action_decision

    action_id = command.get("action_id")
    if not action_id:
        reservation = _find_reservation(command)
        if not reservation:
            return "I could not find the reservation to apply that action."
        action = _latest_pending_action(
            reservation["reservation_id"],
            preferred_type="review_payment_status" if command["intent"] in {"mark_invoice_sent", "mark_paid", "queue_final"} else None,
        )
        if not action:
            return f"No pending action found for {reservation['reservation_id']}."
        action_id = action["action_id"]

    decision = {
        "mark_invoice_sent": "invoice_sent",
        "mark_paid": "paid",
        "queue_final": "queue_final",
        "approve_action": "approve",
        "reject_action": "reject",
        "escalate_action": "escalate",
    }[command["intent"]]
    result = process_action_decision(action_id, decision, decided_by=decided_by)
    if not result.get("ok"):
        return f"Action failed: {result.get('error')}"
    response = f"Action {result.get('status')}.\nReservation: {result.get('reservation_id')}"
    if result.get("next_action_id"):
        response += f"\nNext action queued: {result['next_action_id']}"
    return response


def _revise_pending_email(command: dict[str, Any], raw_text: str) -> str:
    from db.repository import update_reservation_action

    reservation = _find_reservation(command)
    if not reservation:
        return "I could not find the reservation whose draft should be revised."
    action = _latest_pending_action(reservation["reservation_id"])
    if not action:
        return f"No pending action draft found for {reservation['reservation_id']}."
    if not action.get("recipient_email"):
        return "The latest pending action is internal-only, so there is no client/facility email draft to revise."

    revised = _revise_email_with_llm(action, reservation, raw_text)
    update_reservation_action(
        action["action_id"],
        email_subject=revised["subject"],
        email_body=revised["body"],
        recommendation=(
            f"Edited tasting room action\n\nCase: {reservation['reservation_id']}\n"
            f"Client: {reservation.get('client_name')} <{reservation.get('client_email')}>\n"
            f"Recommended action: {action.get('action_type')}\n"
            f"To: {action.get('recipient_email')}\n"
            f"Subject: {revised['subject']}\n\nDraft:\n{revised['body'][:1600]}"
        ),
    )
    return (
        f"Updated pending draft for {reservation['reservation_id']}.\n"
        f"Action: {action['action_id']}\n"
        f"Subject: {revised['subject']}\n\n"
        f"{revised['body'][:1200]}"
    )


def _revise_email_with_llm(action: dict, reservation: dict, instruction: str) -> dict[str, str]:
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0.2)
        result = llm.invoke([
            SystemMessage(content=(
                "Revise a tasting-room operational email draft. Keep the same intent, recipient, "
                "payment safety, and factual details. Do not invent invoice links, payment, final "
                "confirmation, discounts, or availability. Return only JSON with subject and body."
            )),
            HumanMessage(content=(
                f"Instruction: {instruction}\n"
                f"Reservation: {json.dumps(reservation, default=str)[:2000]}\n"
                f"Current subject: {action.get('email_subject')}\n"
                f"Current body:\n{action.get('email_body')}"
            )),
        ])
        parsed = _parse_json_text(result.content)
        subject = (parsed.get("subject") or action.get("email_subject") or "").strip()
        body = (parsed.get("body") or action.get("email_body") or "").strip()
        if subject and body:
            return {"subject": subject[:180], "body": body}
    except Exception:
        pass
    return {"subject": action.get("email_subject") or "", "body": action.get("email_body") or ""}


def _latest_pending_action(reservation_id: str, preferred_type: str | None = None) -> dict | None:
    from db.repository import _get_client

    client = _get_client()
    query = (
        client.table("reservation_action_requests")
        .select("*")
        .eq("reservation_id", reservation_id)
        .eq("status", "pending")
    )
    if preferred_type:
        query = query.eq("action_type", preferred_type)
    rows = query.order("created_at", desc=True).limit(1).execute().data or []
    return rows[0] if rows else None


def _get_action(action_id: str) -> dict | None:
    from db.repository import get_reservation_action

    return get_reservation_action(action_id)


def _parse_json_text(content: Any) -> dict[str, Any]:
    text = content if isinstance(content, str) else str(content)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except Exception:
        return {}


def _help_text() -> str:
    return (
        "Tasting room commands:\n"
        "- status / pending\n"
        "- show Haein\n"
        "- mark Haein invoice sent\n"
        "- mark Haein paid\n"
        "- queue final confirmation for Haein\n"
        "- approve <action_id>\n"
        "- reject <action_id>\n"
        "- revise Haein email to be warmer and shorter"
    )
