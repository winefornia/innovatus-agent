"""
Control Layer — case lifecycle, trace logging, risk classification, failure labeling.

Wraps every agent run as a Case. Sits outside all agent logic — it never invokes
agents, parses messages, or makes domain decisions. It only observes, logs, and gates.

Usage:
    from services.control_layer import control

    case = control.begin_case(text, sender_id, user_id, thread_id)
    control.set_routing(case, intent="invoice_creation", agent="invoice_agent", risk_level="medium")
    # ... agent runs ...
    control.close_case(case, "success", final_response)

All DB writes are best-effort (wrapped in try/except). A DB outage never crashes the agent.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from db.models import Case, FailureLabel, TraceEvent, EvalCase


# ---------------------------------------------------------------------------
# Control Layer
# ---------------------------------------------------------------------------

_STATUS_FOR_OUTCOME: dict[str, str] = {
    "success":   "completed",
    "completed": "completed",
    "refused":   "completed",
    "failed":    "failed",
    "failure":   "failed",
    "escalated": "escalated",
    "rejected":  "completed",
}


class ControlLayer:
    """Supervises agent runs: traces, guards, labels failures, creates eval cases."""

    def __init__(self) -> None:
        # In-memory case registry so graph nodes can look up the active Case
        # by case_id (a plain string safe to store in LangGraph state).
        self._active_cases: dict[str, "Case"] = {}

    def get_case(self, case_id: str) -> "Optional[Case]":
        """Return the active Case for case_id, or None if not found."""
        return self._active_cases.get(case_id)

    def _new_id(self) -> str:
        return uuid.uuid4().hex

    # -- Case lifecycle -------------------------------------------------------

    def begin_case(
        self,
        raw_input: str,
        sender_id: str,
        user_id: str,
        thread_id: str,
    ) -> Case:
        """Open a new case. Returns the Case object immediately; DB write is async."""
        case = Case(
            case_id=self._new_id(),
            sender_id=sender_id,
            user_id=user_id,
            thread_id=thread_id,
            raw_input=raw_input[:2000],   # cap to avoid huge DB rows
        )
        self._active_cases[case.case_id] = case  # register for node lookups
        try:
            from db.repository import insert_case
            insert_case(case)
        except Exception as e:
            logging.debug("[control] begin_case DB write failed: %s", e)

        self._trace(case.case_id, "input_received", "control",
                    {"input_length": len(raw_input), "sender_id": sender_id})
        return case

    def set_routing(self, case: Case, intent: str, agent: str, risk_level: str) -> None:
        """Record the agent/intent/risk for this case."""
        case.intent     = intent
        case.agent      = agent
        case.risk_level = risk_level
        try:
            from db.repository import update_case
            update_case(case.case_id,
                        intent=intent,
                        agent=agent,
                        risk_level=risk_level)
        except Exception as e:
            logging.debug("[control] set_routing DB update failed: %s", e)

        self._trace(case.case_id, "intent_classified", "control", {
            "intent":     intent,
            "agent":      agent,
            "risk_level": risk_level,
        })

    def close_case(
        self,
        case: Case,
        outcome: str,
        final_response: str,
        error_summary: str = "",
    ) -> None:
        """Close the case with a final outcome."""
        case.status         = _STATUS_FOR_OUTCOME.get(outcome, "completed")
        case.outcome        = outcome
        case.final_response = final_response
        case.error_summary  = error_summary

        try:
            from db.repository import update_case
            update_case(case.case_id,
                        status=case.status,
                        outcome=outcome,
                        final_response=final_response[:2000] if final_response else "",
                        error_summary=error_summary[:500] if error_summary else "",
                        closed_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logging.debug("[control] close_case DB update failed: %s", e)

        self._active_cases.pop(case.case_id, None)  # free memory after close

        self._trace(case.case_id, "output_generated", "invoice_agent", {
            "outcome": outcome,
            "response_length": len(final_response) if final_response else 0,
            "has_error": bool(error_summary),
        })

        # Background: synthesize skill facts from completed cases
        if outcome in ("success", "completed"):
            self._synthesize_skills(case)

    # -- Trace logging --------------------------------------------------------

    def _trace(
        self,
        case_id: str,
        event_type: str,
        layer: str,
        data: dict,
        latency_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Write a trace event (best-effort)."""
        event = TraceEvent(
            event_id=self._new_id(),
            case_id=case_id,
            event_type=event_type,
            layer=layer,
            data=data,
            latency_ms=latency_ms,
            error=error,
        )
        try:
            from db.repository import insert_trace_event
            insert_trace_event(event)
        except Exception as e:
            logging.debug("[control] trace write failed: %s", e)

    def log_tool_call(
        self,
        case: Case,
        tool_name: str,
        args: dict,
        result: dict,
        latency_ms: int,
        error: Optional[str] = None,
    ) -> None:
        """Log a tool call and its result as two trace events."""
        self._trace(case.case_id, "tool_call", "square", {
            "tool": tool_name,
            "args_keys": list(args.keys()) if args else [],
        })
        self._trace(case.case_id, "tool_result", "square", {
            "tool":       tool_name,
            "result_keys": list(result.keys()) if result else [],
            "has_error":  "error" in (result or {}),
        }, latency_ms=latency_ms, error=error)

    def log_interrupt(self, case: Case, interrupt_type: str, payload: dict) -> None:
        """Log a LangGraph interrupt (human-in-the-loop checkpoint)."""
        self._trace(case.case_id, "interrupt_issued", "invoice_agent", {
            "interrupt_type": interrupt_type,
            "payload_keys": list(payload.keys()) if payload else [],
        })

    def log_human_decision(
        self,
        case: Case,
        interrupt_type: str,
        decision: str,
        raw_resume: str,
    ) -> None:
        """Log a human decision at a HITL checkpoint."""
        self._trace(case.case_id, "human_decision", "human", {
            "interrupt_type": interrupt_type,
            "decision":       decision,
            "raw_resume_len": len(raw_resume) if raw_resume else 0,
        })

    def log_guardrail(self, case: Case, stage: str, rule: str, passed: bool, action: str, reason: str = "") -> None:
        """Log a guardrail check outcome."""
        self._trace(case.case_id, "guardrail_check", "guardrail", {
            "stage":  stage,
            "rule":   rule,
            "passed": passed,
            "action": action,
            "reason": reason,
        })

    # -- Failure labeling -----------------------------------------------------

    def label_failure(
        self,
        case: Case,
        failure_type: str,
        severity: str,
        source: str,
        description: str,
        suggested_patch: str,
        responsible_layer: str = "invoice_agent",
        confidence: float = 1.0,
    ) -> FailureLabel:
        """Create a failure label for this case and persist it."""
        label = FailureLabel(
            failure_id=self._new_id(),
            case_id=case.case_id,
            failure_type=failure_type,
            severity=severity,
            source=source,
            responsible_layer=responsible_layer,
            description=description[:500],
            suggested_patch=suggested_patch,
            confidence=confidence,
        )
        try:
            from db.repository import insert_failure_label
            insert_failure_label(label)
        except Exception as e:
            logging.debug("[control] label_failure DB write failed: %s", e)

        self._trace(case.case_id, "failure", "guardrail", {
            "failure_type": failure_type,
            "severity":     severity,
            "source":       source,
        }, error=description[:200])

        logging.warning("[control] FAILURE case=%s type=%s severity=%s source=%s",
                        case.case_id, failure_type, severity, source)

        # Async patch proposal — fires in background, never blocks the agent
        self._trigger_patch_proposal(label, case)

        return label

    def _synthesize_skills(self, case: Case) -> None:
        """Background: extract 1–2 skill facts from a completed invoice case via Claude Haiku."""
        import threading

        def _run():
            try:
                from services.skill_service import skill_service
                skill_service.synthesize_from_case(case, user_id=case.user_id)
            except Exception as e:
                logging.debug("[control] skill synthesis failed: %s", e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _trigger_patch_proposal(self, failure: FailureLabel, case: Case) -> None:
        """Background: ask the LLM to propose a fix for this failure.

        Low/medium severity → propose + auto-apply (if evals pass).
        High/critical       → propose only, write to db/patches/ for human review.
        """
        import threading

        def _run():
            try:
                from services.patch_service import patch_service
                proposal = patch_service.propose(failure, case)
                if proposal and failure.severity in ("low", "medium"):
                    patch_service.apply_and_verify(proposal)
            except Exception as e:
                logging.debug("[control] patch proposal failed: %s", e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # -- Failure-to-eval loop -------------------------------------------------

    def create_eval_case(self, failure: FailureLabel, case: Case) -> EvalCase:
        """Convert a production failure into a regression eval case.

        Writes JSON to db/eval_cases/{eval_id}.json and links back to failure_labels.
        """
        import os, json
        from pathlib import Path

        eval_id = f"reg_{failure.failure_type}_{self._new_id()[:8]}"
        eval_case = EvalCase(
            eval_id=eval_id,
            case_id=case.case_id,
            source="production_failure",
            input=case.raw_input,
            expected_intent=case.intent or "",
            expected_agent=case.agent or "",
            risk_level=case.risk_level,
            tags=["regression", failure.failure_type],
        )

        try:
            cases_dir = Path(__file__).parent.parent / "db" / "eval_cases"
            cases_dir.mkdir(exist_ok=True)
            path = cases_dir / f"{eval_id}.json"
            with open(path, "w") as f:
                d = {
                    "eval_id":                 eval_case.eval_id,
                    "case_id":                 eval_case.case_id,
                    "source":                  eval_case.source,
                    "input":                   eval_case.input,
                    "expected_intent":         eval_case.expected_intent,
                    "expected_agent":          eval_case.expected_agent,
                    "should_reach_node":       eval_case.should_reach_node,
                    "expected_output_contains": eval_case.expected_output_contains,
                    "should_not_contain":      eval_case.should_not_contain,
                    "risk_level":              eval_case.risk_level,
                    "tags":                    eval_case.tags,
                    "failure_type":            failure.failure_type,
                    "failure_description":     failure.description,
                    "suggested_patch":         failure.suggested_patch,
                }
                json.dump(d, f, indent=2)

            # Link back to failure_labels
            from db.repository import update_failure_eval_case
            update_failure_eval_case(failure.failure_id, eval_id)
            eval_case.eval_case_id = eval_id  # type: ignore[attr-defined]

            logging.info("[control] eval case created: %s → %s", failure.failure_type, path)
        except Exception as e:
            logging.warning("[control] create_eval_case failed: %s", e)

        return eval_case


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly
# ---------------------------------------------------------------------------

control = ControlLayer()
