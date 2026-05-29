"""Case judgment engine — LLM reads a CaseBundle, returns a structured CaseJudgment."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EvidenceRef(BaseModel):
    source_message_id: str
    claim: str
    evidence_type: Literal["direct", "inferred_match"] = "direct"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class Uncertainty(BaseModel):
    question: str
    why_it_matters: str
    resolution_needed: str
    severity: Literal["low", "medium", "high"]


class CurrentTruth(BaseModel):
    client_intent: Literal[
        "unknown",
        "requested_slot",
        "requested_alternative",
        "accepted_slot",
        "cancelled",
    ]
    facility_status: Literal[
        "unknown",
        "availability_requested",
        "available",
        "booking_requested",
        "confirmed",
    ]
    payment_status: Literal[
        "not_started",
        "invoice_sent",
        "paid",
        "problem",
    ]
    confirmation_status: Literal[
        "not_sent",
        "tentative_sent",
        "final_sent",
    ]


class ToolPlan(BaseModel):
    tool_name: Literal[
        "none",
        "draft_client_reply",
        "draft_josh_availability_request",
        "draft_josh_booking_request",
        "draft_invoice_message",
        "draft_final_confirmation",
        "flag_for_staff_review",
    ]
    reason: str
    requires_human_approval: bool = True


class CaseJudgment(BaseModel):
    case_summary: str = Field(
        description="1–3 sentence plain-English summary of where this case stands right now."
    )
    current_truth: CurrentTruth
    blockers: list[str] = Field(
        default_factory=list,
        description="Concrete things preventing the next action.",
    )
    risks: list[str] = Field(
        default_factory=list,
        description="Potential problems if we act now.",
    )
    uncertainties: list[Uncertainty] = Field(
        default_factory=list,
        description="Things we cannot confirm from evidence — distinct from blockers.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="0–1 overall confidence.")
    next_best_action: ToolPlan
    evidence: list[EvidenceRef] = Field(
        default_factory=list,
        description="Source-backed claims. Use 'direct' when the email explicitly names this client/slot; 'inferred_match' when you are matching by context.",
    )
    interrupt_level: Literal["none", "digest", "immediate"] = Field(
        default="none",
        description=(
            "none=no Telegram notification; "
            "digest=include in periodic summary; "
            "immediate=send approval request now."
        ),
    )


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _fallback_judgment(reason: str) -> CaseJudgment:
    return CaseJudgment(
        case_summary=f"Judgment failed: {reason}. Flagged for staff review.",
        current_truth=CurrentTruth(
            client_intent="unknown",
            facility_status="unknown",
            payment_status="not_started",
            confirmation_status="not_sent",
        ),
        blockers=[reason],
        risks=[],
        uncertainties=[],
        confidence=0.0,
        next_best_action=ToolPlan(
            tool_name="flag_for_staff_review",
            reason=reason,
            requires_human_approval=True,
        ),
        evidence=[],
        interrupt_level="none",
    )


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Innovatus tasting room case analyst. Your job is to read a reservation \
case bundle (emails, claims, events) and return a precise, source-backed judgment.

Rules:
- Only assert facts that are supported by a source_message_id visible in the bundle.
- Never invent availability, payment receipts, or confirmations.
- Distinguish evidence types carefully:
    - "direct": the email explicitly names this client, this date, or this slot.
    - "inferred_match": you are matching by context (e.g. a grouped confirmation that \
probably refers to this booking but does not name the client directly).
- Populate "uncertainties" for anything ambiguous — this is NOT the same as blockers. \
  Blockers prevent action. Uncertainties are things you cannot confirm from the evidence \
  and that a human should verify.
- Set interrupt_level:
    - "immediate" — an irreversible or reputation-sensitive action is warranted right now \
      (e.g. payment received and final confirmation is unsent, client accepted and invoice \
      needs sending).
    - "digest" — case advanced but no urgent action (e.g. new inquiry arrived, Josh replied \
      with availability — can wait for next batch review).
    - "none" — purely informational, no action needed.
- If confidence < 0.6, use "flag_for_staff_review" and "none" interrupt_level.
- "none" action is valid and preferred over acting on insufficient evidence.
"""

_USER_TEMPLATE = """\
## Incoming message type
{message_type}

## Case bundle
```json
{bundle_json}
```

Return a CaseJudgment. Be concise. Evidence refs MUST match source_message_id values \
present in the bundle's messages or claims. Do not hallucinate IDs.
"""


def judge_case(bundle: dict[str, Any], message_type: str) -> CaseJudgment:
    """Call the LLM with the full case bundle and return a structured CaseJudgment."""
    try:
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)
        structured_llm = llm.with_structured_output(CaseJudgment)

        bundle_json = json.dumps(bundle, indent=2, default=str)
        if len(bundle_json) > 24_000:
            bundle_json = bundle_json[:24_000] + "\n... [truncated]"

        result = structured_llm.invoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TEMPLATE.format(
                message_type=message_type,
                bundle_json=bundle_json,
            )},
        ])
        return result  # type: ignore[return-value]

    except Exception as exc:
        logger.exception("judge_case failed: %s", exc)
        return _fallback_judgment(str(exc))
