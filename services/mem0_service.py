"""
Mem0 memory service — per-user persistent memory for the Winefornia agent.

Stores:
  - Past customers and their tiers
  - Common workflows and user preferences
  - Event and account context

All operations are best-effort: failures are logged but never crash the agent.
"""

import logging
from typing import Optional

_client = None
_initialized = False


def _get_client():
    """Lazy-initialize the Mem0 MemoryClient."""
    global _client, _initialized
    if _initialized:
        return _client

    _initialized = True
    try:
        from mem0 import MemoryClient
        from app.config import MEM0_API_KEY

        if not MEM0_API_KEY:
            logging.info("[mem0] MEM0_API_KEY not set — memory layer disabled")
            return None

        _client = MemoryClient(api_key=MEM0_API_KEY)
        logging.info("[mem0] MemoryClient initialized")
        return _client

    except ImportError:
        logging.info("[mem0] mem0ai not installed — memory layer disabled")
        return None
    except Exception as e:
        logging.warning(f"[mem0] Failed to initialize: {e}")
        return None


def get_memory_context(user_id: str, query: str, top_k: int = 5) -> str:
    """Search Mem0 for memories relevant to this query. Returns formatted string."""
    client = _get_client()
    if not client:
        return ""

    try:
        results = client.search(query=query, user_id=user_id, limit=top_k)
        if not results:
            return ""

        lines = []
        for r in results:
            mem = r.get("memory", "")
            if mem:
                lines.append(f"- {mem}")

        return "\n".join(lines) if lines else ""

    except Exception as e:
        logging.debug(f"[mem0] search failed: {e}")
        return ""


def save_memory(user_id: str, content: str, metadata: Optional[dict] = None) -> None:
    """Save an explicit memory for this user."""
    client = _get_client()
    if not client:
        return

    try:
        messages = [{"role": "user", "content": content}]
        kwargs: dict = {"user_id": user_id}
        if metadata:
            kwargs["metadata"] = metadata
        client.add(messages=messages, **kwargs)
    except Exception as e:
        logging.debug(f"[mem0] save_memory failed: {e}")


def save_interaction(
    user_id: str,
    intent: str,
    entities: dict,
    result_summary: str,
) -> None:
    """Save key learnings from a completed interaction.

    Called after each agent response so future routing benefits from context.
    Examples of what gets stored:
      "Invoiced Oak Barrel Restaurant (Wholesale, NET_30)"
      "Customer lookup for John Smith — found Wholesale tier"
    """
    client = _get_client()
    if not client:
        return

    try:
        parts: list[str] = []

        if intent == "invoice_creation":
            customer = entities.get("customer_name") or entities.get("company")
            if customer:
                parts.append(f"Created invoice for {customer}")
        elif intent == "event_invoice":
            event = entities.get("event_name")
            if event:
                parts.append(f"Processed event invoice for {event}")
        elif intent == "customer_lookup":
            customer = entities.get("customer_name") or entities.get("company")
            if customer:
                parts.append(f"Looked up customer: {customer}")
        else:
            parts.append(f"Handled {intent} request")

        if result_summary:
            # Truncate long summaries
            summary = result_summary[:150].strip()
            parts.append(f"Result: {summary}")

        if not parts:
            return

        content = " | ".join(parts)
        client.add(
            messages=[{"role": "assistant", "content": content}],
            user_id=user_id,
            metadata={"type": "interaction", "intent": intent},
        )
    except Exception as e:
        logging.debug(f"[mem0] save_interaction failed: {e}")
