"""
Skill Service — persistent skill memory + reference resolver.

Skills are structured lessons learned from past interactions, stored in Mem0
under agent_id="skills". They improve the agent's defaults over time.

Reference Resolver:
  "same as last time" / "usual" references are resolved by searching:
  1. Supabase invoice_logs for this customer (source of truth)
  2. Mem0 semantic hints (fallback)

Usage:
    from services.skill_service import skill_service

    # Load skills for context injection
    skills = skill_service.load_skills(user_id="tg_12345", query="Cabernet order")

    # Save a new skill after a completed invoice
    skill_service.save_skill(
        user_id="tg_12345",
        text="Oak Barrel Restaurant always orders Wholesale NET_30 CARD+BANK_ACCOUNT",
        metadata={"customer": "Oak Barrel", "type": "skill"},
    )

    # Resolve "same as last time" for a customer
    items = skill_service.resolve_reference("usual", customer_id="cus_abc", user_id="tg_12345")
    # Returns list of items or None if not found
"""

from __future__ import annotations

import logging
from typing import Any, Optional


AGENT_ID = "skills"


class SkillService:
    """Manages skill memory (Mem0) and reference resolution (Supabase + Mem0)."""

    # -------------------------------------------------------------------------
    # Skill memory (Mem0)
    # -------------------------------------------------------------------------

    def load_skills(self, user_id: str, query: str, top_k: int = 5) -> list[str]:
        """Return top-k relevant skill strings for a given query context."""
        try:
            from mem0 import MemoryClient
            from app.config import MEM0_API_KEY
            if not MEM0_API_KEY:
                return []
            client = MemoryClient(api_key=MEM0_API_KEY)
            results = client.search(
                query=query,
                user_id=user_id,
                agent_id=AGENT_ID,
                limit=top_k,
            )
            return [r["memory"] for r in results if r.get("memory")]
        except Exception as e:
            logging.debug("[skill_service] load_skills failed: %s", e)
            return []

    def save_skill(self, user_id: str, text: str, metadata: dict | None = None) -> None:
        """Write a skill to Mem0 under the skills agent namespace."""
        try:
            from mem0 import MemoryClient
            from app.config import MEM0_API_KEY
            if not MEM0_API_KEY:
                return
            client = MemoryClient(api_key=MEM0_API_KEY)
            meta = {"type": "skill", **(metadata or {})}
            client.add(
                messages=[{"role": "assistant", "content": text}],
                user_id=user_id,
                agent_id=AGENT_ID,
                metadata=meta,
            )
            logging.debug("[skill_service] saved skill for user=%s: %s", user_id, text[:80])
        except Exception as e:
            logging.debug("[skill_service] save_skill failed: %s", e)

    def synthesize_from_case(self, case: Any, user_id: str) -> None:
        """Call Claude Haiku to extract 1-2 skills from a completed invoice case.

        Called from control_layer.close_case() in background thread.
        """
        try:
            from langchain_anthropic import ChatAnthropic
            from langchain_core.messages import HumanMessage, SystemMessage

            summary_parts = []
            if getattr(case, "raw_input", None):
                summary_parts.append(f"Request: {case.raw_input[:200]}")
            if getattr(case, "intent", None):
                summary_parts.append(f"Intent: {case.intent}")
            if getattr(case, "outcome", None):
                summary_parts.append(f"Outcome: {case.outcome}")
            if getattr(case, "final_response", None):
                summary_parts.append(f"Response: {case.final_response[:200]}")

            if not summary_parts:
                return

            summary = " | ".join(summary_parts)
            llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
            result = llm.invoke([
                SystemMessage(content=(
                    "You extract reusable patterns for a winery operations agent. "
                    "Given a completed interaction, write 1-2 short facts the agent "
                    "should remember for next time (e.g. customer tier, usual order, "
                    "preference, or watch-out). Return one fact per line, no bullet points."
                )),
                HumanMessage(content=summary),
            ])

            for line in result.content.strip().splitlines():
                line = line.strip()
                if line:
                    meta = {"source": "case_synthesis", "case_id": getattr(case, "case_id", "")}
                    if case.outcome in ("failed", "failure"):
                        meta["type"] = "watch_out"
                    self.save_skill(user_id, line, metadata=meta)
        except Exception as e:
            logging.debug("[skill_service] synthesize_from_case failed: %s", e)

    # -------------------------------------------------------------------------
    # Reference resolver ("same as last time", "usual")
    # -------------------------------------------------------------------------

    def resolve_reference(
        self,
        hint: str,
        customer_name: Optional[str] = None,
        customer_id: Optional[str] = None,
        user_id: str = "",
    ) -> Optional[list[dict]]:
        """Resolve a vague reference like 'same as last time' or 'usual order'.

        Resolution order:
          1. Supabase invoice_logs for this customer (source of truth)
          2. Mem0 semantic skill hints (fallback)

        Returns a list of item dicts (product_name, quantity, unit_type, vintage)
        or None if no past order found.
        """
        # 1. Supabase invoice_logs
        items = self._resolve_from_supabase(customer_name=customer_name,
                                            customer_id=customer_id)
        if items:
            logging.info("[skill_service] resolved reference from Supabase: %d items", len(items))
            return items

        # 2. Mem0 semantic hint
        if user_id and customer_name:
            items = self._resolve_from_mem0(hint=hint, customer_name=customer_name,
                                            user_id=user_id)
            if items:
                logging.info("[skill_service] resolved reference from Mem0: %d items", len(items))
                return items

        return None

    def _resolve_from_supabase(
        self,
        customer_name: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> Optional[list[dict]]:
        """Search Supabase invoice_logs for this customer's most recent invoice items."""
        try:
            from db.repository import get_recent_invoice_for_customer
            record = get_recent_invoice_for_customer(
                customer_name=customer_name,
                customer_id=customer_id,
            )
            if record and record.get("line_items"):
                return record["line_items"]
        except Exception as e:
            logging.debug("[skill_service] supabase resolve failed: %s", e)
        return None

    def _resolve_from_mem0(
        self,
        hint: str,
        customer_name: str,
        user_id: str,
    ) -> Optional[list[dict]]:
        """Search Mem0 for a skill describing this customer's usual order."""
        try:
            skills = self.load_skills(
                user_id=user_id,
                query=f"{customer_name} usual order items",
                top_k=3,
            )
            if not skills:
                return None

            # Use LLM to extract item list from the skill text
            from langchain_anthropic import ChatAnthropic
            from langchain_core.messages import HumanMessage, SystemMessage
            import json

            llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
            result = llm.invoke([
                SystemMessage(content=(
                    "Extract wine line items from this memory snippet. "
                    'Return JSON array: [{"product_name": "...", "quantity": N, "unit_type": "case"|"bottle", "vintage": N|null}]. '
                    "Return empty array [] if no items found. No markdown."
                )),
                HumanMessage(content=f"Customer: {customer_name}\nMemory: {skills[0]}"),
            ])
            content = result.content.strip()
            if content.startswith("```"):
                content = content.split("```", 2)[1].lstrip("json").strip().rsplit("```", 1)[0].strip()
            items = json.loads(content)
            return items if isinstance(items, list) and items else None
        except Exception as e:
            logging.debug("[skill_service] mem0 resolve failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

skill_service = SkillService()
