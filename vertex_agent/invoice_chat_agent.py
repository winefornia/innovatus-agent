"""Conversational assistant for the invoicing Google Chat space.

The invoicing counterpart to vertex_agent.chat_agent (the tasting-room
assistant). Staff type freely — "what's wholesale on the 2023 Viognier?", "set
the FOB price to $41", "make Corporate 25% off", "make the Cab unavailable for
wholesale", "invoice Oak Barrel for 3 cases of Cabernet at wholesale and send it"
— or forward/drop a PDF order. The agent UNDERSTANDS the intent but can ONLY do a
tight, allow-listed set of things; it can't do "a lot of random shit."

It is built exactly like the tasting-room assistant: read tools answer directly,
and anything that touches money or live pricing is CONFIRM-FIRST — the agent
stages the action and only acts on the user's affirming reply. The actual writes
route through the SAME service primitives the invoice graph uses
(services.product_service, services.square_service), so chat is an alternate
control surface, not a second code path. Authorization is enforced upstream: the
Google Chat adapter only reaches discuss() for allow-listed approvers.

PDFs: the Google Chat adapter downloads an attached order PDF, digests it to text
via services.pdf_service, and hands that text to discuss() as input state. The
agent reads it, pulls out the customer + items, and stages an invoice for confirm.
"""

from __future__ import annotations

import os

from vertex_agent.invoice_chat_actions import (
    READ_TOOLS,
    WRITE_TOOLS,
    peek_pending,
    set_current_user,
)

_CHAT_INSTRUCTION = """\
You are the Winefornia invoicing assistant in Google Chat. Staff type questions
and orders; you answer like a sharp colleague who knows the catalog and pricing —
warm, plain-spoken, brief.

WHAT YOU CAN DO (and nothing else — never improvise actions outside these tools):

Read (answer immediately, no confirmation):
- find_products(query) — look up wines by name/alias ("cab", "sb").
- get_pricing(product, vintage) — MSRP + every per-channel price for one wine.
- list_tiers() — the pricing tiers, their discount % and multiplier.
- recent_invoices(limit) — recent invoices and their status.
- price_order(customer_name, tier, items_json) — a priced QUOTE, nothing created.

Act (CONFIRM-FIRST — these stage, then you stop and show the "reply yes" line):
- stage_set_channel_price(product, channel, price, vintage) — change a wholesale/
  fob/club_member/ex_cellar price.
- stage_set_msrp(product, price, vintage) — change a wine's retail MSRP.
- stage_set_tier(tier, discount_percent, msrp_multiplier) — change a whole tier
  (affects every product on it). Pass -1 to leave a field unchanged.
- stage_set_availability(product, tier, available, vintage) — make a wine
  available/unavailable for a tier.
- stage_invoice(customer_name, customer_email, tier, items_json, payment_schedule,
  send) — create (and optionally send) a Square invoice. Set send=true ONLY when
  staff clearly want it SENT; otherwise it's a draft.

CONFIRM-FIRST PROTOCOL (critical):
- Every stage_* tool DOES NOT act — it returns a one-line "reply yes to confirm"
  question. After calling one, STOP and show that question. Do NOT call
  confirm_pending_action() in the same turn.
- When the user's NEXT message confirms ("yes", "do it", "send it", "go ahead"),
  call confirm_pending_action(). When they decline ("no", "never mind"), call
  cancel_pending_action(). If a "[pending confirmation]" note appears below, the
  user is replying to exactly that staged action.
- Only act when the user clearly asks you to. When they're just asking, answer.
- Before staging an invoice, price the order so you can show the total. If an item
  can't be priced or needs a price, ask for it — don't guess.

ITEMS FORMAT — items_json is a JSON array; each item is
  {"product_name": str, "vintage": int|null, "quantity": number,
   "unit_type": "case"|"bottle", "unit_price": number|null}
unit_price is a per-unit dollar price only if the order states one.

PDFs / forwarded orders — if the message contains digested PDF/order text, read
it, pull out the customer (name + email), the tier, and the line items, then quote
and stage_invoice for confirm. If the email or tier is missing, ask.

HOW TO TALK — this is a chat message, not a report. Lead with a one-line takeaway,
then details. Quote concrete numbers (prices, totals, tiers). Always say which
wine/tier/customer you acted on.

FORMATTING — Google Chat does NOT render Markdown tables, "###" headings, or "**"
bold (they show as literal junk). Use ONLY: *bold* with SINGLE asterisks, _italic_
with underscores, and "• " or "- " bullets. NEVER use tables, "#"/"###" headings,
or "**"."""

_chat_agent = None


def _ensure_anthropic_key() -> None:
    """Make sure ANTHROPIC_API_KEY is in os.environ before LiteLLM looks for it.

    LiteLLM reads the key from os.environ, but this module can be the first thing
    imported on a given entry path — and the key may live only in .env / app.config
    (loaded by load_dotenv). Importing app.config runs load_dotenv() and gives us
    the value to backfill. No-op in prod, where the key is a real env var.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        from app.config import ANTHROPIC_API_KEY
        if ANTHROPIC_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    except Exception:  # pragma: no cover - defensive
        pass


def _get_chat_agent():
    """Build the ADK assistant lazily so this module imports without google-adk."""
    global _chat_agent
    if _chat_agent is None:
        from google.adk.agents import LlmAgent
        from google.adk.models.lite_llm import LiteLlm
        _chat_agent = LlmAgent(
            model=LiteLlm(model=os.getenv("INV_AGENT_MODEL", os.getenv("TR_AGENT_MODEL", "anthropic/claude-sonnet-4-6"))),
            name="invoice_assistant",
            description="Conversational assistant for invoicing: answers catalog/pricing questions, edits prices/tiers, and creates/sends invoices (confirm-first for money + live pricing).",
            instruction=_CHAT_INSTRUCTION,
            tools=[*READ_TOOLS, *WRITE_TOOLS],
        )
    return _chat_agent


def discuss(text: str, *, user: str = "") -> str:
    """Run the assistant on a staff message; return its text answer. Never raises."""
    try:
        import asyncio
        from google.adk.runners import InMemoryRunner

        _ensure_anthropic_key()
        # Identify the acting approver for this turn (audit trail + keys the
        # per-user confirm store). Each turn is a fresh, memory-less agent, so if
        # the user has a staged confirm-first action, re-inject it into the prompt
        # so "yes" resolves to the right thing.
        set_current_user(user)
        prompt = text
        pending = peek_pending(user)
        if pending:
            prompt = (
                f"[pending confirmation] The user has a staged action awaiting their yes/no: "
                f"\"{pending['summary']}\". Their message below is their reply to it.\n\n{text}"
            )

        async def _run():
            return await asyncio.wait_for(
                InMemoryRunner(agent=_get_chat_agent(), app_name="inv-chat").run_debug(prompt, quiet=True),
                timeout=float(os.getenv("INV_AGENT_TIMEOUT", "120")),
            )

        events = asyncio.run(_run())
        out = ""
        for e in events:
            c = getattr(e, "content", None)
            if not c:
                continue
            for p in (c.parts or []):
                if getattr(p, "text", None):
                    out = p.text
        return out or "I couldn't work that out — try rephrasing?"
    except Exception as e:  # pragma: no cover - defensive
        return f"Sorry — I hit an error handling that: {e}"
