"""Supabase repository — read/write for invoice_logs and the control layer tables."""

from typing import Optional
from supabase import create_client, Client

from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY
from db.models import (
    AvailabilityClaim,
    Case,
    ExecutionResultRecord,
    FailureLabel,
    InvoiceLog,
    RawEmailEvent,
    Reservation,
    ReservationActionRequest,
    ReservationEvent,
    TraceEvent,
    UnresolvedEvent,
    ValidationResultRecord,
    WorkflowRecord,
)

_client: Optional[Client] = None


def _get_client() -> Client:
    global _client
    if _client:
        return _client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
        )
    _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


def _to_row(record: InvoiceLog) -> dict:
    return {
        "thread_id": record.thread_id,
        "sender_id": record.sender_id,
        "raw_message": record.raw_message,
        "customer_id": record.customer_id,
        "customer_name": record.customer_name,
        "customer_email": record.customer_email,
        "tier_name": record.tier_name,
        "line_items": record.line_items or [],
        "subtotal_cents": record.subtotal_cents,
        "discount_cents": record.discount_cents,
        "total_before_tax_cents": record.total_before_tax_cents,
        "shipping_cents": record.shipping_cents,
        "payment_schedule": record.payment_schedule,
        "payment_methods": record.payment_methods or [],
        "approval": record.approval,
        "square_order_id": record.square_order_id,
        "square_invoice_id": record.square_invoice_id,
        "square_invoice_url": record.square_invoice_url,
        "errors": record.errors or [],
    }


def upsert_invoice(record: InvoiceLog) -> None:
    """Insert or update an invoice log record (keyed on thread_id)."""
    client = _get_client()
    client.table("invoice_logs").upsert(
        _to_row(record),
        on_conflict="thread_id",
    ).execute()


def log_invoice(record: InvoiceLog) -> None:
    """Alias for upsert_invoice — insert or update."""
    upsert_invoice(record)


def get_invoice_log(thread_id: str) -> Optional[InvoiceLog]:
    """Retrieve an invoice log by thread_id. Returns None if not found."""
    client = _get_client()
    result = (
        client.table("invoice_logs")
        .select("*")
        .eq("thread_id", thread_id)
        .limit(1)
        .execute()
    )
    rows = result.data
    if not rows:
        return None
    row = rows[0]
    return InvoiceLog(
        thread_id=row["thread_id"],
        sender_id=row.get("sender_id"),
        raw_message=row.get("raw_message"),
        customer_id=row.get("customer_id"),
        customer_name=row.get("customer_name"),
        customer_email=row.get("customer_email"),
        tier_name=row.get("tier_name"),
        line_items=row.get("line_items") or [],
        subtotal_cents=row.get("subtotal_cents"),
        discount_cents=row.get("discount_cents"),
        total_before_tax_cents=row.get("total_before_tax_cents"),
        shipping_cents=row.get("shipping_cents"),
        payment_schedule=row.get("payment_schedule"),
        payment_methods=row.get("payment_methods") or [],
        approval=row.get("approval"),
        square_order_id=row.get("square_order_id"),
        square_invoice_id=row.get("square_invoice_id"),
        square_invoice_url=row.get("square_invoice_url"),
        errors=row.get("errors") or [],
    )


