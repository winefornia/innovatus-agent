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
    # ask_missing_fields also pauses on low extraction confidence even when no
    # field is literally missing (e.g. a PDF-extracted order that parsed but
    # scored < 0.75). While paused there the graph has run extraction but not yet
    # resolved a customer — detect it so the UI surfaces the clarifying question
    # instead of rendering a terminal "Done." over a graph that is still waiting.
    if (
        state.get("extracted")
        and state.get("extraction_confidence", 1.0) < 0.75
        and not state.get("customer")
        and not state.get("invoice_preview")
        and not state.get("square_invoice_id")
    ):
        return "missing"
    if state.get("customer") and state.get("customer_confirmed") is False:
        return "confirm_customer"
    if state.get("customer") and not state.get("tier_name"):
        return "tier"
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


def clarifying_question(state: dict | None) -> str | None:
    """Return the focused question from a paused ask_missing_fields interrupt.

    The question lives in the LangGraph interrupt payload (``__interrupt__``),
    not in the state channels, so renderers can't read it off ``missing_fields``.
    Returns None when there is no such interrupt (e.g. resumed/snapshot states
    that don't carry ``__interrupt__``).
    """
    if not state:
        return None
    for it in state.get("__interrupt__") or ():
        val = getattr(it, "value", None)
        if val is None and isinstance(it, dict):
            val = it.get("value")
        if isinstance(val, dict) and val.get("type") == "missing_fields" and val.get("question"):
            return val["question"]
    return None
