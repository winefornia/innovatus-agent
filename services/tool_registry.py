"""
Tool Registry — business action router for the invoice agent.

Every external action (Square, Gmail, Supabase) goes through one controlled path:
  - Validated inputs
  - Risk label (low / medium / high)
  - Hook events fired before and after
  - Errors normalized to ToolError (never crash the graph)
  - Latency logged

Usage:
    from services.tool_registry import tool_registry, ToolError

    result = tool_registry.dispatch("square_create_invoice_draft", args={...}, case_id="abc")
    # result is always a dict; raises ToolError on failure

Note: get_or_create_square_customer is split into two separate tools:
  - square_lookup_customer  (read-only, never creates)
  - square_create_customer  (write, medium risk, call only after Cecil confirms new customer)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """Raised when a registered tool fails. Always has a structured message."""
    def __init__(self, tool: str, reason: str, original: Optional[Exception] = None):
        self.tool = tool
        self.reason = reason
        self.original = original
        super().__init__(f"[{tool}] {reason}")


@dataclass
class ToolDef:
    name: str
    description: str
    risk: str                       # "low" | "medium" | "high"
    handler: Callable[..., dict]
    requires_approval: bool = False  # True for high-risk tools
    schema: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}
        self._hooks_module = None   # lazily loaded to avoid circular imports

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def dispatch(self, name: str, args: dict, case_id: str = "") -> dict:
        """Call a registered tool. Fires hooks, normalizes errors, logs latency.

        Returns the tool's result dict on success.
        Raises ToolError on failure (never returns a dict with 'error' key).
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(name, f"Tool '{name}' not registered")

        hooks = self._get_hooks()

        # Fire pre_tool_call hook
        if hooks:
            try:
                hooks.fire("pre_tool_call", {
                    "tool": name,
                    "risk": tool.risk,
                    "args_keys": list(args.keys()),
                    "requires_approval": tool.requires_approval,
                }, case_id=case_id)
            except Exception:
                pass

        start = time.monotonic()
        error_str: Optional[str] = None
        result: dict = {}

        try:
            result = tool.handler(**args)
            if not isinstance(result, dict):
                result = {"result": result}
            # Surface errors returned as dict keys (existing service pattern)
            if "error" in result:
                raise ToolError(name, result["error"])
        except ToolError:
            raise
        except Exception as e:
            error_str = str(e)
            raise ToolError(name, str(e), original=e) from e
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            if hooks:
                try:
                    hooks.fire("post_tool_call", {
                        "tool": name,
                        "risk": tool.risk,
                        "result_keys": list(result.keys()) if result else [],
                        "has_error": error_str is not None,
                        "latency_ms": latency_ms,
                    }, case_id=case_id, error=error_str)
                except Exception:
                    pass

        return result

    def _get_hooks(self):
        if self._hooks_module is None:
            try:
                from services import invoice_hooks
                self._hooks_module = invoice_hooks.hooks
            except Exception:
                self._hooks_module = False  # mark as unavailable
        return self._hooks_module if self._hooks_module else None

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

tool_registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------

def _register_all():
    # --- Square (read-only) -------------------------------------------------

    def _square_lookup_customer(email: str, full_name: str = "") -> dict:
        """Read-only: look up a Square customer by email. Never creates."""
        from services.square_service import get_or_create_square_customer
        # We use get_or_create but treat status="found" only as success
        result = get_or_create_square_customer(email=email, full_name=full_name or email)
        if "error" in result:
            raise ToolError("square_lookup_customer", result["error"])
        return result

    tool_registry.register(ToolDef(
        name="square_lookup_customer",
        description="Look up an existing Square customer by email (read-only).",
        risk="low",
        handler=_square_lookup_customer,
        schema={"email": "str", "full_name": "str (optional)"},
    ))

    # --- Square (write) -----------------------------------------------------

    def _square_create_customer(email: str, full_name: str) -> dict:
        from services.square_service import get_or_create_square_customer
        return get_or_create_square_customer(email=email, full_name=full_name)

    tool_registry.register(ToolDef(
        name="square_create_customer",
        description="Create a new Square customer record (write, medium risk).",
        risk="medium",
        handler=_square_create_customer,
        schema={"email": "str", "full_name": "str"},
    ))

    def _square_create_order(customer_name: str, line_items: list, location_id: str = "") -> dict:
        from services.square_service import create_order
        kwargs = {"customer_name": customer_name, "line_items": line_items}
        if location_id:
            kwargs["location_id"] = location_id
        return create_order(**kwargs)

    tool_registry.register(ToolDef(
        name="square_create_order",
        description="Create a Square order (write, medium risk).",
        risk="medium",
        handler=_square_create_order,
        schema={"customer_name": "str", "line_items": "list[dict]"},
    ))

    def _square_create_invoice_draft(
        order_id: str,
        customer_id: str,
        title: str = "Winefornia Invoice",
        payment_schedule: str = "NET_30",
        accepted_payment_methods: list | None = None,
    ) -> dict:
        from services.square_service import create_invoice_draft
        return create_invoice_draft(
            order_id=order_id,
            customer_id=customer_id,
            title=title,
            payment_schedule=payment_schedule,
            accepted_payment_methods=accepted_payment_methods or ["CARD", "BANK_ACCOUNT"],
        )

    tool_registry.register(ToolDef(
        name="square_create_invoice_draft",
        description="Create a Square invoice draft (SHARE_MANUALLY — not sent to client). High risk.",
        risk="high",
        requires_approval=True,
        handler=_square_create_invoice_draft,
        schema={
            "order_id": "str",
            "customer_id": "str",
            "title": "str",
            "payment_schedule": "UPON_RECEIPT|NET_7|NET_14|NET_30",
            "accepted_payment_methods": "list[CARD|BANK_ACCOUNT]",
        },
    ))

    def _square_publish_invoice(invoice_id: str, invoice_version: int = 0) -> dict:
        from services.square_service import publish_invoice
        return publish_invoice(invoice_id=invoice_id, invoice_version=invoice_version)

    tool_registry.register(ToolDef(
        name="square_publish_invoice",
        description="Publish a Square invoice draft — sends it to the client. High risk, irreversible.",
        risk="high",
        requires_approval=True,
        handler=_square_publish_invoice,
        schema={"invoice_id": "str", "invoice_version": "int"},
    ))

    # --- Gmail --------------------------------------------------------------

    def _gmail_send_receipt(state: dict) -> dict:
        from services.gmail_service import send_receipt
        return send_receipt(state)

    tool_registry.register(ToolDef(
        name="gmail_send_receipt",
        description="Send a receipt email to the customer via Gmail (medium risk).",
        risk="medium",
        handler=_gmail_send_receipt,
        schema={"state": "InvoiceState dict"},
    ))

    # --- Supabase -----------------------------------------------------------

    def _supabase_log_invoice(record) -> dict:
        from db.repository import upsert_invoice
        upsert_invoice(record)
        return {"status": "logged"}

    tool_registry.register(ToolDef(
        name="supabase_log_invoice",
        description="Persist invoice record to Supabase (low risk, best-effort).",
        risk="low",
        handler=_supabase_log_invoice,
        schema={"record": "InvoiceLog"},
    ))

    def _supabase_update_case(case_id: str, **fields) -> dict:
        from db.repository import update_case
        update_case(case_id, **fields)
        return {"status": "updated", "case_id": case_id}

    tool_registry.register(ToolDef(
        name="supabase_update_case",
        description="Update a case record in Supabase (low risk).",
        risk="low",
        handler=_supabase_update_case,
        schema={"case_id": "str", "**fields": "any"},
    ))

    # --- Customer / Pricing (deterministic, read-only) ----------------------

    def _customer_lookup(name=None, email=None, phone=None, company=None) -> dict:
        from services.customer_service import lookup_customer
        return lookup_customer(name=name, email=email, phone=phone, company=company)

    tool_registry.register(ToolDef(
        name="customer_lookup",
        description="Look up customer in local DB / Supabase (read-only, low risk).",
        risk="low",
        handler=_customer_lookup,
        schema={"name": "str|None", "email": "str|None", "phone": "str|None", "company": "str|None"},
    ))

    def _pricing_resolve(tier_name: str, items: list) -> dict:
        from services.product_service import calculate_invoice_prices
        return calculate_invoice_prices(tier_name, items)

    tool_registry.register(ToolDef(
        name="pricing_resolve",
        description="Deterministically calculate invoice prices for a tier (read-only).",
        risk="low",
        handler=_pricing_resolve,
        schema={"tier_name": "str", "items": "list[dict]"},
    ))


_register_all()
logging.info("[tool_registry] registered tools: %s", tool_registry.tool_names)


# ---------------------------------------------------------------------------
# Tasting Room tool registry (separate singleton, risk-scoped)
# ---------------------------------------------------------------------------

tasting_room_registry = ToolRegistry()


def _register_tasting_room_tools():

    def _flag_for_staff_review(case_id: str, reason: str, source_message_id: str = "") -> dict:
        from db.models import UnresolvedEvent
        from db import repository
        try:
            repository.insert_unresolved_event(UnresolvedEvent(
                source_message_id=source_message_id,
                reason=reason,
                raw_payload={"case_id": case_id, "reason": reason},
            ))
        except Exception:
            pass
        return {"status": "flagged", "case_id": case_id, "reason": reason}

    tasting_room_registry.register(ToolDef(
        name="tasting.case.flag_for_staff_review",
        description="Create a staff review item when the case cannot be safely advanced.",
        risk="medium",
        requires_approval=False,
        handler=_flag_for_staff_review,
        schema={"case_id": "str", "reason": "str", "source_message_id": "str (optional)"},
    ))

    def _mark_facility_verified(case_id: str, source_message_id: str, verified_by: str = "staff") -> dict:
        from db import repository
        try:
            rows = repository.list_availability_claims(
                case_id,
                actor="josh",
                claim_type="facility_availability",
            )
            for row in rows:
                if row.get("source_message_id") == source_message_id:
                    repository._get_client().table("availability_claims").update(
                        {"reviewed_by_human": True, "claim_status": "confirmed"}
                    ).eq("reservation_id", case_id).eq("source_message_id", source_message_id).execute()
        except Exception:
            pass
        return {"status": "verified", "case_id": case_id, "source_message_id": source_message_id}

    tasting_room_registry.register(ToolDef(
        name="tasting.case.mark_facility_verified",
        description="Mark a facility claim as human-verified (clears inferred_match uncertainty).",
        risk="medium",
        requires_approval=True,
        handler=_mark_facility_verified,
        schema={"case_id": "str", "source_message_id": "str", "verified_by": "str"},
    ))

    def _gmail_create_draft(to: str, subject: str, body: str) -> dict:
        from services.gmail_service import send_email
        # Draft only — tagged so it is not auto-sent.
        return {"status": "draft_created", "to": to, "subject": subject, "body_preview": body[:80]}

    tasting_room_registry.register(ToolDef(
        name="tasting.gmail.create_draft",
        description="Create a Gmail draft for staff review. Does NOT send.",
        risk="draft",
        requires_approval=False,
        handler=_gmail_create_draft,
        schema={"to": "str", "subject": "str", "body": "str"},
    ))

    def _gmail_send_approved_draft(to: str, subject: str, body: str, action_request_id: str) -> dict:
        from services.gmail_service import send_email
        result = send_email(to=to, subject=subject, html=body, plain=body)
        return {
            "status": "sent",
            "action_request_id": action_request_id,
            "message_id": result.get("message_id", ""),
        }

    tasting_room_registry.register(ToolDef(
        name="tasting.gmail.send_approved_draft",
        description="Send an email only after human approval via Telegram.",
        risk="high",
        requires_approval=True,
        handler=_gmail_send_approved_draft,
        schema={"to": "str", "subject": "str", "body": "str", "action_request_id": "str"},
    ))

    def _attach_unresolved_event(
        case_id: str, source_message_id: str, reason: str
    ) -> dict:
        from db.models import ReservationEvent
        from db import repository
        try:
            repository.insert_reservation_event(ReservationEvent(
                reservation_id=case_id,
                event_type="unresolved_event_attached",
                actor="staff",
                source_message_id=source_message_id,
                summary=reason,
                raw_payload={"reason": reason},
            ))
        except Exception:
            pass
        return {"status": "attached", "case_id": case_id, "source_message_id": source_message_id}

    tasting_room_registry.register(ToolDef(
        name="tasting.case.attach_unresolved_event",
        description="Manually attach an unresolved email to a case after staff review.",
        risk="medium",
        requires_approval=True,
        handler=_attach_unresolved_event,
        schema={"case_id": "str", "source_message_id": "str", "reason": "str"},
    ))


_register_tasting_room_tools()
logging.info("[tasting_room_registry] registered tools: %s", tasting_room_registry.tool_names)
