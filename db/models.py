"""Database models for invoice logging, reservations, and the control layer."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional


@dataclass
class InvoiceLog:
    """Persistent log of every invoice thread, mirroring the invoice_logs table."""

    thread_id: str = ""
    sender_id: Optional[str] = None
    raw_message: Optional[str] = None
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    tier_name: Optional[str] = None
    line_items: list[dict] = field(default_factory=list)
    subtotal_cents: Optional[int] = None
    discount_cents: Optional[int] = None
    total_before_tax_cents: Optional[int] = None
    shipping_cents: Optional[int] = None
    payment_schedule: Optional[str] = None
    payment_methods: list[str] = field(default_factory=list)
    approval: Optional[Literal["approved", "rejected", "edit_requested"]] = None
    square_order_id: Optional[str] = None
    square_invoice_id: Optional[str] = None
    square_invoice_url: Optional[str] = None
    errors: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tasting room reservation models
# ---------------------------------------------------------------------------

@dataclass
class Reservation:
    """Canonical tasting room reservation case."""

    reservation_id: str
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    phone: Optional[str] = None
    requested_date: Optional[str] = None
    requested_time: Optional[str] = None
    guest_count: Optional[int] = None
    experience_type: Optional[str] = None
    price_per_person_cents: Optional[int] = None
    current_state: str = "REQUEST_RECEIVED"
    payment_status: str = "not_sent"
    booking_status: str = "not_booked"
    gmail_thread_ids: list[str] = field(default_factory=list)
    active_slot: dict = field(default_factory=dict)
    candidate_slots: list[dict] = field(default_factory=list)
    recommended_action: Optional[str] = None
    confidence: float = 1.0
    notes: Optional[str] = None


@dataclass
class AvailabilityClaim:
    """An email- or Telegram-derived claim about availability or booking."""

    reservation_id: str
    actor: str
    claim_type: str
    claim_status: str
    actor_email: Optional[str] = None
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    time_description: Optional[str] = None
    guest_count: Optional[int] = None
    experience_type: Optional[str] = None
    source_channel: str = "email"
    source_message_id: Optional[str] = None
    raw_text: Optional[str] = None
    confidence: float = 1.0
    expires_at: Optional[str] = None
    reviewed_by_human: bool = False


@dataclass
class ReservationEvent:
    """Audit event attached to a reservation."""

    reservation_id: str
    event_type: str
    actor: Optional[str] = None
    source_channel: str = "email"
    source_message_id: Optional[str] = None
    summary: Optional[str] = None
    raw_payload: dict = field(default_factory=dict)


@dataclass
class ReservationActionRequest:
    """Pending human approval for a tasting room action."""

    action_id: str
    reservation_id: str
    action_type: str
    status: str = "pending"
    risk_level: str = "medium"
    recipient_email: Optional[str] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    recommendation: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_message_id: Optional[str] = None
    source_message_id: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Control Layer models
# ---------------------------------------------------------------------------

@dataclass
class Case:
    """One row in agent_cases — lifecycle of a single user intent."""
    case_id: str
    sender_id: str
    user_id: str
    thread_id: str
    raw_input: str
    intent: str = ""
    agent: str = ""
    risk_level: str = "low"          # low | medium | high | critical
    status: str = "running"          # running | completed | failed | escalated | abandoned
    final_response: str = ""
    outcome: str = ""                # success | failure | rejected | escalated | refused
    error_summary: str = ""


@dataclass
class TraceEvent:
    """One row in trace_events — a single observable event within a case."""
    event_id: str
    case_id: str
    event_type: str                  # input_received | intent_classified | guardrail_check
                                     # | tool_call | tool_result | interrupt_issued
                                     # | human_decision | output_generated | failure
    layer: str                       # supervisor | invoice_agent | guardrail | human | square | llm
    data: dict = field(default_factory=dict)
    latency_ms: Optional[int] = None
    error: Optional[str] = None


@dataclass
class GuardrailDecision:
    """Result of a single guardrail check."""
    guardrail_id: str
    case_id: str
    stage: str                       # pre_input | pre_model | pre_tool | post_tool | pre_output
    rule: str
    passed: bool
    action: str                      # allow | refuse | sanitize | escalate
    reason: Optional[str] = None
    sanitized_value: Optional[str] = None   # populated when action == "sanitize"


@dataclass
class FailureLabel:
    """One row in failure_labels — a labeled failure linked to a case."""
    failure_id: str
    case_id: str
    failure_type: str
    severity: str                    # low | medium | high | critical
    source: str                      # which node / service
    responsible_layer: str
    description: str
    suggested_patch: str             # prompt | tool | guardrail | schema | routing | workflow
    confidence: float = 1.0
    eval_case_id: Optional[str] = None


@dataclass
class RawEmailEvent:
    """Raw inbound email — stored before any processing for replay."""

    gmail_message_id: str
    gmail_thread_id: str = ""
    subject: str = ""
    from_email: str = ""
    to_email: str = ""
    body: str = ""
    raw_payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ValidationResultRecord:
    """Audit record of what the safety guards allowed or blocked."""

    case_id: str
    tool_name: str
    allowed: bool
    source_message_id: str = ""
    judgment_record_id: str = ""
    block_reason: str = ""
    guardrails_triggered: list = field(default_factory=list)
    approval_required: bool = True
    interrupt_level: str = "none"
    result_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ExecutionResultRecord:
    """Structured result of a tool execution attempt."""

    case_id: str
    tool_name: str
    ok: bool
    action_request_id: str = ""
    result_json: dict = field(default_factory=dict)
    error_type: str = ""
    error_message: str = ""
    created_resource_id: str = ""
    result_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class UnresolvedEvent:
    """An inbound email that could not be confidently matched to a reservation."""

    source_message_id: str
    gmail_thread_id: str = ""
    subject: str = ""
    from_email: str = ""
    message_type: str = "unclassified"
    reason: str = ""
    raw_payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class WorkflowRecord:
    """Terminal business outcome for one completed workflow run.

    Written by gateway.py after every case closes. The single source of truth
    for "what did the bot actually do?" — used by the activity page, staging evals,
    and operator reconciliation.

    Terminal statuses:
      completed_draft_saved           — Square draft created, kept as draft
      completed_sent                  — Square invoice published and sent to client
      completed_reservation_approved  — tasting room booking confirmed
      completed_reservation_declined  — reservation declined or cancelled
      cancelled                       — user rejected or bot declined to proceed
      failed_safely                   — error occurred, no external state changed
      needs_manual_review             — partial success or reconciliation required
    """
    case_id: str
    bot_type: str                   # "invoice" | "tastingroom"
    business_object_type: str       # "invoice" | "reservation"
    business_object_id: str         # square_invoice_id or reservation_id
    status: str                     # terminal status (see docstring)
    summary: str                    # one-line human description
    created_at: str = ""            # ISO timestamp (set by DB default if blank)
    completed_at: str = ""
    external_system: str = ""       # "square" | "gmail" | ""
    external_id: str = ""           # Square invoice_id or Gmail thread_id
    error_message: str = ""
    needs_review: bool = False
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class EvalCase:
    """A single eval case — can be golden, regression, adversarial, or edge."""
    eval_id: str
    source: str                      # production_failure | manual | golden
    input: str
    expected_intent: str
    expected_agent: str
    should_reach_node: Optional[str] = None
    expected_output_contains: list = field(default_factory=list)
    should_not_contain: list = field(default_factory=list)
    risk_level: str = "low"
    tags: list = field(default_factory=list)     # ["regression", "golden", "adversarial"]
    case_id: Optional[str] = None               # linked production case if from failure