def list_recent_invoices(limit: int = 20) -> list[dict]:
    """List recent invoice logs, newest first."""
    client = _get_client()
    result = (
        client.table("invoice_logs")
        .select("thread_id, customer_name, tier_name, total_before_tax_cents, approval, square_invoice_id, created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def get_recent_invoice_for_customer(
    customer_name: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> Optional[dict]:
    """Fetch the most recent invoice log for a customer (by Square customer_id or name).

    Returns the raw row dict (includes line_items JSON) or None if not found.
    """
    if not customer_id and not customer_name:
        return None
    client = _get_client()
    query = client.table("invoice_logs").select("*")
    if customer_id:
        query = query.eq("customer_id", customer_id)
    else:
        query = query.ilike("customer_name", f"%{customer_name}%")
    result = query.order("created_at", desc=True).limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Tasting room reservations
# ---------------------------------------------------------------------------

def _reservation_to_row(record: Reservation) -> dict:
    return {
        "reservation_id": record.reservation_id,
        "client_name": record.client_name,
        "client_email": record.client_email,
        "phone": record.phone,
        "requested_date": record.requested_date,
        "requested_time": record.requested_time,
        "guest_count": record.guest_count,
        "experience_type": record.experience_type,
        "price_per_person_cents": record.price_per_person_cents,
        "current_state": record.current_state,
        "payment_status": record.payment_status,
        "booking_status": record.booking_status,
        "gmail_thread_ids": record.gmail_thread_ids or [],
        "active_slot": record.active_slot or {},
        "candidate_slots": record.candidate_slots or [],
        "recommended_action": record.recommended_action,
        "confidence": record.confidence,
        "notes": record.notes,
    }


def upsert_reservation(record: Reservation) -> None:
    client = _get_client()
    client.table("reservations").upsert(
        _reservation_to_row(record),
        on_conflict="reservation_id",
    ).execute()


def get_reservation(reservation_id: str) -> Optional[dict]:
    client = _get_client()
    result = (
        client.table("reservations")
        .select("*")
        .eq("reservation_id", reservation_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def update_reservation(reservation_id: str, **fields) -> None:
    client = _get_client()
    client.table("reservations").update(fields).eq("reservation_id", reservation_id).execute()


def find_reservation_by_thread(gmail_thread_id: str) -> Optional[dict]:
    client = _get_client()
    result = (
        client.table("reservations")
        .select("*")
        .contains("gmail_thread_ids", [gmail_thread_id])
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def find_recent_reservations(
    client_email: Optional[str] = None,
    requested_date: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    client = _get_client()
    query = client.table("reservations").select("*")
    if client_email:
        query = query.eq("client_email", client_email.lower())
    if requested_date:
        query = query.eq("requested_date", requested_date)
    result = query.order("updated_at", desc=True).limit(limit).execute()
    return result.data or []


def list_recent_reservations(limit: int = 20, include_smoke: bool = False) -> list[dict]:
    client = _get_client()
    query = client.table("reservations").select("*")
    if not include_smoke:
        query = query.not_.like("reservation_id", "TASTING-SMOKE-%")
    result = query.order("updated_at", desc=True).limit(limit).execute()
    return result.data or []


def insert_availability_claim(claim: AvailabilityClaim) -> None:
    client = _get_client()
    client.table("availability_claims").insert({
        "reservation_id": claim.reservation_id,
        "actor": claim.actor,
        "actor_email": claim.actor_email,
        "claim_type": claim.claim_type,
        "claim_status": claim.claim_status,
        "date": claim.date,
        "start_time": claim.start_time,
        "end_time": claim.end_time,
        "time_description": claim.time_description,
        "guest_count": claim.guest_count,
        "experience_type": claim.experience_type,
        "source_channel": claim.source_channel,
        "source_message_id": claim.source_message_id,
        "raw_text": claim.raw_text,
        "confidence": claim.confidence,
        "expires_at": claim.expires_at,
        "reviewed_by_human": claim.reviewed_by_human,
    }).execute()


def list_availability_claims(
    reservation_id: str,
    *,
    actor: Optional[str] = None,
    claim_type: Optional[str] = None,
    claim_status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    client = _get_client()
    query = (
        client.table("availability_claims")
        .select("*")
        .eq("reservation_id", reservation_id)
    )
    if actor:
        query = query.eq("actor", actor)
    if claim_type:
        query = query.eq("claim_type", claim_type)
    if claim_status:
        query = query.eq("claim_status", claim_status)
    result = query.order("created_at", desc=True).limit(limit).execute()
    return result.data or []


def insert_reservation_event(event: ReservationEvent) -> None:
    client = _get_client()
    client.table("reservation_events").insert({
        "reservation_id": event.reservation_id,
        "event_type": event.event_type,
        "actor": event.actor,
        "source_channel": event.source_channel,
        "source_message_id": event.source_message_id,
        "summary": event.summary,
        "raw_payload": event.raw_payload or {},
    }).execute()


def list_reservation_events(reservation_id: str, limit: int = 50) -> list[dict]:
    client = _get_client()
    result = (
        client.table("reservation_events")
        .select("*")
        .eq("reservation_id", reservation_id)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


def list_reservation_events_by_source(source_message_id: str, limit: int = 10) -> list[dict]:
    client = _get_client()
    result = (
        client.table("reservation_events")
        .select("*")
        .eq("source_message_id", source_message_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def insert_reservation_action(action: ReservationActionRequest) -> None:
    client = _get_client()
    client.table("reservation_action_requests").insert({
        "action_id": action.action_id,
        "reservation_id": action.reservation_id,
        "action_type": action.action_type,
        "status": action.status,
        "risk_level": action.risk_level,
        "recipient_email": action.recipient_email,
        "email_subject": action.email_subject,
        "email_body": action.email_body,
        "recommendation": action.recommendation,
        "telegram_chat_id": action.telegram_chat_id,
        "telegram_message_id": action.telegram_message_id,
        "source_message_id": action.source_message_id,
        "decided_by": action.decided_by,
        "decided_at": action.decided_at,
    }).execute()


def get_reservation_action(action_id: str) -> Optional[dict]:
    client = _get_client()
    result = (
        client.table("reservation_action_requests")
        .select("*")
        .eq("action_id", action_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def update_reservation_action(action_id: str, **fields) -> None:
    client = _get_client()
    client.table("reservation_action_requests").update(fields).eq("action_id", action_id).execute()


# ---------------------------------------------------------------------------
# Unresolved events
# ---------------------------------------------------------------------------

def insert_raw_email_event(event: RawEmailEvent) -> None:
    """Insert a raw email event. ON CONFLICT (gmail_message_id) DO NOTHING — idempotent."""
    client = _get_client()
    try:
        client.table("raw_email_events").upsert(
            {
                "event_id": event.event_id,
                "gmail_message_id": event.gmail_message_id,
                "gmail_thread_id": event.gmail_thread_id or None,
                "subject": event.subject or None,
                "from_email": event.from_email or None,
                "to_email": event.to_email or None,
                "body": event.body or None,
                "raw_payload": event.raw_payload or {},
            },
            on_conflict="gmail_message_id",
            ignore_duplicates=True,
        ).execute()
    except Exception:
        pass  # best-effort


def get_raw_email_event(gmail_message_id: str) -> Optional[dict]:
    client = _get_client()
    result = (
        client.table("raw_email_events")
        .select("*")
        .eq("gmail_message_id", gmail_message_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def list_raw_email_events_for_case(case_id: str) -> list[dict]:
    """Return raw email events for a case, ordered chronologically.

    Joins via reservation_events.source_message_id to find which gmail_message_ids
    belong to this case.
    """
    client = _get_client()
    events = list_reservation_events(case_id, limit=100)
    source_ids = [e["source_message_id"] for e in events if e.get("source_message_id")]
    if not source_ids:
        return []
    raw = []
    for mid in source_ids:
        row = get_raw_email_event(mid)
        if row:
            raw.append(row)
    return raw


def insert_validation_result(record: ValidationResultRecord) -> None:
    client = _get_client()
    try:
        client.table("validation_results").insert({
            "result_id": record.result_id,
            "case_id": record.case_id,
            "source_message_id": record.source_message_id or None,
            "judgment_record_id": record.judgment_record_id or None,
            "tool_name": record.tool_name or None,
            "allowed": record.allowed,
            "block_reason": record.block_reason or None,
            "guardrails_triggered": record.guardrails_triggered or [],
            "approval_required": record.approval_required,
            "interrupt_level": record.interrupt_level,
        }).execute()
    except Exception:
        pass  # best-effort


def insert_execution_result(record: ExecutionResultRecord) -> None:
    client = _get_client()
    try:
        client.table("execution_results").insert({
            "result_id": record.result_id,
            "case_id": record.case_id,
            "action_request_id": record.action_request_id or None,
            "tool_name": record.tool_name,
            "ok": record.ok,
            "result_json": record.result_json or None,
            "error_type": record.error_type or None,
            "error_message": record.error_message or None,
            "created_resource_id": record.created_resource_id or None,
        }).execute()
    except Exception:
        pass  # best-effort


def insert_unresolved_event(event: UnresolvedEvent) -> None:
    client = _get_client()
    client.table("unresolved_reservation_events").insert({
        "event_id": event.event_id,
        "source_message_id": event.source_message_id or None,
        "gmail_thread_id": event.gmail_thread_id or None,
        "subject": event.subject or None,
        "from_email": event.from_email or None,
        "message_type": event.message_type,
        "reason": event.reason or None,
        "raw_payload": event.raw_payload or {},
    }).execute()


# ---------------------------------------------------------------------------
# Control Layer — agent_cases, trace_events, failure_labels
# All writes are best-effort: callers should wrap in try/except.
# ---------------------------------------------------------------------------

def insert_case(case: Case) -> None:
    client = _get_client()
    client.table("agent_cases").insert({
        "case_id":    case.case_id,
        "sender_id":  case.sender_id,
        "user_id":    case.user_id,
        "thread_id":  case.thread_id,
        "raw_input":  case.raw_input,
        "intent":     case.intent or None,
        "agent":      case.agent or None,
        "risk_level": case.risk_level,
        "status":     case.status,
    }).execute()


def update_case(case_id: str, **fields) -> None:
    client = _get_client()
    client.table("agent_cases").update(fields).eq("case_id", case_id).execute()


def get_case_row(case_id: str) -> Optional[dict]:
    """Return the agent_cases row for case_id, or None."""
    client = _get_client()
    resp = (
        client.table("agent_cases")
        .select("*")
        .eq("case_id", case_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def list_stale_running_cases(older_than_iso: str, limit: int = 500) -> list[dict]:
    """Return cases still in 'running' that were created before `older_than_iso`.

    Used by the stale-case reaper to find orphans/zombies left behind by a
    process kill or a resume-after-restart (where the in-memory case registry
    was lost).
    """
    client = _get_client()
    resp = (
        client.table("agent_cases")
        .select("case_id,sender_id,intent,created_at")
        .eq("status", "running")
        .lt("created_at", older_than_iso)
        .order("created_at")
        .limit(limit)
        .execute()
    )
    return resp.data or []


def insert_trace_event(event: TraceEvent) -> None:
    client = _get_client()
    client.table("trace_events").insert({
        "event_id":   event.event_id,
        "case_id":    event.case_id,
        "event_type": event.event_type,
        "layer":      event.layer,
        "data":       event.data or {},
        "latency_ms": event.latency_ms,
        "error":      event.error,
    }).execute()


def insert_failure_label(label: FailureLabel) -> None:
    client = _get_client()
    client.table("failure_labels").insert({
        "failure_id":        label.failure_id,
        "case_id":           label.case_id,
        "failure_type":      label.failure_type,
        "severity":          label.severity,
        "source":            label.source,
        "responsible_layer": label.responsible_layer,
        "description":       label.description,
        "suggested_patch":   label.suggested_patch,
        "confidence":        label.confidence,
        "eval_case_id":      label.eval_case_id,
    }).execute()


def update_failure_eval_case(failure_id: str, eval_case_id: str) -> None:
    client = _get_client()
    client.table("failure_labels").update(
        {"eval_case_id": eval_case_id}
    ).eq("failure_id", failure_id).execute()


def list_unlabeled_failures(limit: int = 20) -> list[dict]:
    client = _get_client()
    result = (
        client.table("failure_labels")
        .select("*")
        .eq("patch_applied", False)
        .is_("eval_case_id", "null")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------------------------
# Workflow records — terminal business outcome per case
# ---------------------------------------------------------------------------

def write_workflow_record(record: WorkflowRecord) -> None:
    """Insert a terminal workflow outcome record. Best-effort — does not raise."""
    client = _get_client()
    row: dict = {
        "record_id":            record.record_id,
        "case_id":              record.case_id,
        "bot_type":             record.bot_type,
        "business_object_type": record.business_object_type,
        "business_object_id":   record.business_object_id,
        "status":               record.status,
        "summary":              record.summary,
        "external_system":      record.external_system or None,
        "external_id":          record.external_id or None,
        "error_message":        record.error_message or None,
        "needs_review":         record.needs_review,
    }
    if record.completed_at:
        row["completed_at"] = record.completed_at
    client.table("workflow_records").insert(row).execute()


def list_recent_workflow_records(limit: int = 20, bot_type: str = "") -> list[dict]:
    """List recent workflow records, newest first. Optionally filter by bot_type."""
    client = _get_client()
    query = client.table("workflow_records").select("*")
    if bot_type:
        query = query.eq("bot_type", bot_type)
    result = query.order("created_at", desc=True).limit(limit).execute()
    return result.data or []
