"""
Intent classifier — lightweight routing classification for the eval suite.

This is NOT a routing orchestrator. The live pipeline routes every message
directly to the invoice agent (see services/gateway.py). This module exists
only so the eval suite can verify intent/agent classification in isolation.

It does NOT:
  - Load or write any Mem0 routing memory
  - Invoke sub-agents
  - Touch invoice logic, Square, or customer data

Usage:
    from agents.intent_classifier import classify_intent
    decision = classify_intent("invoice Sarah for 2 cases of cab")
    # decision.intent, decision.agent
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal

AgentName = Literal[
    "invoice_agent",
    "tastingroom_agent",
]


@dataclass
class IntentResult:
    """Classification output for the eval suite — intent + which agent would own it."""
    intent: str
    agent: str
    confidence: float = 1.0
    entities: dict = field(default_factory=dict)

_INTENT_TO_AGENT: dict[str, AgentName] = {
    "invoice_creation":  "invoice_agent",
    "invoice_status":    "invoice_agent",
    "tastingroom_reservation": "tastingroom_agent",
    "tastingroom_status": "tastingroom_agent",
    "general_chat":      "invoice_agent",
}

_INVOICE_KEYWORDS = [
    "invoice", "bill", "charge", "order", "case", "bottle", "wine",
    "cab", "pinot", "chard", "zin", "rosé", "sauvignon",
    "net 30", "net 7", "net 14", "upon receipt", "cases of", "bottles of",
]

_TASTINGROOM_KEYWORDS = [
    "tasting", "reservation", "booking", "josh", "haein", "mira",
    "tour and tasting", "mark paid", "invoice sent", "final confirmation",
]

# Prompt for the LLM classifier
_SYSTEM = (
    "You are an intent routing classifier for Winefornia, a winery operations platform.\n"
    "Your ONLY job: classify the user message into one intent and extract entities.\n"
    "You have NO knowledge of wines, invoices, or customers — that belongs to sub-agents.\n\n"
    "INTENTS:\n"
    "- invoice_creation  : Create a new invoice for a wholesale/trade wine order\n"
    "- invoice_status    : Check or find an existing invoice\n"
    "- tastingroom_reservation : Coordinate Innovatus tasting room reservation cases, client/Josh replies, holds, invoices, payment, final confirmation\n"
    "- tastingroom_status : Check pending tasting room reservation actions or case state\n"
    "- general_chat      : Questions, greetings, anything else\n\n"
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
    '  "missing_info": ["what is still needed before this can be processed"],\n'
    '  "confidence": 0.0-1.0\n'
    "}"
)


def _keyword_classify(text: str) -> str:
    t = text.lower()
    if any(k in t for k in _TASTINGROOM_KEYWORDS):
        if any(k in t for k in ("status", "pending", "show", "what", "state")):
            return "tastingroom_status"
        return "tastingroom_reservation"
    if any(k in t for k in _INVOICE_KEYWORDS):
        return "invoice_creation"
    return "general_chat"


def classify_intent(raw_message: str, user_id: str | None = None) -> IntentResult:
    """Classify intent and return an IntentResult. Pure classification — no domain logic.

    Short messages use the keyword path (no LLM overhead); longer messages use the
    LLM classifier and fall back to keywords on any failure.
    """
    text = raw_message.strip()

    # Fast keyword path for very short messages (no LLM overhead)
    if len(text) < 60:
        intent = _keyword_classify(text)
        agent  = _INTENT_TO_AGENT.get(intent, "invoice_agent")
        return IntentResult(intent=intent, agent=agent)

    # LLM classification
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)

        result = llm.invoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=text),
        ])

        content = result.content.strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.rsplit("```", 1)[0].strip()

        parsed = json.loads(content)
        intent = parsed.get("intent", "general_chat")
        if intent not in _INTENT_TO_AGENT:
            intent = "general_chat"

        agent      = _INTENT_TO_AGENT[intent]
        entities   = parsed.get("entities", {})
        confidence = float(parsed.get("confidence", 1.0))

        return IntentResult(intent=intent, agent=agent,
                            confidence=confidence, entities=entities)

    except Exception as e:
        logging.warning("[intent_classifier] LLM classify failed (%s), using keywords", e)
        intent = _keyword_classify(text)
        agent  = _INTENT_TO_AGENT.get(intent, "invoice_agent")
        return IntentResult(intent=intent, agent=agent, confidence=0.5)
