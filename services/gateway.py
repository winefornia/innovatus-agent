"""
Gateway — channel normalization layer.

All inbound messages (Telegram, FastAPI /intake, Gmail) are normalized into a
NormalizedMessage before reaching invoice logic. This means adding a new channel
(Google Chat, web dashboard, etc.) requires zero changes to the invoice agent.

Usage:
    from services.gateway import gateway, NormalizedMessage

    msg = NormalizedMessage(
        user_id="tg_12345678",
        channel="telegram",
        session_id="tg_12345678",
        text="Invoice Oak Barrel for 3 cases Cab",
        raw={},
        attachments=[],
    )
    result = gateway.dispatch(msg)
    # result: {"thread_id": ..., "state": ..., "interrupt": ...}
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class NormalizedMessage:
    user_id: str        # e.g. "tg_12345678", "api_<uuid>", "gmail_<message_id>"
    channel: str        # "telegram" | "google_chat" | "api" | "gmail" | "pdf"
    session_id: str     # LangGraph thread_id key for this conversation
    text: str           # normalized message text
    raw: dict           # original platform event (for debugging)
    attachments: list   # list of {"type": "pdf", "bytes": b"..."} etc.
    sender_id: str = "" # fallback to user_id if not set

    def __post_init__(self):
        if not self.sender_id:
            self.sender_id = self.user_id


class Gateway:
    """Dispatches NormalizedMessages to the correct agent graph.

    Currently routes everything to the invoice graph. Future: tastingroom_graph
    for tasting room intents.
    """

    @staticmethod
    def _invoke_with_retry(graph, msg, case_id, config, max_retries=1):
        """Invoke the graph, retrying once on transient connection errors."""
        from db.retry import invoke_with_retry
        return invoke_with_retry(
            graph,
            {
                "raw_message": msg.text,
                "sender_id":   msg.sender_id,
                "_case_id":    case_id,
            },
            config=config,
            max_retries=max_retries,
        )

    def dispatch(self, msg: NormalizedMessage) -> dict:
        """Run the invoice graph for this message. Returns the graph result dict."""
        from agents.invoice_graph import invoice_graph
        from services.control_layer import control
        from services.guardrail_service import guardrail

        config = {"configurable": {"thread_id": msg.session_id}}

        # Open case
        case = control.begin_case(
            raw_input=msg.text,
            sender_id=msg.sender_id,
            user_id=msg.user_id,
            thread_id=msg.session_id,
        )

        # Pre-input guardrail
        gd_in = guardrail.check("pre_input",
                                {"message": msg.text, "sender_id": msg.sender_id},
                                case_id=case.case_id)
        control.log_guardrail(case, "pre_input", gd_in.rule, gd_in.passed,
                              gd_in.action, gd_in.reason or "")
        if not gd_in.passed:
            if gd_in.action == "escalate":
                control.label_failure(case, "injection_attempt", "critical",
                                      "pre_input", gd_in.reason or "", "guardrail",
                                      responsible_layer="guardrail")
            control.close_case(case, "refused", gd_in.reason or "blocked",
                               gd_in.reason or "")
            return {
                "thread_id": msg.session_id,
                "final_response": gd_in.reason or "Request blocked.",
                "blocked": True,
            }

        # Single-agent pipeline — every message goes to the invoice agent
        control.set_routing(case, intent="invoice_creation",
                            agent="invoice_agent", risk_level="medium")

        # Invoke invoice graph (retry once on transient DB errors)
        try:
            result = self._invoke_with_retry(
                invoice_graph, msg, case.case_id, config
            )
            final = result.get("final_response", "")

            # Pre-output guardrail
            from services.guardrail_service import guardrail as _g
            if final:
                gd_out = _g.check("pre_output", {"response": final},
                                  case_id=case.case_id)
                control.log_guardrail(case, "pre_output", gd_out.rule, gd_out.passed,
                                      gd_out.action, gd_out.reason or "")
                if gd_out.action == "sanitize" and gd_out.sanitized_value is not None:
                    final = gd_out.sanitized_value
                    result = {**result, "final_response": final}

            # Close case if done
            from services.invoice_interrupts import current_invoice_interrupt
            ix = current_invoice_interrupt(result)
            if result.get("square_invoice_id") or (final and not ix):
                outcome = "success" if result.get("square_invoice_id") else "completed"
                control.close_case(case, outcome, final)
                try:
                    from db.repository import write_workflow_record
                    from db.models import WorkflowRecord
                    terminal_status = _derive_terminal_status(result)
                    write_workflow_record(WorkflowRecord(
                        case_id=case.case_id,
                        bot_type="invoice",
                        business_object_type="invoice",
                        business_object_id=result.get("square_invoice_id") or "",
                        status=terminal_status,
                        summary=final[:200] if final else terminal_status.replace("_", " "),
                        external_system="square" if result.get("square_invoice_id") else "",
                        external_id=result.get("square_invoice_id") or "",
                        error_message=result.get("error") or "",
                        needs_review=bool(result.get("reconciliation_needed")),
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    ))
                except Exception as _wr_exc:
                    logging.warning("[gateway] workflow_record write failed: %s", _wr_exc)

            return {"thread_id": msg.session_id, **result}

        except Exception as e:
            logging.error("[gateway] dispatch error: %s", e, exc_info=True)
            control.label_failure(case, "graph_error", "high", "gateway", str(e), "invoice_agent")
            control.close_case(case, "failed", "", str(e))
            try:
                from db.repository import write_workflow_record
                from db.models import WorkflowRecord
                write_workflow_record(WorkflowRecord(
                    case_id=case.case_id,
                    bot_type="invoice",
                    business_object_type="invoice",
                    business_object_id="",
                    status="failed_safely",
                    summary=str(e)[:200],
                    error_message=str(e),
                    needs_review=False,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                ))
            except Exception:
                pass
            return {
                "thread_id": msg.session_id,
                "final_response": f"Something went wrong: {e}",
                "error": str(e),
            }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

gateway = Gateway()


# ---------------------------------------------------------------------------
# Terminal status derivation (pure function — testable without gateway)
# ---------------------------------------------------------------------------

def _derive_terminal_status(result: dict) -> str:
    """Map an invoice_graph result dict to one terminal WorkflowRecord status."""
    if result.get("reconciliation_needed"):
        return "needs_manual_review"
    approval = result.get("approval")
    if approval == "rejected":
        return "cancelled"
    if result.get("square_invoice_id"):
        send = result.get("send_decision", "draft")
        return "completed_sent" if send == "send" else "completed_draft_saved"
    if result.get("error"):
        return "failed_safely"
    return "needs_manual_review"


# ---------------------------------------------------------------------------
# Channel-specific NormalizedMessage factories
# ---------------------------------------------------------------------------

def from_telegram(chat_id: int, text: str) -> NormalizedMessage:
    return NormalizedMessage(
        user_id=f"tg_{chat_id}",
        channel="telegram",
        session_id=f"tg_{chat_id}",
        text=text,
        raw={"chat_id": chat_id},
        attachments=[],
    )


def from_api(message: str, sender_id: str = "api", thread_id: str | None = None) -> NormalizedMessage:
    sid = thread_id or f"intake_{uuid.uuid4().hex[:8]}"
    return NormalizedMessage(
        user_id=f"api_{sender_id}",
        channel="api",
        session_id=sid,
        text=message,
        raw={"sender_id": sender_id},
        attachments=[],
        sender_id=sender_id,
    )


def from_pdf(extracted_text: str, sender_id: str = "pdf_upload",
             thread_id: str | None = None) -> NormalizedMessage:
    sid = thread_id or f"pdf_{uuid.uuid4().hex[:8]}"
    return NormalizedMessage(
        user_id=f"api_{sender_id}",
        channel="pdf",
        session_id=sid,
        text=extracted_text,
        raw={"sender_id": sender_id},
        attachments=[],
        sender_id=sender_id,
    )
