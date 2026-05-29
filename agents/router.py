"""
Intent Router — classifies incoming messages and extracts entities.

Intents:
  invoice_creation  — create a new invoice for a customer order
  invoice_status    — check status of an existing invoice
  customer_lookup   — look up customer info, tier, account history
  event_invoice     — bulk invoice for event attendees / group purchase
  outreach          — send follow-up, marketing comms to customers
  general_chat      — questions, greetings, pricing inquiries

Also loads Mem0 context and injects it into the enriched message.
"""

import json
import logging
from dataclasses import dataclass, field

_INVOICE_KEYWORDS = [
    "invoice", "bill", "charge", "order", "case", "bottle", "wine",
    "cab", "pinot", "chard", "zin", "rosé", "sauvignon",
    "net 30", "net 7", "net 14", "upon receipt",
    "cases of", "bottles of",
]

_EVENT_KEYWORDS = [
    "event", "people from", "attendees", "guests", "group", "everyone at",
    "yesterday's", "last night", "tasting", "dinner", "party",
]

_CRM_KEYWORDS = [
    "look up", "find customer", "customer info", "who is", "account",
    "history", "past orders", "how many times",
]

_OUTREACH_KEYWORDS = [
    "send email", "follow up", "reach out", "contact", "message",
    "remind", "newsletter",
]

VALID_INTENTS = frozenset([
    "invoice_creation",
    "invoice_status",
    "customer_lookup",
    "event_invoice",
    "outreach",
    "general_chat",
])


@dataclass
class RouterResult:
    intent: str
    entities: dict = field(default_factory=dict)
    missing_info: list = field(default_factory=list)
    memory_context: str = ""
    enriched_message: str = ""  # original + injected memory context


def _keyword_classify(text: str) -> str:
    """Fast keyword-based classification as fallback."""
    t = text.lower()
    if any(k in t for k in _EVENT_KEYWORDS):
        return "event_invoice"
    if any(k in t for k in _CRM_KEYWORDS):
        return "customer_lookup"
    if any(k in t for k in _OUTREACH_KEYWORDS):
        return "outreach"
    if any(k in t for k in _INVOICE_KEYWORDS):
        return "invoice_creation"
    return "general_chat"


def route(raw_message: str, user_id: str) -> RouterResult:
    """Classify intent, extract entities, inject Mem0 context."""
    # Load memory context (best-effort — never blocks routing)
    memory_context = ""
    try:
        from services.mem0_service import get_memory_context
        memory_context = get_memory_context(user_id=user_id, query=raw_message)
    except Exception as e:
        logging.debug(f"[router] mem0 context skipped: {e}")

    # Keyword fast-path for obvious short messages (< 60 chars, no LLM needed)
    text = raw_message.strip()
    if len(text) < 60 and not memory_context:
        intent = _keyword_classify(text)
        enriched = text
        return RouterResult(
            intent=intent,
            enriched_message=enriched,
            memory_context=memory_context,
        )

    # LLM classification + entity extraction
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        memory_section = (
            f"\n\nRelevant memory about this user:\n{memory_context}"
            if memory_context else ""
        )

        system = (
            "You are a routing agent for Winefornia, a California winery's operations platform.\n"
            "Classify the message into one intent and extract entities.\n"
            f"{memory_section}\n\n"
            "INTENTS:\n"
            "- invoice_creation: Create a new invoice for a wine order (customer name/company + items needed)\n"
            "- invoice_status: Check status of an existing invoice (Square ID or customer name)\n"
            "- customer_lookup: Look up customer info, tier, account history\n"
            "- event_invoice: Bulk invoice for event attendees or a group (needs guest list)\n"
            "- outreach: Send emails, follow-ups, marketing messages to customers\n"
            "- general_chat: Questions, greetings, pricing inquiries, anything else\n\n"
            "Return ONLY a JSON object — no markdown, no explanation:\n"
            "{\n"
            '  "intent": "<intent>",\n'
            '  "entities": {\n'
            '    "customer_name": null or string,\n'
            '    "company": null or string,\n'
            '    "event_name": null or string,\n'
            '    "event_date": null or string,\n'
            '    "invoice_id": null or string,\n'
            '    "items": []\n'
            "  },\n"
            '  "missing_info": ["list what info is still needed before processing"]\n'
            "}\n\n"
            "For event_invoice: missing_info should include: guest list (names/emails), "
            "pricing tier, any items not yet specified.\n"
            "For invoice_creation mentioning 'people from [event]': use event_invoice."
        )

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=raw_message),
        ])

        content = result.content.strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.rsplit("```", 1)[0].strip()

        parsed = json.loads(content)
        intent = parsed.get("intent", "general_chat")
        if intent not in VALID_INTENTS:
            intent = "general_chat"

        enriched = raw_message
        if memory_context:
            enriched = f"{raw_message}\n\n[User context from memory:\n{memory_context}]"

        return RouterResult(
            intent=intent,
            entities=parsed.get("entities", {}),
            missing_info=parsed.get("missing_info", []),
            memory_context=memory_context,
            enriched_message=enriched,
        )

    except Exception as e:
        logging.warning(f"[router] LLM classification failed ({e}), using keywords")
        intent = _keyword_classify(raw_message)
        return RouterResult(
            intent=intent,
            enriched_message=raw_message,
            memory_context=memory_context,
        )
