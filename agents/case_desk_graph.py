"""Case Desk Agent — evidence-and-judgment architecture for tasting room email coordination.

Architecture:
  store_raw_event
  → extract_claims
  → resolve_case          (match + claims only; unresolved → END)
  → persist_claims
  → build_case_bundle
  → judge_case
  → save_case_judgment
  → update_reservation_cache
  → validate_and_act
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import POSTGRES_CONNECTION_STRING

logger = logging.getLogger(__name__)


class CaseDeskState(TypedDict, total=False):
    # --- input ---
    raw_email: str
    sender_id: str
    subject: str
    from_email: str
    to_email: str
    body: str
    gmail_message_id: str
    gmail_thread_id: str
    disable_actions: bool

    # --- extracted ---
    message_type: str
    extracted_facts: dict[str, Any]
    _claims: list[dict[str, Any]]

    # --- resolved ---
    reservation_id: str
    _reservation: dict[str, Any]
    _unresolved: bool

    # --- judgment ---
    _bundle: dict[str, Any]
    _judgment: dict[str, Any]
    _judgment_record_id: str
    _validation_result_id: str

    # --- output ---
    action_id: str
    final_response: str
    error: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def store_raw_event(state: CaseDeskState) -> CaseDeskState:
    """Persist inbound email to raw_email_events. Idempotent — safe to re-run."""
    from db.models import RawEmailEvent
    from db import repository

    mid = state.get("gmail_message_id", "")
    logger.debug("case_desk: store_raw_event msg=%s thread=%s", mid, state.get("gmail_thread_id"))

    if mid:
        try:
            repository.insert_raw_email_event(RawEmailEvent(
                gmail_message_id=mid,
                gmail_thread_id=state.get("gmail_thread_id", ""),
                subject=state.get("subject", ""),
                from_email=state.get("from_email", ""),
                to_email=state.get("to_email", ""),
                body=state.get("body") or state.get("raw_email", ""),
                raw_payload={
                    "subject": state.get("subject"),
                    "from": state.get("from_email"),
                    "to": state.get("to_email"),
                    "thread_id": state.get("gmail_thread_id"),
                },
            ))
        except Exception as exc:
            logger.debug("store_raw_event: best-effort insert failed: %s", exc)
    return {}


def extract_claims(state: CaseDeskState) -> CaseDeskState:
    """Classify the email, extract facts, build placeholder claims."""
    from services.tastingroom_service import (
        build_claims,
        classify_email,
        extract_email_facts,
        llm_extract_email,
        merge_llm_facts,
    )
    from db.models import Reservation

    subject = state.get("subject", "")
    sender = state.get("from_email") or state.get("sender_id", "")
    body = state.get("body") or state.get("raw_email", "")

    message_type = classify_email(subject, sender, body)
    facts = extract_email_facts(subject, sender, body, message_type)
    llm_facts = llm_extract_email(subject, sender, body, message_type)
    facts = merge_llm_facts(facts, llm_facts, message_type)
    facts = {**facts, "message_type": message_type, "sender_email": facts.get("sender_email")}

    placeholder = Reservation(reservation_id="__placeholder__")
    claims = build_claims(
        placeholder,
        facts,
        message_type,
        body,
        state.get("gmail_message_id", ""),
    )
    return {
        "message_type": message_type,
        "extracted_facts": facts,
        "_claims": [asdict(c) for c in claims],
    }


def resolve_case(state: CaseDeskState) -> CaseDeskState:
    """Match or create a reservation. No apply_state() — matching only.

    If the email is unclassified AND there is no existing case AND facts are
    sparse (no client email, no date), mark as unresolved and bail out.
    """
    from services.tastingroom_service import (
        build_claims,
        find_or_create_reservation,
        merge_reservation,
    )
    from db.models import UnresolvedEvent
    from db import repository

    facts = state.get("extracted_facts", {})
    message_type = state.get("message_type", "unclassified")
    body = state.get("body") or state.get("raw_email", "")

    rid, existing = find_or_create_reservation(
        gmail_thread_id=state.get("gmail_thread_id", ""),
        subject=state.get("subject", ""),
        facts=facts,
    )

    # Unresolved check: brand-new case + unclassified + no useful facts.
    is_new = existing is None
    has_useful_facts = bool(
        facts.get("client_email") or facts.get("requested_date") or facts.get("client_name")
    )
    if is_new and message_type == "unclassified" and not has_useful_facts:
        try:
            repository.insert_unresolved_event(UnresolvedEvent(
                source_message_id=state.get("gmail_message_id", ""),
                gmail_thread_id=state.get("gmail_thread_id", ""),
                subject=state.get("subject", ""),
                from_email=state.get("from_email", ""),
                message_type=message_type,
                reason="Unclassified email with no useful facts and no matching reservation.",
                raw_payload={
                    "subject": state.get("subject"),
                    "from": state.get("from_email"),
                    "facts": facts,
                },
            ))
        except Exception:
            pass
        return {"_unresolved": True, "final_response": "Unresolved: no matching case, logged for review."}

    reservation = merge_reservation(existing, rid, facts, state.get("gmail_thread_id", ""))
    claims = build_claims(
        reservation,
        facts,
        message_type,
        body,
        state.get("gmail_message_id", ""),
    )
    return {
        "reservation_id": reservation.reservation_id,
        "_reservation": asdict(reservation),
        "_claims": [asdict(c) for c in claims],
        "_unresolved": False,
    }


def _route_after_resolve(state: CaseDeskState) -> str:
    return END if state.get("_unresolved") else "persist_claims"


def persist_claims(state: CaseDeskState) -> CaseDeskState:
    """Upsert reservation + events + claims to DB."""
    from services.tastingroom_service import persist_processed_email
    from db.models import AvailabilityClaim, Reservation

    reservation_data = state.get("_reservation")
    claims_data = state.get("_claims", [])
    if not reservation_data:
        return {"error": "No reservation to persist."}

    reservation = Reservation(**reservation_data)
    claims = [AvailabilityClaim(**c) for c in claims_data]

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


def build_bundle_node(state: CaseDeskState) -> CaseDeskState:
    """Assemble full CaseBundle from DB."""
    from services.case_memory import build_case_bundle
    from db.models import AvailabilityClaim, Reservation

    reservation_data = state.get("_reservation")
    if not reservation_data:
        return {"error": "No reservation for bundle."}

    reservation = Reservation(**reservation_data)
    claims = [asdict(AvailabilityClaim(**c)) for c in state.get("_claims", [])]
    bundle = build_case_bundle(reservation=reservation, claims=claims, events=[])
    return {"_bundle": bundle}


def judge_case_node(state: CaseDeskState) -> CaseDeskState:
    """LLM judgment over the full case bundle."""
    from services.case_judge import judge_case

    judgment = judge_case(
        bundle=state.get("_bundle", {}),
        message_type=state.get("message_type", "unclassified"),
    )
    return {"_judgment": judgment.model_dump()}


def save_case_judgment(state: CaseDeskState) -> CaseDeskState:
    """Persist the CaseJudgment snapshot to DB. Idempotent via idempotency_key."""
    from db.models import CaseJudgmentRecord
    from db import repository
    from services.case_judge import CaseJudgment

    judgment_data = state.get("_judgment", {})
    if not judgment_data:
        return {}

    case_id = state.get("reservation_id", "")
    source_message_id = state.get("gmail_message_id", "")
    idempotency_key = f"{case_id}:{source_message_id}" if case_id and source_message_id else None

    # Check if this judgment was already saved (retry safety).
    if idempotency_key:
        try:
            existing = (
                repository._get_client()
                .table("case_judgments")
                .select("record_id")
                .eq("idempotency_key", idempotency_key)
                .limit(1)
                .execute()
            )
            if existing.data:
                return {"_judgment_record_id": existing.data[0]["record_id"]}
        except Exception:
            pass

    try:
        judgment = CaseJudgment.model_validate(judgment_data)
        record = CaseJudgmentRecord(
            case_id=case_id,
            source_message_id=source_message_id,
            judgment_json=judgment_data,
            confidence=judgment.confidence,
            next_best_action=judgment.next_best_action.tool_name,
            interrupt_level=judgment.interrupt_level,
        )
        # Add idempotency_key to insert payload via direct client call.
        repository._get_client().table("case_judgments").insert({
            "record_id": record.record_id,
            "case_id": record.case_id,
            "source_message_id": record.source_message_id or None,
            "judgment_json": record.judgment_json or {},
            "confidence": record.confidence,
            "next_best_action": record.next_best_action or None,
            "interrupt_level": record.interrupt_level,
            "idempotency_key": idempotency_key,
        }).execute()
        return {"_judgment_record_id": record.record_id}
    except Exception as exc:
        logger.exception("save_case_judgment failed: %s", exc)
        return {}


def _derive_state_from_truth(truth) -> str:
    if truth.confirmation_status == "final_sent":
        return "FINAL_CONFIRMED"
    if truth.payment_status == "paid":
        return "PAYMENT_RECEIVED"
    if truth.payment_status == "invoice_sent":
        return "INVOICE_SENT"
    if truth.facility_status == "confirmed" and truth.client_intent == "accepted_slot":
        return "TENTATIVELY_BOOKED"
    if truth.client_intent == "accepted_slot":
        return "CLIENT_ACCEPTED_SLOT"
    if truth.facility_status == "available":
        return "READY_TO_OFFER_CLIENT"
    if truth.facility_status == "availability_requested":
        return "WAITING_FOR_JOSH"
    if truth.client_intent == "requested_slot":
        return "REQUEST_RECEIVED"
    return "UNRESOLVED"


def update_reservation_cache(state: CaseDeskState) -> CaseDeskState:
    """Write judgment-derived truth back to reservations table.

    current_state is now a cache derived from the latest CaseJudgment,
    not the output of a deterministic state machine.
    """
    from services.case_judge import CaseJudgment
    from db import repository

    judgment_data = state.get("_judgment", {})
    reservation_id = state.get("reservation_id", "")
    if not judgment_data or not reservation_id:
        return {}

    try:
        judgment = CaseJudgment.model_validate(judgment_data)
        derived_state = _derive_state_from_truth(judgment.current_truth)
        mapped_action = judgment.next_best_action.tool_name

        repository.update_reservation(
            reservation_id,
            current_state=derived_state,
            recommended_action=mapped_action,
            confidence=judgment.confidence,
        )
        # Reflect in the in-memory reservation dict too.
        reservation_data = state.get("_reservation", {})
        if reservation_data:
            reservation_data = {
                **reservation_data,
                "current_state": derived_state,
                "recommended_action": mapped_action,
            }
            return {"_reservation": reservation_data}
    except Exception as exc:
        logger.exception("update_reservation_cache failed: %s", exc)
    return {}


_TOOL_TO_ACTION = {
    "draft_client_reply": "offer_client_slot",
    "draft_josh_availability_request": "ask_josh_availability",
    "draft_josh_booking_request": "confirm_booking_with_josh",
    "draft_invoice_message": "send_tentative_invoice",
    "draft_final_confirmation": "send_final_confirmation",
    "flag_for_staff_review": "escalate",
}


def validate_and_act(state: CaseDeskState) -> CaseDeskState:
    """Apply safety guards. Fire Telegram approval only if interrupt_level == 'immediate'.

    Persists ValidationResultRecord and ExecutionResultRecord for every run.
    Idempotency check prevents duplicate action_requests on retry.
    """
    from services.case_judge import CaseJudgment, ToolPlan
    from services.safety_guards import downgrade_to_flag, validate_plan
    from services.tastingroom_service import create_action_request
    from services.tool_registry import tasting_room_registry
    from db.models import Reservation, ValidationResultRecord, ExecutionResultRecord
    from db import repository

    judgment_data = state.get("_judgment", {})
    reservation_data = state.get("_reservation", {})
    reservation_id = state.get("reservation_id", "")
    source_message_id = state.get("gmail_message_id", "")
    judgment_record_id = state.get("_judgment_record_id", "")
    reservation = Reservation(**reservation_data) if reservation_data else None

    try:
        judgment = CaseJudgment.model_validate(judgment_data)
    except Exception as exc:
        logger.exception("validate_and_act: could not parse judgment: %s", exc)
        return {
            "final_response": (
                f"Reservation {reservation_id} processed.\n"
                "Judgment parse failed — flagged for staff review."
            )
        }

    allowed, reason = validate_plan(judgment)
    if not allowed:
        judgment = downgrade_to_flag(judgment, reason)
        logger.info("validate_and_act: action blocked — %s", reason)

    action = judgment.next_best_action.tool_name

    # Validate against tool registry — unknown tools get flagged.
    tool_def = tasting_room_registry.get(f"tasting.{action}") or tasting_room_registry.get(action)
    if tool_def is None and action not in ("none", "flag_for_staff_review"):
        reason = f"Tool '{action}' not in tasting_room_registry — flagging for review."
        judgment = downgrade_to_flag(judgment, reason)
        allowed = False
        action = judgment.next_best_action.tool_name
        logger.warning("validate_and_act: %s", reason)

    # Save validation audit record.
    val_record = ValidationResultRecord(
        case_id=reservation_id,
        tool_name=action,
        allowed=allowed,
        source_message_id=source_message_id,
        judgment_record_id=judgment_record_id,
        block_reason=reason if not allowed else "",
        guardrails_triggered=[] if allowed else [reason],
        approval_required=judgment.next_best_action.requires_human_approval,
        interrupt_level=judgment.interrupt_level,
    )
    repository.insert_validation_result(val_record)

    # Build Telegram-style summary.
    evidence_lines = "\n".join(
        f"  [{e.evidence_type}|{e.confidence:.0%}] {e.source_message_id[:8]}… {e.claim}"
        for e in judgment.evidence[:5]
    )
    uncertainty_lines = "\n".join(
        f"  [{u.severity}] {u.question}"
        for u in judgment.uncertainties[:3]
    )

    summary_lines = [
        f"Case: {reservation_id}",
        f"Summary: {judgment.case_summary}",
        f"Confidence: {judgment.confidence:.0%}  |  interrupt: {judgment.interrupt_level}",
        f"Next action: {action}  ({judgment.next_best_action.reason})",
    ]
    if judgment.blockers:
        summary_lines.append(f"Blockers: {'; '.join(judgment.blockers)}")
    if judgment.risks:
        summary_lines.append(f"Risks: {'; '.join(judgment.risks)}")
    if uncertainty_lines:
        summary_lines.append(f"Uncertainties:\n{uncertainty_lines}")
    if evidence_lines:
        summary_lines.append(f"Evidence:\n{evidence_lines}")

    # Gate: fire Telegram for "immediate" and "digest" (everything with an action).
    # Only skip for "none" (purely informational, no action needed).
    skip_telegram = (
        state.get("disable_actions")
        or judgment.interrupt_level == "none"
        or action in ("none",)
    )
    if skip_telegram:
        summary_lines.append(
            "Telegram: skipped"
            + (" (actions disabled)" if state.get("disable_actions") else f" (level={judgment.interrupt_level})")
        )
        return {
            "_validation_result_id": val_record.result_id,
            "final_response": "\n".join(summary_lines),
        }

    mapped_action = _TOOL_TO_ACTION.get(action, action)

    # Idempotency: skip if action request already exists for this email+action.
    action_id: str | None = None
    idempotency_key = f"{reservation_id}:{source_message_id}:{mapped_action}"
    try:
        existing = (
            repository._get_client()
            .table("reservation_action_requests")
            .select("action_id")
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
        )
        if existing.data:
            action_id = existing.data[0]["action_id"]
            logger.info("validate_and_act: idempotent skip, existing action_id=%s", action_id)
    except Exception:
        pass

    exec_ok = False
    exec_error_type = ""
    exec_error_msg = ""

    if action_id is None and reservation:
        try:
            action_id = create_action_request(
                reservation,
                mapped_action,
                source_message_id=source_message_id,
            )
            # Backfill idempotency_key on the new row.
            if action_id:
                try:
                    repository._get_client().table("reservation_action_requests").update(
                        {"idempotency_key": idempotency_key}
                    ).eq("action_id", action_id).execute()
                except Exception:
                    pass
            exec_ok = bool(action_id)
        except Exception as exc:
            logger.exception("create_action_request failed: %s", exc)
            exec_error_type = type(exc).__name__
            exec_error_msg = str(exc)
    else:
        exec_ok = bool(action_id)

    # Save execution result.
    repository.insert_execution_result(ExecutionResultRecord(
        case_id=reservation_id,
        tool_name=mapped_action,
        ok=exec_ok,
        action_request_id=action_id or "",
        result_json={"action_id": action_id} if action_id else {},
        error_type=exec_error_type,
        error_message=exec_error_msg,
        created_resource_id=action_id or "",
    ))

    summary_lines.append(f"Approval request: {action_id or 'failed'}")
    return {
        "_validation_result_id": val_record.result_id,
        "action_id": action_id or "",
        "final_response": "\n".join(summary_lines),
    }


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------

def build_case_desk_graph(checkpointer=None):
    g = StateGraph(CaseDeskState)

    g.add_node("store_raw_event", store_raw_event)
    g.add_node("extract_claims", extract_claims)
    g.add_node("resolve_case", resolve_case)
    g.add_node("persist_claims", persist_claims)
    g.add_node("build_case_bundle", build_bundle_node)
    g.add_node("judge_case", judge_case_node)
    g.add_node("save_case_judgment", save_case_judgment)
    g.add_node("update_reservation_cache", update_reservation_cache)
    g.add_node("validate_and_act", validate_and_act)

    g.add_edge(START, "store_raw_event")
    g.add_edge("store_raw_event", "extract_claims")
    g.add_edge("extract_claims", "resolve_case")
    g.add_conditional_edges("resolve_case", _route_after_resolve)
    g.add_edge("persist_claims", "build_case_bundle")
    g.add_edge("build_case_bundle", "judge_case")
    g.add_edge("judge_case", "save_case_judgment")
    g.add_edge("save_case_judgment", "update_reservation_cache")
    g.add_edge("update_reservation_cache", "validate_and_act")
    g.add_edge("validate_and_act", END)

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


case_desk_graph = build_case_desk_graph(checkpointer=_make_checkpointer())
