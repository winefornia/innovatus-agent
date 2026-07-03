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

Memory: each turn still runs a fresh agent, but every message is keyed to its
CASE — the Google Chat thread (vertex_agent.invoice_chat_memory) — and discuss()
replays that case's rolling transcript above the new message. A terse follow-up
("2023, Other tier, $30 shipping") therefore resolves against the order given
earlier in the thread instead of arriving as a context-free fragment.
"""

from __future__ import annotations

import os

from vertex_agent.invoice_chat_actions import (
    READ_TOOLS,
    WRITE_TOOLS,
    peek_pending,
    set_current_user,
)
from vertex_agent.invoice_chat_memory import record_turn, render_case

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
  shipping_fee, send) — create (and optionally send) a Square invoice. Set
  shipping_fee to 0 when staff say shipping is free/waived, or to the custom
  dollar amount. If shipping is not known, ask whether it is free or the custom
  amount before staging. Set send=true ONLY when staff clearly want it SENT;
  otherwise it's a draft.
- stage_send_invoice(customer_name, invoice_number) — send an ALREADY-DRAFTED
  invoice to the customer (publishes the Square draft). Use when staff ask to
  send an existing draft — "send Christina's invoice", "send it out", "publish
  that draft" — including days later; it finds the draft by customer name.

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
  can't be priced or needs a price, ask for it — don't guess. Also confirm
  shipping before staging: ask "free shipping, or what custom shipping amount?"
  unless the user already provided it.

ITEMS FORMAT — items_json is a JSON array; each item is
  {"product_name": str, "vintage": int|null, "quantity": number,
   "unit_type": "case"|"bottle", "unit_price": number|null}
unit_price is a per-unit dollar price only if the order states one.

ATTACHED DOCUMENTS — a message may include the FULL text of an attached/linked
document under "[Attached document: ...]". It might be a reference sheet (price
list, inventory), an order, or anything else — DON'T assume it's an order. Read it,
then do what the USER's words ask:
- A question ("what's the retail price for Viognier 2023?") → answer it from the
  document (and/or the catalog). Quote the number from the sheet. Do NOT start an
  invoice.
- An order ("invoice Oak Barrel for 3 cases at wholesale") → pull customer + items,
  quote, and stage_invoice for confirm.
- A pricing change ("set wholesale to $53") → stage the edit.
The document is context; the user's message decides the action. If they only sent a
doc with no clear request, ask what they want to do with it.

CONVERSATION MEMORY — a "[conversation so far]" block above the newest message
replays this space's recent exchanges. That block is your ACTIVE CASE: one order
being worked from first paste to its end state (invoice drafted/sent, or staff
cancel). Rules:
- A terse follow-up ("2023", "Other tier", "2023 / Other", "$30 shipping") is
  answering YOUR latest clarifying questions, in order. Combine it with
  everything already given earlier (customer, contact info, items, quantities,
  discounts, shipping) and move the order forward. NEVER re-ask for a fact that
  appears anywhere in the conversation, and NEVER treat a follow-up as a brand
  new request or claim there is no previous context when the block is present.
- "the previous order" / "that invoice" / "her" refer to the case in the block.
- Keep driving toward the end state: quote → stage_invoice → confirmed draft →
  (when staff say send) stage_send_invoice → sent. If the conversation block is
  missing and staff reference an earlier order, use recent_invoices /
  stage_send_invoice to pick the case back up — the drafts live in Square.

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


def _build_prompt(text: str, user: str, case: str) -> str:
    """Assemble the turn's prompt: case transcript, then pending-confirm note,
    then the new message. Each turn is a fresh, memory-less agent, so ALL
    cross-turn context the model needs must be re-injected here."""
    parts: list[str] = []
    history = render_case(case)
    if history:
        parts.append(
            "[conversation so far — this Chat thread]\n"
            f"{history}\n"
            "(The staff message below continues this conversation. Short replies "
            "answer your latest questions above — combine them with the details "
            "already given instead of re-asking.)"
        )
    pending = peek_pending(user)
    if pending:
        parts.append(
            f"[pending confirmation] The user has a staged action awaiting their yes/no: "
            f"\"{pending['summary']}\". Their message below is their reply to it."
        )
    parts.append(text)
    return "\n\n".join(parts)


def discuss(text: str, *, user: str = "", case: str = "") -> str:
    """Run the assistant on a staff message; return its text answer. Never raises.

    `case` keys the rolling conversation memory (the Chat thread — see
    invoice_chat_memory). Pass "" to run a one-off, memory-less turn.
    """
    try:
        import asyncio
        from google.adk.runners import InMemoryRunner

        _ensure_anthropic_key()
        # Identify the acting approver for this turn (audit trail + keys the
        # per-user confirm store).
        set_current_user(user)
        prompt = _build_prompt(text, user, case)
        # Record the staff side up front so the context survives even if this
        # turn errors out; the assistant side is recorded only on success.
        record_turn(case, "staff", text)

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
        if out:
            record_turn(case, "assistant", out)
        return out or "I couldn't work that out — try rephrasing?"
    except Exception as e:  # pragma: no cover - defensive
        return f"Sorry — I hit an error handling that: {e}"
