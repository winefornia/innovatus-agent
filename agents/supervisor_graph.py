"""
Supervisor Agent — autonomous routing orchestrator.

Responsibilities (ONLY):
  1. Load its own routing memory (separate from sub-agent memories)
  2. Classify intent from the user message
  3. Return a RoutingDecision — which agent to call and what to pass it
  4. Record the routing outcome into its own memory

What it does NOT do:
  - Invoke sub-agents
  - Touch invoice logic, Square, customer data
  - Share memory namespace with sub-agents

Memory model:
  Each agent has its own Mem0 agent_id so memories never cross:
    agent_id="supervisor"        → routing history, intent patterns per user
    agent_id="invoice_agent"     → customer prefs, tier history  (set by invoice agent)

Usage:
    supervisor = SupervisorAgent()
    decision = supervisor.route(message, user_id="tg_1234567890")
    # caller dispatches to decision.agent
    supervisor.record(user_id, decision, outcome_summary)
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

# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    """Everything the caller needs to dispatch to the right agent."""
    agent: AgentName
    intent: str
    entities: dict = field(default_factory=dict)
    missing_info: list = field(default_factory=list)
    enriched_message: str = ""      # original message + supervisor memory context
    routing_context: str = ""       # supervisor's own memory that shaped this decision
    confidence: float = 1.0
    risk_level: str = "low"         # low | medium | high | critical (set by classify_risk)


# ---------------------------------------------------------------------------
# Supervisor Agent
# ---------------------------------------------------------------------------

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


class SupervisorAgent:
    """Stateless routing agent. Mem0 provides cross-session continuity.

    Instantiate once at startup; call .route() for each message.
    """

    AGENT_ID = "supervisor"

    # Prompt for the LLM classifier
    _SYSTEM = (
        "You are an intent routing agent for Winefornia, a winery operations platform.\n"
        "Your ONLY job: classify the user message into one intent and extract entities.\n"
        "You have NO knowledge of wines, invoices, or customers — that belongs to sub-agents.\n\n"
        "{routing_context}"
        "INTENTS:\n"
        "- invoice_creation  : Create a new invoice for a wholesale/trade wine order\n"
        "- invoice_status    : Check or find an existing invoice\n"
        "- tastingroom_reservation : Coordinate Innovatus tasting room reservation cases, client/Josh replies, holds, invoices, payment, final confirmation\n"
        "- tastingroom_status : Check pending tasting room reservation actions or case state\n"
        "- general_chat      : Questions, greetings, anything else\n\n"
        "Return ONLY a JSON object — no markdown, no explanation:\n"
        "{{\n"
        '  "intent": "<intent>",\n'
        '  "entities": {{\n'
        '    "customer_name": null or string,\n'
        '    "company": null or string,\n'
        '    "event_name": null or string,\n'
        '    "event_date": null or string,\n'
        '    "invoice_id": null or string,\n'
        '    "items": []\n'
        "  }},\n"
        '  "missing_info": ["what is still needed before this can be processed"],\n'
        '  "confidence": 0.0-1.0\n'
        "}}"
    )

    def _load_routing_context(self, user_id: str, query: str) -> str:
        """Load this user's routing history from supervisor's own Mem0 namespace."""
        try:
            from mem0 import MemoryClient
            from app.config import MEM0_API_KEY
            if not MEM0_API_KEY:
                return ""
            client = MemoryClient(api_key=MEM0_API_KEY)
            results = client.search(
                query=query,
                user_id=user_id,
                agent_id=self.AGENT_ID,
                limit=4,
            )
            if not results:
                return ""
            lines = [f"- {r['memory']}" for r in results if r.get("memory")]
            if not lines:
                return ""
            return "Routing history for this user:\n" + "\n".join(lines) + "\n\n"
        except Exception as e:
            logging.debug("[supervisor] mem0 load skipped: %s", e)
            return ""

    def _keyword_classify(self, text: str) -> str:
        t = text.lower()
        if any(k in t for k in _TASTINGROOM_KEYWORDS):
            if any(k in t for k in ("status", "pending", "show", "what", "state")):
                return "tastingroom_status"
            return "tastingroom_reservation"
        if any(k in t for k in _INVOICE_KEYWORDS):
            return "invoice_creation"
        return "general_chat"

    def route(self, raw_message: str, user_id: str) -> RoutingDecision:
        """Classify intent, load routing memory, return dispatch decision.

        This method is pure routing — no domain logic.
        """
        text = raw_message.strip()

        # Fast keyword path for very short messages (no LLM overhead)
        if len(text) < 60:
            intent = self._keyword_classify(text)
            agent  = _INTENT_TO_AGENT.get(intent, "invoice_agent")
            from services.control_layer import classify_risk
            return RoutingDecision(
                agent=agent,
                intent=intent,
                enriched_message=text,
                risk_level=classify_risk(intent, {}, confidence=1.0),
            )

        # Load supervisor's own routing context
        routing_context = self._load_routing_context(user_id, text)

        # LLM classification
        try:
            from langchain_anthropic import ChatAnthropic
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
            system_prompt = self._SYSTEM.format(routing_context=routing_context)

            result = llm.invoke([
                SystemMessage(content=system_prompt),
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

            from services.control_layer import classify_risk
            risk_level = classify_risk(intent, entities, confidence)

            # Enrich message with routing context only (not domain data)
            enriched = text
            if routing_context:
                enriched = f"{text}\n\n[Routing context:\n{routing_context.strip()}]"

            return RoutingDecision(
                agent=agent,
                intent=intent,
                entities=entities,
                missing_info=parsed.get("missing_info", []),
                enriched_message=enriched,
                routing_context=routing_context,
                confidence=confidence,
                risk_level=risk_level,
            )

        except Exception as e:
            logging.warning("[supervisor] LLM classify failed (%s), using keywords", e)
            intent = self._keyword_classify(text)
            agent  = _INTENT_TO_AGENT.get(intent, "invoice_agent")
            from services.control_layer import classify_risk
            return RoutingDecision(
                agent=agent,
                intent=intent,
                enriched_message=text,
                risk_level=classify_risk(intent, {}, confidence=0.5),
            )

    def record(self, user_id: str, decision: RoutingDecision, outcome: str) -> None:
        """Save this routing event to the supervisor's own memory.

        Stored under agent_id='supervisor' — never mixes with sub-agent memories.
        """
        try:
            from mem0 import MemoryClient
            from app.config import MEM0_API_KEY
            if not MEM0_API_KEY:
                return
            client = MemoryClient(api_key=MEM0_API_KEY)

            parts = [f"Routed to {decision.agent} (intent: {decision.intent})"]
            customer = (decision.entities or {}).get("customer_name") or \
                       (decision.entities or {}).get("company")
            if customer:
                parts.append(f"for {customer}")
            if outcome:
                parts.append(f"outcome: {outcome[:100]}")

            client.add(
                messages=[{"role": "assistant", "content": " | ".join(parts)}],
                user_id=user_id,
                agent_id=self.AGENT_ID,
                metadata={"type": "routing", "intent": decision.intent, "agent": decision.agent},
            )
        except Exception as e:
            logging.debug("[supervisor] record failed: %s", e)


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly
# ---------------------------------------------------------------------------

supervisor = SupervisorAgent()
