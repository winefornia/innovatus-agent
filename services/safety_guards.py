"""Safety guards — hard rules for irreversible tasting room actions."""

from __future__ import annotations

from services.case_judge import CaseJudgment, ToolPlan


def validate_plan(judgment: CaseJudgment) -> tuple[bool, str]:
    """Check whether the judgment's next_best_action is safe to execute.

    Returns:
        (allowed, reason) — if not allowed, reason explains the block.
    """
    action = judgment.next_best_action.tool_name
    truth = judgment.current_truth

    # Low-confidence catch-all.
    if judgment.confidence < 0.6 and action not in ("none", "flag_for_staff_review"):
        return (
            False,
            f"Confidence {judgment.confidence:.2f} is below 0.6 — action blocked, flag for staff.",
        )

    if action == "draft_final_confirmation":
        if truth.payment_status != "paid":
            return (
                False,
                f"Cannot draft final confirmation: payment_status is '{truth.payment_status}', expected 'paid'.",
            )
        if truth.facility_status != "confirmed":
            return (
                False,
                f"Cannot draft final confirmation: facility_status is '{truth.facility_status}', expected 'confirmed'.",
            )
        # Block if facility confirmation rests entirely on weak inferred evidence.
        facility_keywords = ("facility", "josh", "confirmed", "booking", "venue")
        weak_inferred = [
            e for e in judgment.evidence
            if e.evidence_type == "inferred_match"
            and e.confidence < 0.85
            and any(kw in e.claim.lower() for kw in facility_keywords)
        ]
        if weak_inferred:
            return (
                False,
                "Facility confirmation is inferred (confidence < 0.85). "
                "Staff must verify before final confirmation.",
            )

    if action == "draft_invoice_message":
        if truth.client_intent != "accepted_slot":
            return (
                False,
                f"Cannot send invoice: client_intent is '{truth.client_intent}', expected 'accepted_slot'.",
            )

    if action == "draft_josh_booking_request":
        if truth.facility_status not in ("available",):
            return (
                False,
                f"Cannot request Josh booking: facility_status is '{truth.facility_status}', expected 'available'.",
            )

    return (True, "ok")


def downgrade_to_flag(judgment: CaseJudgment, reason: str) -> CaseJudgment:
    """Return a copy of the judgment with next_best_action downgraded to flag_for_staff_review."""
    return judgment.model_copy(update={
        "next_best_action": ToolPlan(
            tool_name="flag_for_staff_review",
            reason=reason,
            requires_human_approval=True,
        ),
        "interrupt_level": "none",
        "blockers": judgment.blockers + [reason],
    })
