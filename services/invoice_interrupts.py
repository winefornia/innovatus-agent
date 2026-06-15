"""Shared invoice graph interrupt detection.

UI adapters (Telegram, Google Chat) use this to decide whether the graph is
waiting for human input and which checkpoint it's at.

The reliable signal is the ACTUAL interrupt payload emitted by interrupt({...})
in the graph — every payload carries a "type". We map those types to the short
canonical names the adapters render/route on. This replaces the old approach of
reverse-engineering the checkpoint by guessing from state fields, which silently
broke whenever an interrupt fired for a reason the heuristic didn't model (e.g.
ask_missing_fields firing on low extraction confidence with no missing fields).

Accepts either:
  - an invoke() result dict (the pending interrupt is under "__interrupt__"), or
  - a LangGraph StateSnapshot (pending interrupts under .interrupts).
"""

from __future__ import annotations

# interrupt payload "type" → canonical name used by adapters/render.
_INTERRUPT_TYPE_MAP: dict[str, str] = {
    "missing_fields":                "missing",
    "confirm_customer":              "confirm_customer",
    "tier_and_payment_confirmation": "tier",
    "price_confirmation":            "price_confirmation",
    "invoice_approval_required":     "approval",
    "confirm_send_to_client":        "send",
    "offer_email_receipt":           "email",
    "edit_instruction":              "edit_instruction",
    "edit_clarification":            "edit_clarification",
}

# Interrupts answered by a typed text reply (vs. a button/card click). Adapters
# resume these with the raw message text.
TEXT_INPUT_INTERRUPTS = frozenset(
    {"missing", "edit_instruction", "edit_clarification", "price_confirmation"}
)


def interrupt_payload(obj) -> dict | None:
    """Return the first pending interrupt payload dict from a result or snapshot.

    Works across checkpointers: an invoke() result carries "__interrupt__";
    a StateSnapshot may expose .interrupts (MemorySaver) and/or only populate
    .tasks[*].interrupts (PostgresSaver) — we check all of them.
    """
    if obj is None:
        return None
    interrupts = None
    if isinstance(obj, dict):
        interrupts = obj.get("__interrupt__")
    else:
        interrupts = getattr(obj, "interrupts", None)
        if not interrupts:
            collected = []
            for task in (getattr(obj, "tasks", None) or ()):
                collected.extend(getattr(task, "interrupts", None) or ())
            interrupts = collected
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return value if isinstance(value, dict) else None


def current_invoice_interrupt(obj) -> str | None:
    """Return the active human-input checkpoint name, or None.

    Reads the real interrupt payload type first; falls back to legacy state-field
    inference only when no payload is present (e.g. a caller passed snapshot.values
    instead of the snapshot or result).
    """
    payload = interrupt_payload(obj)
    if payload:
        return _INTERRUPT_TYPE_MAP.get(payload.get("type"))
    return _infer_from_state(obj) if isinstance(obj, dict) else None


def _infer_from_state(state: dict | None) -> str | None:
    """Legacy fallback: best-effort inference from state fields."""
    if not state:
        return None
    if state.get("missing_fields"):
        return "missing"
    if state.get("customer") and state.get("customer_confirmed") is False:
        return "confirm_customer"
    if state.get("customer") and not state.get("tier_name"):
        return "tier"
    if state.get("awaiting_price") and not state.get("invoice_preview"):
        return "price_confirmation"
    if state.get("approval") == "edit_requested" and state.get("invoice_preview"):
        return "edit_instruction"
    if state.get("invoice_preview") and not state.get("approval") and not state.get("square_invoice_id"):
        return "approval"
    if state.get("square_invoice_id") and not state.get("send_decision"):
        return "send"
    if state.get("send_decision") == "send" and not state.get("email_receipt_decision"):
        return "email"
    return None
