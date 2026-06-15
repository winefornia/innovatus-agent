"""Shared invoice graph interrupt detection.

UI adapters use this to decide whether a graph state is waiting for human input.
Keep it independent from any channel-specific rendering code.
"""

from __future__ import annotations


def current_invoice_interrupt(state: dict | None) -> str | None:
    """Return the active human-input checkpoint for an invoice graph state."""
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
    if (
        state.get("edit_instruction")
        and not state.get("approval")
        and state.get("invoice_preview")
        and not state.get("square_invoice_id")
    ):
        changes = (state.get("edit_patch") or {}).get("field_changes", [])
        if any(change.get("confidence", 1.0) < 0.80 for change in changes):
            return "edit_clarification"
    if state.get("invoice_preview") and not state.get("approval") and not state.get("square_invoice_id"):
        return "approval"
    if state.get("square_invoice_id") and not state.get("send_decision"):
        return "send"
    if state.get("send_decision") == "send" and not state.get("email_receipt_decision"):
        return "email"
    return None
