"""Supabase repository — read/write for invoice_logs and the control layer tables."""

import logging
import re
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
        "square_invoice_number": record.square_invoice_number,
        "verification_status": record.verification_status or "pending",
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
        square_invoice_number=row.get("square_invoice_number"),
        verification_status=row.get("verification_status") or "pending",
        errors=row.get("errors") or [],
    )


def find_invoice_log_by_number(square_invoice_number: str) -> Optional[dict]:
    """Fetch the invoice log row for a Square invoice number ("202468")."""
    number = str(square_invoice_number or "").lstrip("#").strip()
    if not number:
        return None
    client = _get_client()
    result = (
        client.table("invoice_logs")
        .select("*")
        .eq("square_invoice_number", number)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def find_square_invoice_by_number(invoice_number: str) -> Optional[dict]:
    """Fetch the synced square_invoices row for a display invoice number.

    Covers invoices the agent didn't create (drafted directly in the Square
    Dashboard), which invoice_logs therefore doesn't know about."""
    number = str(invoice_number or "").lstrip("#").strip()
    if not number:
        return None
    client = _get_client()
    result = (
        client.table("square_invoices")
        .select("square_invoice_id, invoice_number, status, total_money_cents")
        .eq("invoice_number", number)
        .order("invoice_created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def list_unverified_invoices(limit: int = 25) -> list[dict]:
    """Invoice logs still awaiting Square-email confirmation (open cases)."""
    client = _get_client()
    result = (
        client.table("invoice_logs")
        .select("thread_id, customer_name, square_invoice_id, square_invoice_number, verification_status, created_at")
        .in_("verification_status", ["pending", "created_confirmed"])
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def mark_invoice_verification(thread_id: str, *, status: str, stamp_field: str) -> None:
    """Record a Square-email confirmation on an invoice log (by thread_id)."""
    from datetime import datetime, timezone

    client = _get_client()
    client.table("invoice_logs").update({
        "verification_status": status,
        stamp_field: datetime.now(timezone.utc).isoformat(),
    }).eq("thread_id", thread_id).execute()


def set_invoice_number(thread_id: str, square_invoice_number: str) -> None:
    """Backfill the Square invoice number on an invoice log (by thread_id)."""
    client = _get_client()
    client.table("invoice_logs").update({
        "square_invoice_number": str(square_invoice_number or "").lstrip("#").strip(),
    }).eq("thread_id", thread_id).execute()


def update_workflow_record_status(external_id: str, status: str, summary: str = "") -> None:
    """Move a workflow record (matched by its Square invoice id) to a new status —
    used by the invoice mail validator to close pending_verification cases."""
    client = _get_client()
    patch: dict = {"status": status}
    if summary:
        patch["summary"] = summary[:200]
    client.table("workflow_records").update(patch).eq("external_id", external_id).execute()


def list_recent_invoices(limit: int = 20) -> list[dict]:
    """List recent invoice logs, newest first."""
    client = _get_client()
    result = (
        client.table("invoice_logs")
        .select("thread_id, customer_name, tier_name, total_before_tax_cents, approval, square_invoice_id, square_invoice_number, verification_status, created_at")
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
# Per-customer history (synced Square data + agent invoice logs)
#
# square_invoices / square_orders are written by scripts/sync.py; these are the
# runtime READ side, powering the invoice chat agent's client_history tools.
# In the live data only ~40% of synced rows carry the customers.id uuid while
# others carry only the raw square_customer_id, so matching must accept EITHER
# key — an eq on one of them silently drops history.
# ---------------------------------------------------------------------------

def _match_customer(query, customer_id: Optional[str], square_customer_id: Optional[str]):
    """Filter a square_* query by whichever customer keys we have (OR when both).
    Returns None when there is nothing to match on."""
    if customer_id and square_customer_id:
        return query.or_(f"customer_id.eq.{customer_id},square_customer_id.eq.{square_customer_id}")
    if customer_id:
        return query.eq("customer_id", customer_id)
    if square_customer_id:
        return query.eq("square_customer_id", square_customer_id)
    return None


def list_square_invoices_for_customer(
    customer_id: Optional[str] = None,
    square_customer_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Historical Square invoices for one customer, newest first."""
    client = _get_client()
    query = client.table("square_invoices").select(
        "square_invoice_id, square_order_id, invoice_number, title, status, "
        "payment_schedule, total_money_cents, due_date, paid_at, invoice_created_at"
    )
    query = _match_customer(query, customer_id, square_customer_id)
    if query is None:
        return []
    result = query.order("invoice_created_at", desc=True).limit(limit).execute()
    return result.data or []


def list_square_orders_for_customer(
    customer_id: Optional[str] = None,
    square_customer_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Historical Square orders (with line_items) for one customer, newest first."""
    client = _get_client()
    query = client.table("square_orders").select(
        "square_order_id, state, total_money_cents, line_items, order_created_at"
    )
    query = _match_customer(query, customer_id, square_customer_id)
    if query is None:
        return []
    result = query.order("order_created_at", desc=True).limit(limit).execute()
    return result.data or []


def get_square_orders_by_ids(order_ids: list[str]) -> list[dict]:
    """Fetch square_orders rows by their Square order ids (for invoice→items joins)."""
    ids = [i for i in (order_ids or []) if i]
    if not ids:
        return []
    client = _get_client()
    result = (
        client.table("square_orders")
        .select("square_order_id, state, total_money_cents, line_items, order_created_at")
        .in_("square_order_id", ids)
        .execute()
    )
    return result.data or []


def list_invoice_logs_for_customer(
    customer_name: Optional[str] = None,
    customer_id: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """Agent-created invoice logs for one customer, newest first.

    The plural sibling of get_recent_invoice_for_customer — same matching rules.
    """
    if not customer_id and not customer_name:
        return []
    client = _get_client()
    query = client.table("invoice_logs").select(
        "thread_id, customer_name, customer_email, tier_name, line_items, "
        "total_before_tax_cents, shipping_cents, payment_schedule, approval, "
        "square_invoice_id, square_invoice_number, verification_status, created_at"
    )
    if customer_id:
        query = query.eq("customer_id", customer_id)
    else:
        query = query.ilike("customer_name", f"%{customer_name}%")
    result = query.order("created_at", desc=True).limit(limit).execute()
    return result.data or []


def update_customer_fields(customer_id: str, fields: dict) -> bool:
    """Update one customers row (by uuid). Returns True if a row was touched.

    The write side of the invoice chat agent's stage_update_client — tier,
    contact info, notes. Caller validates the fields; this just persists.
    """
    if not customer_id or not fields:
        return False
    client = _get_client()
    result = client.table("customers").update(fields).eq("id", customer_id).execute()
    return bool(result.data)


# ---------------------------------------------------------------------------
# Invoice chat transcript (durable memory for the invoice chat assistant)
#
# invoice_chat_memory keeps a fast in-process rolling window; these rows are
# the durable copy — they survive restarts (rehydration) and power the
# months-later past_conversations recall tool.
# ---------------------------------------------------------------------------

def insert_chat_turn(case_key: str, role: str, text: str, user_id: str = "") -> None:
    """Append one side of an invoice-chat exchange to the durable transcript."""
    client = _get_client()
    client.table("invoice_chat_turns").insert({
        "case_key": case_key,
        "user_id": user_id or None,
        "role": role,
        "text": text,
    }).execute()


def list_chat_turns_for_case(case_key: str, limit: int = 16) -> list[dict]:
    """The newest `limit` turns for one case, returned oldest-first."""
    if not case_key:
        return []
    client = _get_client()
    result = (
        client.table("invoice_chat_turns")
        .select("role, text, created_at")
        .eq("case_key", case_key)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed(result.data or []))


def search_chat_turns(query_text: str, limit: int = 20) -> list[dict]:
    """Case-insensitive substring search over the durable chat transcript,
    newest first — powers "what did we discuss about X months ago?"."""
    q = (query_text or "").strip()
    if not q:
        return []
    client = _get_client()
    result = (
        client.table("invoice_chat_turns")
        .select("case_key, role, text, created_at")
        .ilike("text", f"%{q}%")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def list_reservations_for_client(
    client_name: Optional[str] = None,
    client_email: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """Tasting-room reservations for one client, newest first. READ-ONLY —
    the invoice chat agent may look at bookings but never touches the
    tasting-room pipeline (Hard Rule 2 governs case intake, not reads)."""
    if not client_name and not client_email:
        return []
    client = _get_client()
    query = client.table("reservations").select(
        "reservation_id, client_name, client_email, requested_date, requested_time, "
        "guest_count, experience_type, current_state, payment_status, booking_status, "
        "square_invoice_number, square_invoice_status, notes, created_at, updated_at"
    ).not_.like("reservation_id", "TASTING-SMOKE-%")
    if client_email:
        query = query.ilike("client_email", client_email.strip())
    else:
        query = query.ilike("client_name", f"%{client_name}%")
    result = query.order("updated_at", desc=True).limit(limit).execute()
    return result.data or []


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
        "square_customer_id": record.square_customer_id,
        "square_order_id": record.square_order_id,
        "square_invoice_id": record.square_invoice_id,
        "square_invoice_number": record.square_invoice_number,
        "square_invoice_url": record.square_invoice_url,
        "square_invoice_total_cents": record.square_invoice_total_cents,
        "square_invoice_status": record.square_invoice_status,
        "square_invoice_verified_at": record.square_invoice_verified_at,
        "calendar_event_id": record.calendar_event_id,
        "calendar_event_url": record.calendar_event_url,
        "gmail_thread_ids": record.gmail_thread_ids or [],
        "active_slot": record.active_slot or {},
        "candidate_slots": record.candidate_slots or [],
        "recommended_action": record.recommended_action,
        "confidence": record.confidence,
        "notes": record.notes,
    }


_MISSING_COLUMN_RE = re.compile(r"Could not find the '([^']+)' column of '([^']+)'")
# Columns already alerted about this process — one Chat alert per column, not one
# per email, so schema drift is loud without flooding the space.
_alerted_missing_columns: set[str] = set()


def _missing_column(exc: Exception, table: str) -> Optional[str]:
    """Return the column name if `exc` is PostgREST's missing-column error
    (PGRST204) for `table`, else None."""
    text = " ".join(str(part) for part in (getattr(exc, "message", ""), exc))
    match = _MISSING_COLUMN_RE.search(text)
    if match and match.group(2) == table:
        return match.group(1)
    return None


def _alert_schema_drift(table: str, column: str, context: str) -> None:
    """CRITICAL log + best-effort Chat alert when the live schema is missing a
    column the code writes. Never raises."""
    logging.getLogger(__name__).critical(
        "[repository] %s table is missing column %r — dropped it to save %s. "
        "Apply the pending alters in db/schema.sql to Supabase.",
        table, column, context,
    )
    if column in _alerted_missing_columns:
        return
    _alerted_missing_columns.add(column)
    try:
        from app.adapters.google_chat_tastingroom import post_text

        post_text(
            f"🚨 Schema drift: the `{table}` table is missing column `{column}`. "
            f"I saved {context} without it, but apply the pending alters in "
            "db/schema.sql to Supabase before data is lost."
        )
    except Exception:
        pass


def verify_reservations_schema() -> Optional[str]:
    """Probe the live reservations table for every column this code writes.

    Returns None when code and DB agree, else the PostgREST error text. Run at
    watcher startup so a deploy whose schema.sql alters were not applied is
    caught on boot — not days later when the first booking silently fails.
    """
    columns = ",".join(_reservation_to_row(Reservation(reservation_id="__probe__")))
    try:
        _get_client().table("reservations").select(columns).limit(1).execute()
        return None
    except Exception as exc:
        return str(exc)


def upsert_reservation(record: Reservation) -> None:
    """Insert or update a reservation (keyed on reservation_id).

    Schema-drift tolerant: if the live table lacks a column this code writes
    (PostgREST PGRST204), the column is dropped from the row and the write is
    retried, so a new case is opened instead of lost. (A July 2026 booking was
    silently dropped exactly this way.) Each drift is alerted loudly.
    """
    client = _get_client()
    row = _reservation_to_row(record)
    for _ in range(len(row)):
        try:
            client.table("reservations").upsert(
                row,
                on_conflict="reservation_id",
            ).execute()
            return
        except Exception as exc:
            column = _missing_column(exc, "reservations")
            if not column or column not in row:
                raise
            row.pop(column)
            _alert_schema_drift("reservations", column, record.reservation_id)
    raise RuntimeError(f"upsert_reservation: no writable columns left for {record.reservation_id}")


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
    source_ids = list(dict.fromkeys(
        e["source_message_id"] for e in events if e.get("source_message_id")
    ))
    if not source_ids:
        return []
    raw = []
    for mid in source_ids:
        row = get_raw_email_event(mid)
        if row:
            raw.append(row)
    return raw


def list_raw_email_events_by_thread(gmail_thread_id: str, limit: int = 10) -> list[dict]:
    """Return raw emails previously stored for a Gmail thread, oldest first.

    Powers thread-aware LLM extraction: a reply like "yes, that works" only
    makes sense next to the earlier messages of the same thread.
    """
    if not gmail_thread_id:
        return []
    client = _get_client()
    # The table's timestamp column is ingested_at (not created_at) — it lives
    # outside schema.sql; see the raw_email_events reference DDL there.
    result = (
        client.table("raw_email_events")
        .select("*")
        .eq("gmail_thread_id", gmail_thread_id)
        .order("ingested_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = result.data or []
    rows.reverse()  # newest-first from the DB → oldest-first for the LLM
    return rows


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


def list_recent_cases(limit: int = 20, status: str = "") -> list[dict]:
    """Recent agent cases, newest first; optionally filtered by status.

    Read-only view for the MCP operator console (app/mcp_invoice.py)."""
    client = _get_client()
    query = (
        client.table("agent_cases")
        .select("case_id, sender_id, user_id, intent, agent, risk_level, status, outcome, error_summary, created_at, closed_at")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if status:
        query = query.eq("status", status)
    resp = query.execute()
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


def list_trace_events_for_case(case_id: str, limit: int = 100) -> list[dict]:
    """Trace events for one case, oldest first — the step-by-step story.

    Read-only view for the MCP operator console (app/mcp_invoice.py)."""
    client = _get_client()
    resp = (
        client.table("trace_events")
        .select("event_id, event_type, layer, data, latency_ms, error, ts")
        .eq("case_id", case_id)
        .order("ts")
        .limit(limit)
        .execute()
    )
    return resp.data or []


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


# ── system heartbeat (liveness) ──────────────────────────────────────────────
# A tiny key→timestamp table the always-on watcher stamps each poll so a separate
# process (the web app's monitor) can tell whether the watcher is still alive.

def record_heartbeat(name: str, meta: Optional[dict] = None) -> None:
    """Stamp `name`'s last-seen time as now (UTC). Upsert on the name PK."""
    from datetime import datetime, timezone

    client = _get_client()
    client.table("system_heartbeat").upsert({
        "name": name,
        "last_beat_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta or {},
    }).execute()


def get_heartbeat(name: str) -> Optional[dict]:
    """Return {name, last_beat_at, meta} for `name`, or None if never stamped."""
    client = _get_client()
    result = client.table("system_heartbeat").select("*").eq("name", name).limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


# ── chat pending-confirmation store (durable across restarts) ─────────────────
# Backs vertex_agent.chat_actions so a staged-but-unconfirmed action (send email,
# cancel, revoke) survives a web restart instead of living only in process memory.

def upsert_chat_pending(chat_user: str, kind: str, params: dict, summary: str) -> None:
    from datetime import datetime, timezone

    client = _get_client()
    client.table("chat_pending_actions").upsert({
        "chat_user": chat_user,
        "kind": kind,
        "params": params or {},
        "summary": summary or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def get_chat_pending(chat_user: str) -> Optional[dict]:
    client = _get_client()
    result = (
        client.table("chat_pending_actions").select("*")
        .eq("chat_user", chat_user).limit(1).execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def delete_chat_pending(chat_user: str) -> None:
    client = _get_client()
    client.table("chat_pending_actions").delete().eq("chat_user", chat_user).execute()


def list_chat_pending(limit: int = 20) -> list[dict]:
    """All staged-but-unconfirmed chat actions, newest first (one per chat user).

    Read-only view for the MCP operator console (app/mcp_invoice.py); params
    (which can hold drafted email bodies) are deliberately excluded."""
    client = _get_client()
    result = (
        client.table("chat_pending_actions")
        .select("chat_user, kind, summary, created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []
