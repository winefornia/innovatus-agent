"""
Invoice Hooks — lightweight lifecycle event bus.

Replaces scattered trace/log calls with clean pre/post wrappers.
Inspired by Hermes plugin/hook pattern.

Events:
    pre_llm_call       → before any LLM call (prompt, model, context)
    post_llm_call      → after LLM call (extracted fields, confidence, latency)
    pre_tool_call      → before tool_registry.dispatch (tool name, risk, args)
    post_tool_call     → after tool_registry.dispatch (result, latency, error)
    on_interrupt       → LangGraph interrupt issued (type, payload keys)
    on_human_decision  → human resumes a LangGraph interrupt (decision, raw value)
    on_case_failure    → failure labeled (triggers patch_service in background)
    on_case_close      → case completed (triggers skill synthesis in background)

Usage:
    from services.invoice_hooks import hooks

    hooks.fire("pre_llm_call", {"model": "claude-haiku-4-5-20251001", "prompt_len": 400}, case_id="abc")
    hooks.fire("post_llm_call", {"confidence": 0.82, "fields": ["customer_name", "items"]}, case_id="abc")

Registering a subscriber:
    hooks.register("pre_tool_call", my_fn)   # fn(event, data, case_id, error)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Optional


EventHandler = Callable[[str, dict, str, Optional[str]], None]


class InvoiceHooks:
    """Simple synchronous event bus. All handlers are called in registration order.

    Handlers must not raise — exceptions are caught and logged.
    """

    def __init__(self):
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def register(self, event: str, handler: EventHandler) -> None:
        self._handlers[event].append(handler)

    def fire(
        self,
        event: str,
        data: dict,
        case_id: str = "",
        error: Optional[str] = None,
    ) -> None:
        for handler in self._handlers.get(event, []):
            try:
                handler(event, data, case_id, error)
            except Exception as e:
                logging.debug("[hooks] handler error for event=%s: %s", event, e)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

hooks = InvoiceHooks()


# ---------------------------------------------------------------------------
# Default subscribers — wired to ControlLayer trace events
# ---------------------------------------------------------------------------

def _register_control_layer_subscribers():
    """Wire ControlLayer._trace() calls as hook subscribers.

    This replaces manually calling control._trace() at each site.
    The tool_registry fires pre/post_tool_call automatically.
    LLM call sites fire pre/post_llm_call manually.
    """

    def _on_pre_llm(event, data, case_id, error):
        try:
            from services.control_layer import control
            case = control.get_case(case_id)
            if case:
                control._trace(case_id, "llm_call_start", "invoice_agent", data)
        except Exception:
            pass

    def _on_post_llm(event, data, case_id, error):
        try:
            from services.control_layer import control
            case = control.get_case(case_id)
            if case:
                control._trace(case_id, "llm_call_end", "invoice_agent", data,
                               latency_ms=data.get("latency_ms"), error=error)
        except Exception:
            pass

    def _on_pre_tool(event, data, case_id, error):
        try:
            from services.control_layer import control
            case = control.get_case(case_id)
            if case:
                control._trace(case_id, "tool_call", data.get("tool", "unknown"), {
                    "tool": data.get("tool"),
                    "risk": data.get("risk"),
                    "args_keys": data.get("args_keys", []),
                })
        except Exception:
            pass

    def _on_post_tool(event, data, case_id, error):
        try:
            from services.control_layer import control
            case = control.get_case(case_id)
            if case:
                control._trace(case_id, "tool_result", data.get("tool", "unknown"), {
                    "tool": data.get("tool"),
                    "result_keys": data.get("result_keys", []),
                    "has_error": data.get("has_error", False),
                }, latency_ms=data.get("latency_ms"), error=error)
        except Exception:
            pass

    def _on_interrupt(event, data, case_id, error):
        try:
            from services.control_layer import control
            case = control.get_case(case_id)
            if case:
                control.log_interrupt(case, data.get("interrupt_type", ""), data)
        except Exception:
            pass

    def _on_human_decision(event, data, case_id, error):
        try:
            from services.control_layer import control
            case = control.get_case(case_id)
            if case:
                control.log_human_decision(
                    case,
                    data.get("interrupt_type", ""),
                    data.get("decision", ""),
                    data.get("raw_resume", ""),
                )
        except Exception:
            pass

    hooks.register("pre_llm_call",      _on_pre_llm)
    hooks.register("post_llm_call",     _on_post_llm)
    hooks.register("pre_tool_call",     _on_pre_tool)
    hooks.register("post_tool_call",    _on_post_tool)
    hooks.register("on_interrupt",      _on_interrupt)
    hooks.register("on_human_decision", _on_human_decision)


_register_control_layer_subscribers()
