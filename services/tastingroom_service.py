"""Tasting room reservation coordination service.

The tasting room agent is email-native: every inbound email updates a
reservation case, and availability is stored as a source-backed claim.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from email.utils import parseaddr
from typing import Any, Optional

from app.config import (
    JOSH_EMAIL,
    TASTINGROOM_SAFE_MODE,
    TASTINGROOM_TEST_RECIPIENT,
)
from db.models import (
    AvailabilityClaim,
    Reservation,
    ReservationActionRequest,
    ReservationEvent,
)

JOSH_EMAIL_LC = JOSH_EMAIL.lower()


def _client_salutation(name: str | None) -> str:
    if not name:
        return "there"
    cleaned = re.sub(r"\s+", " ", name).strip()
    cleaned = re.sub(r"\b(FireHorse|Test|E2E|Smoke)\b", "", cleaned, flags=re.I).strip()
    return (cleaned.split()[0] if cleaned else "there").title()


def _facility_salutation() -> str:
    return "Cecil" if "cecil" in JOSH_EMAIL_LC else "Josh"


def _guest_phrase(count: int | None) -> str:
    if not count:
        return "your party"
    if count == 1:
        return "one guest"
    return f"your party of {count}"


def _friendly_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value[:10]).strftime("%A, %B %-d, %Y")
    except Exception:
        return value


def _friendly_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value[:5], "%H:%M").strftime("%-I:%M %p").lower()
    except Exception:
        return value.removesuffix(":00")


def _compact_slot(reservation: Reservation) -> str:
    parts = [
        reservation.requested_date or "requested date",
        _friendly_time(reservation.requested_time),
        f"{reservation.guest_count} guests" if reservation.guest_count else "guests TBD",
    ]
    return " ".join(part for part in parts if part)


SAFE_ACTIONS = {
    "",
    "ask_internal_availability",
    "ask_josh_availability",
    "ask_client_alternatives",
    "offer_client_slot",
    "send_tentative_invoice",
    "review_payment_status",
    "send_final_confirmation",
    "close_case",
    "escalate",
    "wait_for_josh",
}

def _email_only(value: str) -> str:
    return parseaddr(value or "")[1].lower()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _latest_text(value: str) -> str:
    text = value or ""
    markers = [
        "\nOn ",
        "\nFrom:",
        "\n________________________________",
        "\n---------- Forwarded message",
    ]
    cut = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx > 0:
            cut = min(cut, idx)
    return text[:cut].strip() or text.strip()


def _date_slug(value: str | None) -> str:
    if not value:
        return date.today().strftime("%Y%m%d")
    return value.replace("-", "")


def _name_slug(value: str | None) -> str:
    raw = re.sub(r"[^A-Za-z0-9]+", "-", value or "UNKNOWN").strip("-")
    return raw.upper()[:28] or "UNKNOWN"


def make_reservation_id(client_name: str | None, requested_date: str | None, guest_count: int | None) -> str:
    return f"TASTING-{_date_slug(requested_date)}-{guest_count or 'X'}G-{_name_slug(client_name)}"


def parse_price_cents(experience: str | None) -> Optional[int]:
    if not experience:
        return None
    match = re.search(r"\$(\d+(?:\.\d{1,2})?)", experience)
    if not match:
        return None
    return int(round(float(match.group(1)) * 100))


def parse_date(value: str | None, *, reference_year: int = 2026) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else reference_year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\b",
        text,
        flags=re.I,
    )
    if match:
        try:
            return datetime.strptime(f"{match.group(1)} {match.group(2)}, {reference_year}", "%B %d, %Y").date().isoformat()
        except ValueError:
            return None
    return None


def parse_time(value: str | None) -> Optional[str]:
    if not value:
        return None
    text = value.strip().lower().replace(".", "")
    if text in {"am", "morning"}:
        return None
    match = re.search(r"\b(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?\b", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    suffix = match.group(3)
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif not suffix and hour in {1, 2, 3, 4, 5, 6, 7}:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}:00"


def parse_time_candidates(text: str) -> list[dict]:
    candidates: list[dict] = []
    for raw in re.findall(r"\b(?:10|12:?30|1230|2:?30|230|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", text, flags=re.I):
        parsed = parse_time(raw)
        if parsed and {"start_time": parsed, "time_description": raw.strip()} not in candidates:
            candidates.append({"start_time": parsed, "time_description": raw.strip()})
    lowered = text.lower()
    if "morning" in lowered or re.search(r"\bam\b", lowered):
        candidates.append({"start_time": None, "time_description": "morning"})
    return candidates


def parse_dated_slot_lines(text: str) -> list[dict]:
    slots: list[dict] = []
    for line in text.splitlines():
        slot_date = parse_date(line)
        if not slot_date:
            continue
        for candidate in parse_time_candidates(line):
            slot = {"date": slot_date, **candidate}
            if slot not in slots:
                slots.append(slot)
    return slots


def parse_primary_time(text: str | None) -> Optional[str]:
    candidates = parse_time_candidates(text or "")
    return candidates[0]["start_time"] if candidates else None


def parse_squarespace_form(body: str) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key_norm = key.strip().lower()
        fields[key_norm] = value.strip()

    name = fields.get("name")
    email = fields.get("email")
    phone = fields.get("phone")
    requested_date = parse_date(fields.get("date requested"))
    requested_time = parse_time(fields.get("time"))
    guests_raw = fields.get("number of guests", "")
    guest_count = int(guests_raw) if guests_raw.isdigit() else None
    experience = fields.get("production tour and tasting")
    notes = fields.get("questions / comments")

    return {
        "client_name": name,
        "client_email": email.lower() if email else None,
        "phone": phone,
        "requested_date": requested_date,
        "requested_time": requested_time,
        "guest_count": guest_count,
        "experience_type": experience,
        "price_per_person_cents": parse_price_cents(experience),
        "notes": notes,
    }


def classify_email(subject: str, sender: str, body: str) -> str:
    sender_email = _email_only(sender)
    latest = _latest_text(body)
    text = f"{subject}\n{latest}".lower()
    subj_l = subject.lower()
    sender_l = sender.lower()
    # Squarespace form — keyed primarily on the (stable) notifier sender, because
    # the body is HTML and may not carry the legacy "sent via form submission" /
    # "date requested" text markers. This is the website-request entry point.
    if "form-submission@squarespace.info" in sender_l or "squarespace" in sender_l:
        if "form submission" in subj_l or any(
            k in subj_l for k in ("tasting", "booking", "visit", "reservation")
        ):
            return "squarespace_form"
    if "sent via form submission" in text and "date requested" in text:
        return "squarespace_form"
    if "your reservation has been confirmed" in text and "party name:" in text:
        return "final_confirmation_sent"
    if sender_email == JOSH_EMAIL_LC or "josh uran" in sender.lower():
        if "confirmed" in text or "these are confirmed" in text:
            return "josh_booking_confirmation"
        if any(word in text for word in ("open", "available", "booked", "full", "confirmed", "neither")) or parse_time_candidates(latest):
            return "josh_availability_reply"
        return "josh_reply"
    if "hi josh" in text and re.search(r"\b(book|confirm)\b", text):
        return "facility_booking_request"
    if "hi josh" in text and "availability" in text:
        return "facility_availability_request"
    if "fully booked" in text and ("other dates" in text or "work with your schedule" in text):
        return "staff_unavailable_reply"
    if any(phrase in text for phrase in ("checked our availability", "slot open", "do have the", "should have availability")) and (
        "would you like" in text or "let me know if you" in text
    ):
        return "staff_slot_offer"
    if "invoice" in text and any(word in text for word in ("paid", "payment", "prepayment", "link", "created")):
        return "invoice_payment_message"
    if any(phrase in text for phrase in ("please reserve", "would like to book", "yes", "that works", "reserve that time")):
        return "client_acceptance"
    if any(phrase in text for phrase in ("do you have any availability", "how about", "are available", "any other dates")):
        return "client_alternative_request"
    if any(phrase in text for phrase in ("thank you", "reach out then", "back in november")):
        return "client_deferred"
    return "unclassified"


def extract_email_facts(subject: str, sender: str, body: str, message_type: str) -> dict[str, Any]:
    latest = _latest_text(body)
    if message_type == "squarespace_form":
        facts = parse_squarespace_form(body)
    else:
        combined = f"{subject}\n{latest}"
        facts = {
            "client_email": None,
            "requested_date": parse_date(latest) or parse_date(subject),
            "requested_time": parse_primary_time(latest) or parse_primary_time(subject),
            "guest_count": None,
            "candidate_slots": [],
        }
        guests = re.search(r"\b(?:party of|for|guests?:?)\s*(\d+)\b|\b(\d+)\s*(?:people|guests?)\b", combined, flags=re.I)
        if guests:
            facts["guest_count"] = int(guests.group(1) or guests.group(2))
        if message_type == "invoice_payment_message":
            name_match = re.search(r"\b(?:by|for|to)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})", combined)
        else:
            name_match = re.search(r"\bParty Name:\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})", combined)
        if name_match:
            facts["client_name"] = name_match.group(1).strip()
        if message_type == "invoice_payment_message":
            lowered = latest.lower()
            if "paid invoice" in lowered or "has paid invoice" in lowered:
                facts["payment_status"] = "paid"
            elif "created invoice" in lowered or "sent an invoice" in lowered:
                facts["payment_status"] = "sent"
        if message_type in {"client_alternative_request", "josh_availability_reply", "facility_booking_request", "facility_availability_request", "staff_slot_offer"}:
            slots = parse_dated_slot_lines(latest)
            date_matches = re.findall(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", latest)
            dates = [parse_date(d) for d in date_matches] or [facts.get("requested_date")]
            if not slots:
                for slot_date in [d for d in dates if d]:
                    for candidate in parse_time_candidates(latest) or [{"start_time": None, "time_description": "unspecified"}]:
                        slot = {"date": slot_date, **candidate}
                        if slot not in slots:
                            slots.append(slot)
            facts["candidate_slots"] = slots
    facts["message_type"] = message_type
    facts["sender_email"] = _email_only(sender)
    if message_type in {"client_acceptance", "client_alternative_request", "client_deferred"}:
        sender_email = facts["sender_email"]
        if sender_email and sender_email not in {JOSH_EMAIL_LC, "form-submission@squarespace.info"}:
            facts["client_email"] = facts.get("client_email") or sender_email
    return facts


def llm_extract_email(subject: str, sender: str, body: str, message_type: str) -> dict[str, Any]:
    """Best-effort LLM extraction; deterministic regex remains the fallback."""
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=(
                "Extract winery tasting reservation facts from this email. "
                "Return only JSON. Use null when unknown. Do not infer payment or booking confirmation. "
                "Fields: client_name, client_email, phone, requested_date YYYY-MM-DD, requested_time HH:MM:SS, "
                "guest_count integer, experience_type, price_per_person_cents integer, "
                "candidate_slots array of {date,start_time,time_description}, summary."
            )),
            HumanMessage(content=f"Type: {message_type}\nSubject: {subject}\nFrom: {sender}\n\n{body[:6000]}"),
        ])
        content = result.content.strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.rsplit("```", 1)[0].strip()
        parsed = json.loads(content)
        return {k: v for k, v in parsed.items() if v not in ("", [], {})}
    except Exception as exc:
        logging.debug("[tastingroom] LLM extraction skipped: %s", exc)
        return {}


def merge_llm_facts(facts: dict[str, Any], llm_facts: dict[str, Any], message_type: str | None = None) -> dict[str, Any]:
    """Merge LLM facts conservatively: never overwrite deterministic facts."""
    merged = dict(facts)
    untrusted_client_context = message_type in {
        "josh_reply",
        "josh_availability_reply",
        "josh_booking_confirmation",
        "facility_availability_request",
        "facility_booking_request",
    }
    for key, value in (llm_facts or {}).items():
        if value in (None, "", [], {}):
            continue
        if untrusted_client_context and key in {"client_name", "client_email", "phone"} and not merged.get(key):
            continue
        if key == "candidate_slots":
            existing = merged.get("candidate_slots") or []
            for slot in value:
                if slot not in existing:
                    existing.append(slot)
            merged["candidate_slots"] = existing
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def build_case_timeline(reservation_id: str, limit: int = 40) -> str:
    try:
        from db.repository import list_availability_claims, list_reservation_events

        events = list_reservation_events(reservation_id, limit=limit)
        claims = list_availability_claims(reservation_id, limit=limit)
    except Exception as exc:
        logging.debug("[tastingroom] timeline unavailable: %s", exc)
        return ""

    lines: list[str] = []
    for event in events[-limit:]:
        lines.append(
            f"- event {event.get('created_at')}: {event.get('event_type')} "
            f"from {event.get('actor') or 'unknown'}; {event.get('summary') or ''}"
        )
    if claims:
        lines.append("Claims:")
    for claim in claims[-limit:]:
        lines.append(
            f"- {claim.get('actor')} {claim.get('claim_type')}={claim.get('claim_status')} "
            f"{claim.get('date') or ''} {claim.get('start_time') or claim.get('time_description') or ''} "
            f"source={claim.get('source_message_id')}"
        )
    return "\n".join(lines)[-6000:]


def plan_next_action_from_timeline(
    reservation: Reservation,
    *,
    message_type: str,
    fallback_action: str | None,
) -> dict[str, str]:
    """Use the LLM as a case brain, bounded by safe action names.

    The state machine remains the guardrail. The model may choose no action or
    refine the next action, but only from SAFE_ACTIONS.
    """
    if reservation.current_state in {"FINAL_CONFIRMED", "CANCELLED_OR_DEFERRED"}:
        return {"recommended_action": ""}
    if fallback_action in {"close_case", "wait_for_josh"}:
        return {"recommended_action": fallback_action or ""}
    timeline = build_case_timeline(reservation.reservation_id)
    if not timeline:
        return {"recommended_action": _guard_planned_action(reservation, fallback_action or "", fallback_action)}
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=(
                "You are the Innovatus tasting room case brain. Read the timeline and choose the next Audrey-like "
                "operational action. Do not invent availability, payment, or booking. Preserve the distinction between "
                "client requested slot, Josh/facility availability, internal availability, client acceptance, invoice, "
                "payment, and final confirmation. Return only JSON with keys: recommended_action, reason. "
                f"recommended_action must be one of: {', '.join(sorted(SAFE_ACTIONS))}."
            )),
            HumanMessage(content=(
                f"Reservation: {reservation.reservation_id}\n"
                f"Client: {reservation.client_name} <{reservation.client_email}>\n"
                f"Slot: {reservation.requested_date} {reservation.requested_time}\n"
                f"Guests: {reservation.guest_count}\n"
                f"State: {reservation.current_state}\n"
                f"Fallback action: {fallback_action or 'none'}\n"
                f"Latest message type: {message_type}\n\n"
                f"Timeline:\n{timeline}"
            )),
        ])
        parsed = _parse_json_text(result.content)
        action = (parsed.get("recommended_action") or fallback_action or "").strip()
        action = _guard_planned_action(reservation, action, fallback_action)
        return {"recommended_action": action, "reason": parsed.get("reason", "")}
    except Exception as exc:
        logging.debug("[tastingroom] timeline planner skipped: %s", exc)
        return {"recommended_action": _guard_planned_action(reservation, fallback_action or "", fallback_action)}


def _guard_planned_action(reservation: Reservation, action: str, fallback_action: str | None) -> str:
    if action not in SAFE_ACTIONS:
        return fallback_action or "escalate"
    if reservation.current_state in {"FINAL_CONFIRMED", "CANCELLED_OR_DEFERRED"}:
        return ""
    if action == "send_final_confirmation" and reservation.payment_status != "paid":
        return fallback_action if fallback_action != "send_final_confirmation" else "review_payment_status"
    if action == "send_tentative_invoice" and reservation.current_state not in {
        "CLIENT_ACCEPTED_SLOT",
        "TENTATIVELY_BOOKED",
    }:
        return fallback_action or "escalate"
    if action == "offer_client_slot" and reservation.current_state not in {
        "READY_TO_OFFER_CLIENT",
        "SLOT_OFFERED_TO_CLIENT",
    }:
        return fallback_action or "escalate"
    return action


def _parse_json_text(content: Any) -> dict[str, Any]:
    text = content if isinstance(content, str) else str(content)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


def find_or_create_reservation(
    *,
    gmail_thread_id: str,
    subject: str,
    facts: dict[str, Any],
) -> tuple[str, Optional[dict]]:
    from db.repository import find_recent_reservations, find_reservation_by_thread, get_reservation

    explicit = re.search(r"\bTASTING-[A-Z0-9-]+\b", f"{subject}\n{facts}", flags=re.I)
    if explicit:
        rid = explicit.group(0).upper()
        return rid, get_reservation(rid)

    if gmail_thread_id:
        existing = find_reservation_by_thread(gmail_thread_id)
        if existing:
            terminal = existing.get("current_state") in {"FINAL_CONFIRMED", "CANCELLED_OR_DEFERRED"}
            facility_context = facts.get("message_type") in {
                "josh_reply",
                "josh_availability_reply",
                "josh_booking_confirmation",
                "facility_availability_request",
                "facility_booking_request",
            }
            if not (terminal and facility_context):
                return existing["reservation_id"], existing

    email = facts.get("client_email")
    requested_date = facts.get("requested_date")
    matches = find_recent_reservations(email, requested_date, limit=2) if (email or requested_date) else []
    if len(matches) == 1:
        return matches[0]["reservation_id"], matches[0]

    contextual = _find_contextual_reservation(facts)
    if contextual:
        return contextual["reservation_id"], contextual
    named = _find_named_reservation(facts)
    if named:
        return named["reservation_id"], named

    rid = make_reservation_id(facts.get("client_name"), requested_date, facts.get("guest_count"))
    return rid, get_reservation(rid)


def _find_named_reservation(facts: dict[str, Any]) -> Optional[dict]:
    if not facts.get("client_name"):
        return None
    from db.repository import find_recent_reservations

    target = _name_slug(facts.get("client_name"))
    for row in find_recent_reservations(limit=50):
        if row.get("client_name") and _name_slug(row.get("client_name")) == target:
            return row
    return None


def _find_contextual_reservation(facts: dict[str, Any]) -> Optional[dict]:
    from db.repository import find_recent_reservations

    candidate_dates = {
        slot.get("date")
        for slot in facts.get("candidate_slots") or []
        if slot.get("date")
    }
    if facts.get("requested_date"):
        candidate_dates.add(facts["requested_date"])
    if not candidate_dates:
        return None

    guest_count = facts.get("guest_count")
    rows = find_recent_reservations(limit=50)
    unresolved = {
        "REQUEST_RECEIVED",
        "WAITING_FOR_JOSH",
        "NEEDS_INTERNAL_CHECK",
        "CLIENT_REQUESTED_ALTERNATIVE",
        "READY_TO_OFFER_CLIENT",
        "SLOT_OFFERED_TO_CLIENT",
        "CLIENT_ACCEPTED_SLOT",
        "TENTATIVELY_BOOKED",
        "WAITING_FOR_PAYMENT",
    }
    scored: list[tuple[int, dict]] = []
    target_name = _name_slug(facts.get("client_name")) if facts.get("client_name") else None
    for row in rows:
        if row.get("current_state") in {"FINAL_CONFIRMED", "CANCELLED_OR_DEFERRED"} and not target_name:
            continue
        if target_name and row.get("client_name") and _name_slug(row.get("client_name")) != target_name:
            continue
        row_dates = {row.get("requested_date")}
        row_dates.update(
            slot.get("date")
            for slot in (row.get("candidate_slots") or [])
            if isinstance(slot, dict) and slot.get("date")
        )
        active = row.get("active_slot") or {}
        if isinstance(active, dict) and active.get("date"):
            row_dates.add(active["date"])
        if not candidate_dates.intersection(row_dates):
            continue
        score = 10
        if guest_count and row.get("guest_count") == guest_count:
            score += 5
        if target_name and row.get("client_name") and _name_slug(row.get("client_name")) == target_name:
            score += 8
        if row.get("current_state") in unresolved:
            score += 3
        if row.get("client_email"):
            score += 1
        scored.append((score, row))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def merge_reservation(existing: Optional[dict], reservation_id: str, facts: dict[str, Any], gmail_thread_id: str) -> Reservation:
    thread_ids = list((existing or {}).get("gmail_thread_ids") or [])
    if gmail_thread_id and gmail_thread_id not in thread_ids:
        thread_ids.append(gmail_thread_id)

    message_type = facts.get("message_type")
    preserves_existing_slot = message_type in {
        "client_acceptance",
        "client_deferred",
        "invoice_payment_message",
        "final_confirmation_sent",
    }
    existing_date = (existing or {}).get("requested_date")
    existing_time = (existing or {}).get("requested_time")

    return Reservation(
        reservation_id=reservation_id,
        client_name=facts.get("client_name") or (existing or {}).get("client_name"),
        client_email=(facts.get("client_email") or (existing or {}).get("client_email") or None),
        phone=facts.get("phone") or (existing or {}).get("phone"),
        requested_date=(
            existing_date
            if preserves_existing_slot and existing_date
            else facts.get("requested_date") or existing_date
        ),
        requested_time=(
            existing_time
            if preserves_existing_slot and existing_time
            else facts.get("requested_time") or existing_time
        ),
        guest_count=facts.get("guest_count") or (existing or {}).get("guest_count"),
        experience_type=facts.get("experience_type") or (existing or {}).get("experience_type"),
        price_per_person_cents=facts.get("price_per_person_cents") or (existing or {}).get("price_per_person_cents"),
        current_state=(existing or {}).get("current_state") or "REQUEST_RECEIVED",
        payment_status=(existing or {}).get("payment_status") or "not_sent",
        booking_status=(existing or {}).get("booking_status") or "not_booked",
        gmail_thread_ids=thread_ids,
        active_slot=(existing or {}).get("active_slot") or {},
        candidate_slots=(
            facts.get("candidate_slots")
            if facts.get("message_type") == "client_alternative_request"
            else (existing or {}).get("candidate_slots")
        ) or [],
        recommended_action=(existing or {}).get("recommended_action"),
        confidence=float((existing or {}).get("confidence") or facts.get("confidence") or 1.0),
        notes=facts.get("notes") or (existing or {}).get("notes"),
    )


def build_claims(reservation: Reservation, facts: dict[str, Any], message_type: str, raw_text: str, source_message_id: str) -> list[AvailabilityClaim]:
    claims: list[AvailabilityClaim] = []
    sender_email = facts.get("sender_email")
    if message_type == "squarespace_form":
        claims.append(AvailabilityClaim(
            reservation_id=reservation.reservation_id,
            actor="client",
            actor_email=reservation.client_email,
            claim_type="requested_slot",
            claim_status="requested",
            date=reservation.requested_date,
            start_time=reservation.requested_time,
            guest_count=reservation.guest_count,
            experience_type=reservation.experience_type,
            source_message_id=source_message_id,
            raw_text=raw_text[:2000],
            confidence=0.95,
        ))
    elif message_type == "josh_availability_reply":
        has_times = bool(facts.get("candidate_slots") or parse_time_candidates(_latest_text(raw_text)))
        status = "available" if (re.search(r"\b(open|available)\b", raw_text, flags=re.I) or has_times) else "ambiguous"
        if re.search(r"\b(full|unavailable|neither|no availability)\b", raw_text, flags=re.I):
            status = "unavailable"
        slots = facts.get("candidate_slots") or [{"date": reservation.requested_date, "start_time": facts.get("requested_time"), "time_description": _clean_text(raw_text)[:120]}]
        deduped_slots = []
        seen_slots = set()
        for slot in slots:
            key = (slot.get("date") or reservation.requested_date, slot.get("start_time"), status)
            if key in seen_slots:
                continue
            seen_slots.add(key)
            deduped_slots.append(slot)
        slots = deduped_slots
        for slot in slots:
            claims.append(AvailabilityClaim(
                reservation_id=reservation.reservation_id,
                actor="josh",
                actor_email=sender_email,
                claim_type="facility_availability",
                claim_status=status,
                date=slot.get("date") or reservation.requested_date,
                start_time=slot.get("start_time"),
                time_description=slot.get("time_description") or _clean_text(raw_text)[:120],
                guest_count=reservation.guest_count or facts.get("guest_count"),
                experience_type=reservation.experience_type,
                source_message_id=source_message_id,
                raw_text=raw_text[:2000],
                confidence=0.86 if status == "available" else 0.7,
            ))
    elif message_type == "josh_booking_confirmation":
        claims.append(AvailabilityClaim(
            reservation_id=reservation.reservation_id,
            actor="josh",
            actor_email=sender_email,
            claim_type="facility_booking_confirmation",
            claim_status="confirmed",
            date=reservation.requested_date,
            start_time=reservation.requested_time,
            guest_count=reservation.guest_count,
            experience_type=reservation.experience_type,
            source_message_id=source_message_id,
            raw_text=raw_text[:2000],
            confidence=0.9,
        ))
    elif message_type == "facility_booking_request":
        for slot in facts.get("candidate_slots") or [_best_slot(facts, reservation)]:
            claims.append(AvailabilityClaim(
                reservation_id=reservation.reservation_id,
                actor="internal_staff",
                actor_email=sender_email,
                claim_type="facility_booking_request",
                claim_status="requested",
                date=slot.get("date") or reservation.requested_date,
                start_time=slot.get("start_time") or reservation.requested_time,
                time_description=slot.get("time_description"),
                guest_count=facts.get("guest_count") or reservation.guest_count,
                experience_type=reservation.experience_type,
                source_message_id=source_message_id,
                raw_text=raw_text[:2000],
                confidence=0.9,
            ))
    elif message_type == "facility_availability_request":
        for slot in facts.get("candidate_slots") or [_best_slot(facts, reservation)]:
            claims.append(AvailabilityClaim(
                reservation_id=reservation.reservation_id,
                actor="internal_staff",
                actor_email=sender_email,
                claim_type="facility_availability_request",
                claim_status="requested",
                date=slot.get("date") or reservation.requested_date,
                start_time=slot.get("start_time"),
                time_description=slot.get("time_description"),
                guest_count=facts.get("guest_count") or reservation.guest_count,
                experience_type=reservation.experience_type,
                source_message_id=source_message_id,
                raw_text=raw_text[:2000],
                confidence=0.85,
            ))
    elif message_type == "staff_slot_offer":
        slot = _best_slot(facts, reservation)
        claims.append(AvailabilityClaim(
            reservation_id=reservation.reservation_id,
            actor="internal_staff",
            actor_email=sender_email,
            claim_type="internal_availability",
            claim_status="available",
            date=slot.get("date") or reservation.requested_date,
            start_time=slot.get("start_time") or reservation.requested_time,
            time_description=slot.get("time_description"),
            guest_count=facts.get("guest_count") or reservation.guest_count,
            experience_type=reservation.experience_type,
            source_message_id=source_message_id,
            raw_text=raw_text[:2000],
            confidence=0.9,
            reviewed_by_human=True,
        ))
    elif message_type == "client_alternative_request":
        for slot in facts.get("candidate_slots") or []:
            claims.append(AvailabilityClaim(
                reservation_id=reservation.reservation_id,
                actor="client",
                actor_email=reservation.client_email or sender_email,
                claim_type="requested_slot",
                claim_status="alternative_offered",
                date=slot.get("date"),
                start_time=slot.get("start_time"),
                time_description=slot.get("time_description"),
                guest_count=reservation.guest_count,
                experience_type=reservation.experience_type,
                source_message_id=source_message_id,
                raw_text=raw_text[:2000],
                confidence=0.8,
            ))
    return claims


def _has_available_claim(reservation_id: str, actor: str, claim_type: str) -> bool:
    try:
        from db.repository import list_availability_claims

        return bool(list_availability_claims(
            reservation_id,
            actor=actor,
            claim_type=claim_type,
            claim_status="available",
            limit=1,
        ))
    except Exception as exc:
        logging.warning("[tastingroom] availability claim lookup failed: %s", exc)
        return False


def _best_slot(facts: dict[str, Any], reservation: Reservation) -> dict:
    slots = facts.get("candidate_slots") or []
    if slots:
        target_date = (reservation.active_slot or {}).get("date") or reservation.requested_date
        target_time = (reservation.active_slot or {}).get("start_time") or reservation.requested_time
        for slot in slots:
            if slot.get("date") == target_date and (not target_time or slot.get("start_time") == target_time):
                return slot
        for slot in slots:
            if target_date and slot.get("date") == target_date:
                return slot
        with_time = [slot for slot in slots if slot.get("date") and slot.get("start_time")]
        return (with_time or slots)[-1]
    return {
        "date": facts.get("requested_date") or reservation.requested_date,
        "start_time": facts.get("requested_time") or reservation.requested_time,
    }


def draft_for_action(reservation: Reservation, action: str) -> dict[str, str]:
    client_name = _client_salutation(reservation.client_name)
    guests = _guest_phrase(reservation.guest_count)
    requested_date = _friendly_date(reservation.requested_date) or "the requested date"
    requested_time = _friendly_time(reservation.requested_time) or "your requested time"
    experience = reservation.experience_type or "Tasting"
    rid = reservation.reservation_id

    if action == "ask_josh_availability":
        subject = f"Availability Check {_compact_slot(reservation)} - {rid}"
        body = (
            f"Hi {_facility_salutation()},\n\n"
            "Do you have availability for the tasting request below?\n\n"
            f"Reservation ID: {rid}\n"
            f"Date: {requested_date}\n"
            f"Requested time: {requested_time}\n"
            f"Guests: {reservation.guest_count or 'Unknown'}\n"
            f"Experience: {experience}\n\n"
            "Please reply with one of:\n"
            "- Available: [time]\n"
            "- Unavailable\n"
            "- Alternative: [time]\n\n"
            "Thank you,\nAudrey\n\nINNOVATUS Wine\nwww.innovatuswine.com"
        )
        return _llm_refine_draft(reservation, action, {"recipient": JOSH_EMAIL, "subject": subject, "body": body})

    if action == "ask_client_alternatives":
        subject = "Your Innovatus tasting request"
        body = (
            f"Hi {client_name},\n\n"
            f"Thank you for your tasting request. Unfortunately, we do not have availability for {requested_date}"
            f"{' at ' + requested_time if requested_time else ''}. We would still love to host you.\n\n"
            "If there are any other dates or times that work with your schedule, please send them over and I will check availability.\n\n"
            "Cheers,\nAudrey\n\nINNOVATUS Wine\nwww.innovatuswine.com"
        )
        return _llm_refine_draft(reservation, action, {"recipient": reservation.client_email or "", "subject": subject, "body": body})

    if action == "offer_client_slot":
        subject = "Innovatus tasting availability"
        body = (
            f"Hi {client_name},\n\n"
            f"We have availability for {requested_date} at {requested_time} for {guests} "
            f"for the {experience}.\n\n"
            "Would you like us to hold this reservation for you? Once you confirm, "
            "we will send the prepayment invoice to finalize the booking.\n\n"
            "Cheers,\nAudrey\n\nINNOVATUS Wine\nwww.innovatuswine.com"
        )
        return _llm_refine_draft(reservation, action, {"recipient": reservation.client_email or "", "subject": subject, "body": body})

    if action == "send_tentative_invoice":
        subject = "Your Innovatus tasting reservation"
        body = (
            f"Hi {client_name},\n\n"
            "Wonderful. I have your reservation tentatively booked and held for you.\n\n"
            "For visits, we require prepayment to confirm the reservation and finalize the booking. "
            "I will send the invoice link separately once it is ready. Once the invoice is paid, "
            "your reservation will be confirmed and completed.\n\n"
            "Please let me know if you have any questions. We look forward to your visit.\n\n"
            "Cheers,\nAudrey\n\nINNOVATUS Wine\nwww.innovatuswine.com"
        )
        return _llm_refine_draft(reservation, action, {"recipient": reservation.client_email or "", "subject": subject, "body": body})

    if action == "send_final_confirmation":
        subject = "Your Innovatus tasting is confirmed"
        body = (
            f"Hi {client_name},\n\n"
            "Thank you for your payment. Your tasting reservation is now confirmed.\n\n"
            f"Date: {requested_date}\n"
            f"Time: {requested_time}\n"
            f"Guests: {guests}\n"
            f"Experience: {experience}\n\n"
            "We look forward to hosting you.\n\n"
            "Cheers,\nAudrey\n\nINNOVATUS Wine\nwww.innovatuswine.com"
        )
        return _llm_refine_draft(reservation, action, {"recipient": reservation.client_email or "", "subject": subject, "body": body})

    if action == "review_payment_status":
        return {
            "recipient": "",
            "subject": f"Create/send Square invoice for {rid}",
            "body": (
                "Create or verify the Square invoice for this tentative reservation. "
                "Use Mark Invoice Sent only after the Square invoice has actually been sent to the client. "
                "Use Mark Paid only after Square/payment evidence confirms payment."
            ),
        }

    return {
        "recipient": "",
        "subject": f"Review reservation {rid}",
        "body": "This reservation needs human review before the next action.",
    }


def _llm_refine_draft(reservation: Reservation, action: str, draft: dict[str, str]) -> dict[str, str]:
    """Personalize an email draft from the saved case timeline, with fallback."""
    if action not in {"ask_josh_availability", "ask_client_alternatives", "offer_client_slot", "send_tentative_invoice", "send_final_confirmation"}:
        return draft
    timeline = build_case_timeline(reservation.reservation_id)
    if not timeline:
        return draft
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0.2)
        result = llm.invoke([
            SystemMessage(content=(
                "Rewrite this tasting-room email in Audrey's concise, warm operational style. "
                "Use only facts in the reservation, timeline, and base draft. Do not invent availability, "
                "invoice links, payment, calendar invites, discounts, or confirmation. Keep the same recipient "
                "and intent. Return only JSON with keys subject and body."
            )),
            HumanMessage(content=(
                f"Action: {action}\n"
                f"Reservation: {reservation.reservation_id}\n"
                f"Client: {reservation.client_name} <{reservation.client_email}>\n"
                f"Slot: {reservation.requested_date} {reservation.requested_time}\n"
                f"Guests: {reservation.guest_count}\n\n"
                f"Timeline:\n{timeline}\n\n"
                f"Base subject: {draft.get('subject')}\n"
                f"Base body:\n{draft.get('body')}"
            )),
        ])
        parsed = _parse_json_text(result.content)
        subject = (parsed.get("subject") or draft.get("subject") or "").strip()
        body = (parsed.get("body") or draft.get("body") or "").strip()
        if not subject or not body:
            return draft
        return {**draft, "subject": subject[:180], "body": body}
    except Exception as exc:
        logging.debug("[tastingroom] draft refinement skipped: %s", exc)
        return draft


def _guest_line(reservation: Reservation) -> str:
    n = reservation.guest_count
    if not n:
        return ""
    return f"{n} guest{'s' if n != 1 else ''}"


def _slot_line(reservation: Reservation) -> str:
    parts = []
    if reservation.requested_date:
        parts.append(_friendly_date(reservation.requested_date))
    if reservation.requested_time:
        parts.append(_friendly_time(reservation.requested_time))
    return " at ".join(parts) if parts else ""


def _header(reservation: Reservation) -> str:
    name = reservation.client_name or "someone (no name yet)"
    slot = _slot_line(reservation)
    guests = _guest_line(reservation)
    lines = [name]
    if slot:
        lines[0] += f" — {slot}"
    if guests:
        lines[0] += f", {guests}"
    if reservation.experience_type:
        lines.append(reservation.experience_type)
    return "\n".join(lines)


def approval_message(reservation: Reservation, action: str, draft: dict[str, str]) -> str:
    header = _header(reservation)

    if action == "ask_internal_availability":
        return (
            f"New tasting request\n\n"
            f"{header}\n\n"
            f"The caves are available. Does this work for our schedule?\n"
            f"(Nothing gets sent — just recording whether we can do it.)"
        )
    if action == "review_payment_status":
        return (
            f"Payment check needed\n\n"
            f"{header}\n\n"
            f"The client said yes to this slot. Now we need to handle payment.\n\n"
            f"- \"Invoice Sent\" = you already sent them a Square invoice\n"
            f"- \"Paid\" = they already paid\n"
            f"- \"Send Confirmation\" = payment done, send them the final details"
        )
    if action == "escalate":
        return (
            f"Needs your attention\n\n"
            f"{header}\n\n"
            f"I couldn't figure out the next step on this one automatically. "
            f"Can you take a look?"
        )
    # Generic action with email draft
    recipient = draft.get("recipient") or ""
    subject = draft.get("subject") or ""
    body = draft.get("body", "")[:1600]
    return (
        f"Ready to send an email\n\n"
        f"{header}\n\n"
        f"To: {recipient}\n"
        f"Subject: {subject}\n\n"
        f"{body}"
        f"{safe_note()}"
    )


def safe_note() -> str:
    if not TASTINGROOM_SAFE_MODE:
        return ""
    target = TASTINGROOM_TEST_RECIPIENT or "(not set)"
    return f"\n\n(Test mode — emails go to {target} instead of the real recipient.)"


def _rows_for_action(action: str, action_id: str) -> list[list[tuple[str, str]]]:
    if action == "escalate":
        return [
            [("I'll handle it", f"tr:{action_id}:escalate"),
             ("Ignore", f"tr:{action_id}:reject")],
        ]
    if action == "ask_internal_availability":
        return [
            [("Yes, we can do it", f"tr:{action_id}:internal_available"),
             ("No, we can't", f"tr:{action_id}:internal_unavailable")],
            [("Suggest other times", f"tr:{action_id}:suggest_alternatives"),
             ("I'll handle it", f"tr:{action_id}:escalate")],
        ]
    if action == "review_payment_status":
        return [
            [("Invoice sent", f"tr:{action_id}:invoice_sent"),
             ("Already paid", f"tr:{action_id}:paid")],
            [("Send confirmation", f"tr:{action_id}:queue_final"),
             ("I'll handle it", f"tr:{action_id}:escalate")],
        ]
    return [
        [("Send it", f"tr:{action_id}:approve"), ("Don't send", f"tr:{action_id}:reject")],
        [("I'll handle it", f"tr:{action_id}:escalate")],
    ]


def create_action_request(reservation: Reservation, action: str, source_message_id: str | None = None) -> Optional[str]:
    if action in {"close_case", "wait_for_josh"}:
        return None
    draft = draft_for_action(reservation, action)
    action_id = uuid.uuid4().hex
    request = ReservationActionRequest(
        action_id=action_id,
        reservation_id=reservation.reservation_id,
        action_type=action,
        risk_level="high" if action in {"send_final_confirmation"} else "medium",
        recipient_email=draft.get("recipient"),
        email_subject=draft.get("subject"),
        email_body=draft.get("body"),
        recommendation=approval_message(reservation, action, draft),
        source_message_id=source_message_id,
    )
    from db.repository import insert_reservation_action

    insert_reservation_action(request)
    rows = _rows_for_action(action, action_id)

    # Google Chat is the sole approval channel. Posts an interactive card to the
    # configured space; if GOOGLE_CHAT_TR_SPACE is unset, the action is persisted
    # (visible via /status and the activity page) but no card is pushed.
    try:
        from app.adapters.google_chat_tastingroom import is_enabled, post_action_card

        if is_enabled():
            post_action_card(action_id, request.recommendation or "", rows)
        else:
            logging.warning(
                "[tastingroom] Google Chat approval channel not configured "
                "(set GOOGLE_CHAT_TR_SPACE) — action %s saved but not pushed", action_id
            )
    except Exception as exc:
        logging.warning("[tastingroom] Google Chat approval send failed: %s", exc)
    return action_id


def process_action_decision(action_id: str, decision: str, decided_by: str = "telegram") -> dict[str, Any]:
    from db.repository import (
        get_reservation_action,
        get_reservation,
        insert_availability_claim,
        insert_reservation_event,
        update_reservation,
        update_reservation_action,
    )

    action = get_reservation_action(action_id)
    if not action:
        return {"ok": False, "error": "Action request not found."}
    if action.get("status") not in {"pending"}:
        return {"ok": False, "error": f"Action already {action.get('status')}."}

    now = datetime.utcnow().isoformat()
    action_type = action.get("action_type")
    reservation_id = action["reservation_id"]
    reservation = get_reservation(reservation_id)

    if decision == "reject":
        update_reservation_action(action_id, status="rejected", decided_by=decided_by, decided_at=now)
        return {"ok": True, "status": "rejected", "reservation_id": reservation_id}
    if decision == "escalate":
        update_reservation_action(action_id, status="escalated", decided_by=decided_by, decided_at=now)
        if reservation:
            update_reservation(reservation_id, current_state="HUMAN_REVIEW_REQUIRED", recommended_action="escalate")
        return {"ok": True, "status": "escalated", "reservation_id": reservation_id}

    if action_type == "ask_internal_availability":
        return _process_internal_availability_decision(
            action=action,
            reservation=reservation,
            decision=decision,
            decided_by=decided_by,
            decided_at=now,
        )

    if action_type == "review_payment_status":
        return _process_payment_decision(
            action=action,
            reservation=reservation,
            decision=decision,
            decided_by=decided_by,
            decided_at=now,
        )

    if decision != "approve":
        return {"ok": False, "error": "Unknown tasting room action decision."}

    update_reservation_action(action_id, status="approved", decided_by=decided_by, decided_at=now)
    try:
        from services.gmail_service import send_email

        intended_recipient = action.get("recipient_email") or ""
        actual_recipient = intended_recipient
        if TASTINGROOM_SAFE_MODE:
            if not TASTINGROOM_TEST_RECIPIENT:
                raise RuntimeError("TASTINGROOM_SAFE_MODE is enabled but TASTINGROOM_TEST_RECIPIENT is not set.")
            actual_recipient = TASTINGROOM_TEST_RECIPIENT

        send_result = send_email(
            to=actual_recipient,
            subject=action.get("email_subject") or "",
            html=(action.get("email_body") or "").replace("\n", "<br>"),
            plain=action.get("email_body") or "",
        )
        if send_result.get("message_id") and not send_result.get("dry_run"):
            try:
                from services.tastingroom_mailbox import labels_for_result
                from services.gmail_service import apply_message_labels

                applied_labels = labels_for_result(action_type, {
                    "ask_josh_availability": "WAITING_FOR_JOSH",
                    "offer_client_slot": "WAITING_FOR_CLIENT_REPLY",
                    "ask_client_alternatives": "WAITING_FOR_CLIENT_REPLY",
                    "send_tentative_invoice": "WAITING_FOR_PAYMENT",
                    "send_final_confirmation": "FINAL_CONFIRMED",
                }.get(action_type))
                apply_message_labels(send_result["message_id"], add_labels=applied_labels)
            except Exception as exc:
                logging.warning("[tastingroom] sent-email labeling failed: %s", exc)
        update_reservation_action(action_id, status="sent")
        _apply_post_send_state(action, reservation, safe_actual_recipient=actual_recipient, send_result=send_result)
        insert_reservation_event(ReservationEvent(
            reservation_id=reservation_id,
            event_type="approved_email_sent",
            actor=decided_by,
            source_channel="telegram",
            source_message_id=action_id,
            summary=f"Sent {action_type} to {actual_recipient}",
            raw_payload={
                "send_result": send_result,
                "action": action,
                "safe_mode": TASTINGROOM_SAFE_MODE,
                "intended_recipient": intended_recipient,
                "actual_recipient": actual_recipient,
            },
        ))
        updated = get_reservation(reservation_id)
        next_action_id = None
        if action_type == "send_tentative_invoice" and updated:
            next_action_id = create_action_request(_reservation_from_row(updated), "review_payment_status", source_message_id=action_id)
        return {
            "ok": True,
            "status": "sent",
            "reservation_id": reservation_id,
            "reservation": updated,
            "next_action_id": next_action_id,
        }
    except Exception as exc:
        update_reservation_action(action_id, status="failed")
        return {"ok": False, "error": str(exc), "reservation_id": reservation_id}


def _reservation_from_row(row: dict) -> Reservation:
    return Reservation(
        reservation_id=row["reservation_id"],
        client_name=row.get("client_name"),
        client_email=row.get("client_email"),
        phone=row.get("phone"),
        requested_date=row.get("requested_date"),
        requested_time=row.get("requested_time"),
        guest_count=row.get("guest_count"),
        experience_type=row.get("experience_type"),
        price_per_person_cents=row.get("price_per_person_cents"),
        current_state=row.get("current_state") or "REQUEST_RECEIVED",
        payment_status=row.get("payment_status") or "not_sent",
        booking_status=row.get("booking_status") or "not_booked",
        gmail_thread_ids=row.get("gmail_thread_ids") or [],
        active_slot=row.get("active_slot") or {},
        candidate_slots=row.get("candidate_slots") or [],
        recommended_action=row.get("recommended_action"),
        confidence=float(row.get("confidence") or 1.0),
        notes=row.get("notes"),
    )


def _apply_post_send_state(
    action: dict,
    reservation: Optional[dict],
    safe_actual_recipient: str,
    send_result: Optional[dict] = None,
) -> None:
    from db.repository import get_reservation, update_reservation

    action_type = action.get("action_type")
    reservation_id = action["reservation_id"]
    extra: dict[str, Any] = {}
    sent_thread_id = (send_result or {}).get("thread_id")
    current_reservation = reservation or get_reservation(reservation_id)
    if current_reservation and sent_thread_id:
        thread_ids = list(current_reservation.get("gmail_thread_ids") or [])
        if sent_thread_id not in thread_ids:
            thread_ids.append(sent_thread_id)
        extra["gmail_thread_ids"] = thread_ids
    if action_type == "ask_josh_availability":
        update_reservation(reservation_id, current_state="WAITING_FOR_JOSH", recommended_action=None, **extra)
    elif action_type in {"offer_client_slot", "ask_client_alternatives"}:
        update_reservation(reservation_id, current_state="WAITING_FOR_CLIENT_REPLY", recommended_action=None, **extra)
    elif action_type == "send_tentative_invoice":
        update_reservation(
            reservation_id,
            current_state="WAITING_FOR_PAYMENT",
            payment_status="awaiting_invoice_marker",
            booking_status="tentative",
            recommended_action="review_payment_status",
            **extra,
        )
    elif action_type == "send_final_confirmation":
        update_reservation(
            reservation_id,
            current_state="FINAL_CONFIRMED",
            payment_status="paid",
            booking_status="confirmed",
            recommended_action=None,
            **extra,
        )


def _process_internal_availability_decision(
    *,
    action: dict,
    reservation: Optional[dict],
    decision: str,
    decided_by: str,
    decided_at: str,
) -> dict[str, Any]:
    from db.repository import (
        insert_availability_claim,
        insert_reservation_event,
        update_reservation,
        update_reservation_action,
    )

    reservation_id = action["reservation_id"]
    if decision not in {"internal_available", "internal_unavailable", "suggest_alternatives"}:
        return {"ok": False, "error": "Unknown internal availability decision.", "reservation_id": reservation_id}

    status = {
        "internal_available": "available",
        "internal_unavailable": "unavailable",
        "suggest_alternatives": "alternative_offered",
    }[decision]
    insert_availability_claim(AvailabilityClaim(
        reservation_id=reservation_id,
        actor="internal_staff",
        actor_email=decided_by,
        claim_type="internal_availability",
        claim_status=status,
        date=(reservation or {}).get("requested_date"),
        start_time=(reservation or {}).get("requested_time"),
        guest_count=(reservation or {}).get("guest_count"),
        experience_type=(reservation or {}).get("experience_type"),
        source_channel="telegram",
        source_message_id=action["action_id"],
        raw_text=decision,
        confidence=1.0,
        reviewed_by_human=True,
    ))
    if decision == "internal_available":
        facility_available = _has_available_claim(reservation_id, "josh", "facility_availability")
        if facility_available:
            new_state = "READY_TO_OFFER_CLIENT"
            recommended_action = "offer_client_slot"
            next_action = "offer_client_slot"
        else:
            new_state = "WAITING_FOR_JOSH"
            recommended_action = "wait_for_josh"
            next_action = None
        update_reservation(
            reservation_id,
            current_state=new_state,
            recommended_action=recommended_action,
        )
    elif decision == "internal_unavailable":
        new_state = "INTERNAL_UNAVAILABLE"
        update_reservation(
            reservation_id,
            current_state=new_state,
            recommended_action="ask_client_alternatives",
        )
        next_action = "ask_client_alternatives"
    else:
        new_state = "NO_COMMON_SLOT"
        update_reservation(
            reservation_id,
            current_state=new_state,
            recommended_action="ask_client_alternatives",
        )
        next_action = "ask_client_alternatives"

    update_reservation_action(action["action_id"], status="completed", decided_by=decided_by, decided_at=decided_at)
    insert_reservation_event(ReservationEvent(
        reservation_id=reservation_id,
        event_type="internal_availability_marked",
        actor=decided_by,
        source_channel="telegram",
        source_message_id=action["action_id"],
        summary=f"Internal availability marked: {status}",
        raw_payload={"decision": decision, "action": action},
    ))
    updated = reservation.copy() if reservation else None
    if updated and next_action:
        updated.update({"current_state": new_state, "recommended_action": next_action})
        next_action_id = create_action_request(_reservation_from_row(updated), next_action, source_message_id=action["action_id"])
    else:
        next_action_id = None
    return {
        "ok": True,
        "status": "completed",
        "reservation_id": reservation_id,
        "next_action": next_action,
        "next_action_id": next_action_id,
    }


def _process_payment_decision(
    *,
    action: dict,
    reservation: Optional[dict],
    decision: str,
    decided_by: str,
    decided_at: str,
) -> dict[str, Any]:
    from db.repository import insert_reservation_event, update_reservation, update_reservation_action

    reservation_id = action["reservation_id"]
    next_action_id = None
    if decision == "invoice_sent":
        update_reservation(
            reservation_id,
            current_state="INVOICE_SENT",
            payment_status="sent",
            recommended_action="review_payment_status",
        )
        status = "invoice_sent"
        updated = reservation.copy() if reservation else None
        if updated:
            updated.update({"current_state": "INVOICE_SENT", "payment_status": "sent", "recommended_action": "review_payment_status"})
            next_action_id = create_action_request(_reservation_from_row(updated), "review_payment_status", source_message_id=action["action_id"])
    elif decision == "paid":
        update_reservation(
            reservation_id,
            current_state="PAYMENT_RECEIVED",
            payment_status="paid",
            recommended_action="send_final_confirmation",
        )
        status = "paid"
        updated = reservation.copy() if reservation else None
        if updated:
            updated.update({"current_state": "PAYMENT_RECEIVED", "payment_status": "paid", "recommended_action": "send_final_confirmation"})
            next_action_id = create_action_request(_reservation_from_row(updated), "send_final_confirmation", source_message_id=action["action_id"])
        else:
            next_action_id = None
    elif decision == "queue_final":
        if not reservation or reservation.get("payment_status") != "paid":
            return {"ok": False, "error": "Payment must be marked paid before final confirmation.", "reservation_id": reservation_id}
        next_action_id = create_action_request(_reservation_from_row(reservation), "send_final_confirmation", source_message_id=action["action_id"])
        status = "final_confirmation_queued"
    else:
        return {"ok": False, "error": "Unknown payment decision.", "reservation_id": reservation_id}

    update_reservation_action(action["action_id"], status="completed", decided_by=decided_by, decided_at=decided_at)
    insert_reservation_event(ReservationEvent(
        reservation_id=reservation_id,
        event_type="payment_status_marked",
        actor=decided_by,
        source_channel="telegram",
        source_message_id=action["action_id"],
        summary=f"Payment action: {status}",
        raw_payload={"decision": decision, "action": action},
    ))
    return {"ok": True, "status": status, "reservation_id": reservation_id, "next_action_id": next_action_id}


def persist_processed_email(
    *,
    reservation: Reservation,
    message_type: str,
    facts: dict[str, Any],
    claims: list[AvailabilityClaim],
    source_message_id: str,
    raw_payload: dict,
) -> None:
    from db.repository import insert_availability_claim, insert_reservation_event, upsert_reservation

    upsert_reservation(reservation)
    insert_reservation_event(ReservationEvent(
        reservation_id=reservation.reservation_id,
        event_type=message_type,
        actor=facts.get("sender_email"),
        source_channel="email",
        source_message_id=source_message_id,
        summary=f"Processed {message_type}; next action: {reservation.recommended_action}",
        raw_payload=raw_payload,
    ))
    for claim in claims:
        insert_availability_claim(claim)
