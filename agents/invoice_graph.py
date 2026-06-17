"""
Invoice Agent — LangGraph stateful workflow.

Design: deterministic state machine. LLM (Claude Haiku) is called only as a
sidecar for extraction, clarifying questions, fuzzy match hints, and edit
parsing. It never drives routing or takes actions.

Flow:
  classify_intent
    ↓ invoice_request                    ↓ chat/question
  extract_invoice_fields              chat_response
    ↓ [reference resolver: "usual" → Supabase → Mem0]
  ask_missing_fields                  [INTERRUPT: single focused LLM question
    ↓                                   when confidence < 0.75 or fields missing]
  resolve_customer
    ↓ exact match                        ↓ fuzzy (LLM hint in payload)
    auto-confirmed                    clarify_customer_match  [INTERRUPT]
    ↓
  confirm_tier_and_payment            [INTERRUPT: tier wizard keyboard]
    ↓
  resolve_products_and_prices         [deterministic — catalog × multiplier]
    ↓
  create_invoice_preview
    ↓
  approval_gate                       [INTERRUPT: approve / reject / edit]
    ↓ approved    ↓ edit_requested
    │             interpret_edit      [INTERRUPT: ask what to change]
    │               ↓ apply_patch     [deterministic; INTERRUPT if conf < 0.80]
    │               ↓ resolve_products_and_prices → create_invoice_preview
    ↓
  create_square_invoice_draft         [tool_registry: customer → order → draft]
    ↓
  confirm_send                        [INTERRUPT: publish or keep draft]
    ↓ send
  offer_email_receipt                 [INTERRUPT: send receipt?]
    ↓
  send_receipt_email_node
    ↓
  respond

Interrupts surface in Telegram (bot.py) and Google Chat (google_chat_adapter.py)
as inline keyboards. All interrupts are guarded against stale callbacks via
which() + _VALID_AT.

New fields in InvoiceState vs original:
  extraction_confidence, extraction_ambiguities  — confidence gate
  edit_instruction, edit_patch, edit_rounds      — structured edit flow
  _case_id                                       — control layer linkage
"""

import hashlib
import json
import logging
import os
import re
import time
from typing import TypedDict, Literal, Any

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from app.config import POSTGRES_CONNECTION_STRING


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class InvoiceState(TypedDict, total=False):
    # Input
    raw_message: str
    sender_id: str

    # Classification
    intent: str

    # Extraction (LLM output)
    extracted: dict[str, Any]       # customer_name, company, email, phone, items
    missing_fields: list[str]
    extraction_confidence: float    # combined LLM + deterministic confidence (0.0–1.0)
    extraction_ambiguities: list[str]  # human-readable ambiguity notes

    # Customer resolution
    customer: dict[str, Any]    # from customers.json
    customer_confirmed: bool    # True=exact match or new; False=fuzzy match needs confirm

    # Tier + payment confirmation (from interrupt)
    tier_name: str
    payment_schedule: str       # UPON_RECEIPT | NET_7 | NET_14 | NET_30
    payment_methods: list[str]  # CARD | BANK_ACCOUNT

    # Pricing
    line_items: list[dict[str, Any]]
    pricing_result: dict[str, Any]  # full output of calculate_invoice_prices
    awaiting_price: Any             # variable-pricing items pending operator price (interrupt)

    # Invoice preview
    invoice_preview: dict[str, Any]

    # Approval
    approval: Literal["approved", "rejected", "edit_requested"]
    edit_instruction: str       # raw edit text from Cecil (e.g. "make it 6 bottles")
    edit_patch: dict            # structured patch from interpret_edit LLM
    edit_rounds: int            # number of edit cycles used (max 2)

    # Square output
    square_order_id: str
    square_invoice_id: str
    square_invoice_version: int
    square_invoice_url: str         # public pay link (set after publish)
    send_decision: Literal["send", "draft"]

    # Email receipt
    email_receipt_decision: str  # "send" | "skip"
    receipt_sent_to: str         # email address receipt was sent to

    # Response
    final_response: str

    # Reconciliation — set when a critical side effect partially succeeded
    # (e.g. Square draft created but Supabase log failed).
    # Must be surfaced to Cecil and resolved manually.
    reconciliation_needed: bool
    reconciliation_reason: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

_INVOICE_SIGNALS = [
    "invoice", "bill", "charge", "payment", "order",
    "case", "bottle", "wine", "cab", "pinot", "chard", "zin",
    "net 30", "net 7", "net 14", "upon receipt",
    "wants", "would like", "can we get", "please send",
    "cases of", "bottles of", "6 btl", "12 btl",
]


def classify_intent(state: InvoiceState) -> InvoiceState:
    text = state["raw_message"].lower()

    # Fast path — unambiguous invoice signals
    if any(w in text for w in _INVOICE_SIGNALS):
        return {"intent": "invoice_request"}

    # For short greetings / obvious chat — go to chat node
    if len(text.strip()) < 80:
        return {"intent": "chat"}

    # Longer / ambiguous messages — ask Claude
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=(
                "You help classify messages for a winery invoicing system. "
                "Reply with ONLY one word:\n"
                "  invoice_request — if this is a request to create/process an invoice or order\n"
                "  chat — anything else (question, greeting, pricing inquiry, modification ask, etc.)"
            )),
            HumanMessage(content=state["raw_message"]),
        ])
        intent = "invoice_request" if "invoice" in result.content.strip().lower() else "chat"
        return {"intent": intent}
    except Exception:
        return {"intent": "chat"}


def chat_response(state: InvoiceState) -> InvoiceState:
    """Handle conversational messages — questions, pricing inquiries, modifications,
    greetings, anything that's not a direct invoice creation request.

    Answers with full context: catalog, tiers, and any in-progress invoice state.
    If the user's message is actually an invoice request in disguise, suggests
    they paste the order so the agent can process it.
    """
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage
        from services.product_service import _load_catalog, _load_tiers
    except ImportError as e:
        return {"final_response": f"Chat not available: {e}"}

    # Build catalog context (summarise — don't dump the whole file)
    try:
        catalog = _load_catalog()
        tiers   = _load_tiers()
        # Group by name for a compact summary
        names = sorted({p["name"] for p in catalog})
        catalog_summary = ", ".join(names)
        tier_lines = "\n".join(
            f"  • {t['name']}: {t['discount_percent']}% off MSRP (×{t['msrp_multiplier']})"
            for t in tiers
        )
    except Exception:
        catalog_summary = "(catalog unavailable)"
        tier_lines = "(tiers unavailable)"

    # Include any in-progress invoice context
    invoice_ctx_parts = []
    ext = state.get("extracted") or {}
    customer = state.get("customer") or {}
    if customer.get("full_name"):
        invoice_ctx_parts.append(
            f"Customer in progress: {customer['full_name']}"
            + (f" ({customer.get('tier_name', 'no tier assigned')})" if customer.get("tier_name") else "")
        )
    if ext.get("items"):
        items_str = ", ".join(
            f"{i.get('quantity')} {i.get('unit_type','?')} {i.get('product_name','?')}"
            for i in ext["items"]
        )
        invoice_ctx_parts.append(f"Items being invoiced: {items_str}")
    if state.get("tier_name"):
        invoice_ctx_parts.append(f"Confirmed tier: {state['tier_name']}, {state.get('payment_schedule','')}")

    invoice_ctx = ("\n\nCurrent invoice in progress:\n" + "\n".join(invoice_ctx_parts)) if invoice_ctx_parts else ""

    system = f"""You are a smart assistant for Winefornia, a California winery.
You help the operations team (Cecil, Audrey) with invoicing, customer questions, and wine info.

Available wines: {catalog_summary}

Pricing tiers:
{tier_lines}
{invoice_ctx}

Guidelines:
- Be concise and direct. No filler phrases.
- If asked about pricing, give the MSRP multiplier and discount percent for the relevant tier.
- If asked to "change" something on an in-progress invoice (e.g. different quantity, different customer),
  tell the user to reject the current invoice and resubmit with the corrected order — the agent will re-extract.
- If the user seems to be placing a new order, confirm what you understood and say
  "Paste the full order details and I'll process it."
- You know about all wines in the catalog above. Don't make up products not listed.
- Keep replies under 4 sentences unless a list or table is genuinely helpful.
"""

    try:
        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0.3)
        result = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=state["raw_message"]),
        ])
        return {"final_response": result.content.strip()}
    except Exception as e:
        return {"final_response": "I'm here to help — describe an order or ask about pricing and I'll assist right away."}


def extract_invoice_fields(state: InvoiceState) -> InvoiceState:
    """Use Claude API to extract structured fields from the raw message."""
    raw = state["raw_message"]

    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        system = (
            "You are an invoice extraction assistant for a California winery (Winefornia).\n"
            "Extract invoice details from messy real-world input: forwarded emails, phone notes, "
            "WhatsApp messages, abbreviated orders — whatever form they come in.\n\n"
            "Return ONLY a JSON object with these keys (null if not found):\n"
            "  customer_name: person's name (first+last if available)\n"
            "  company: business/restaurant/retailer name\n"
            "  email: email address\n"
            "  phone: phone number\n"
            "  items: array of line items, each with:\n"
            "    product_name: wine name (e.g. 'Cabernet Sauvignon', 'Pinot Noir', 'Chardonnay'). "
            "Resolve abbreviations: cab→Cabernet Sauvignon, pn/pinot→Pinot Noir, chard→Chardonnay, "
            "sauv blanc/sb→Sauvignon Blanc, zin→Zinfandel\n"
            "    vintage: year as INTEGER (e.g. 2022), null if not specified — never a string\n"
            "    quantity: INTEGER\n"
            "    unit_type: MUST be exactly 'case' or 'bottle' (singular, never plural) — default 'case' for wholesale\n"
            "    unit_price: the price charged per ONE unit_type as stated in the source, as a NUMBER in dollars "
            "(e.g. 714.00). If the source shows a line total for multiple units, divide by quantity to get the "
            "per-unit price. If both a retail/list/MSRP and an actual charged/cost price are shown, use the "
            "CHARGED/cost price. null if no price is stated in the source.\n\n"
            "Also return:\n"
            "  confidence: float 0.0–1.0 — your extraction confidence. Lower if customer is first-name only, "
            "items are vague, quantities unclear, or the message references 'same as last time' / 'usual'.\n"
            "  ambiguities: list of short strings describing what is unclear or could be misread "
            "(e.g. 'first name only — multiple matches possible', 'unit_type inferred as case'). "
            "Empty list if everything is clear.\n\n"
            "Rules:\n"
            "- If the message mentions a company/restaurant/retailer, put it in company even if only "
            "a company name is given (customer_name can be null)\n"
            "- Extract all wine items mentioned, even if described informally\n"
            "- Do not invent vintages — use null if not stated\n"
            "Return ONLY the JSON object, no markdown, no explanation."
        )
        from services.invoice_hooks import hooks
        case_id = state.get("_case_id", "")
        hooks.fire("pre_llm_call", {"model": "claude-haiku-4-5-20251001", "node": "extract_invoice_fields", "prompt_len": len(raw)}, case_id=case_id)
        t0 = time.monotonic()
        result = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=raw),
        ])
        latency_ms = int((time.monotonic() - t0) * 1000)
        content = result.content.strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.rsplit("```", 1)[0].strip()
        extracted = json.loads(content)
        hooks.fire("post_llm_call", {"node": "extract_invoice_fields", "fields": list(extracted.keys()), "latency_ms": latency_ms}, case_id=case_id)
    except Exception:
        # Fallback: minimal keyword extraction so the graph doesn't die
        extracted = {
            "customer_name": None,
            "company": None,
            "email": None,
            "phone": None,
            "items": [],
            "confidence": 0.3,
            "ambiguities": ["extraction failed — using fallback"],
        }

    # Pull LLM confidence/ambiguities then apply deterministic adjustments
    llm_confidence  = float(extracted.pop("confidence", 1.0))
    ambiguities     = list(extracted.pop("ambiguities", []))
    confidence      = _adjust_confidence(extracted, llm_confidence, ambiguities)

    # Resolve "same as last time" / "usual" references before confidence gates
    _ref_keywords = ("usual", "same", "last time", "regular", "normal")
    items_raw = extracted.get("items", [])
    if any(any(kw in str(i.get("product_name", "")).lower() for kw in _ref_keywords) for i in items_raw):
        cust_name = extracted.get("company") or extracted.get("customer_name")
        if cust_name:
            try:
                from services.skill_service import skill_service
                resolved = skill_service.resolve_reference(
                    hint="usual",
                    customer_name=cust_name,
                    user_id=state.get("sender_id", ""),
                )
                if resolved:
                    extracted["items"] = resolved
                    confidence = max(confidence, 0.90)
                    ambiguities = [a for a in ambiguities if "usual" not in a.lower()
                                   and "same" not in a.lower()]
            except Exception:
                pass

    missing: list[str] = []
    has_customer = any([
        extracted.get("customer_name"),
        extracted.get("company"),
        extracted.get("email"),
        extracted.get("phone"),
    ])
    if not has_customer:
        missing.append("customer (name, company, email, or phone)")
    if not extracted.get("items"):
        missing.append("items (product name and quantity)")

    # Step-progress message for Cecil
    if not missing and confidence >= 0.75:
        n_items = len(extracted.get("items", []))
        who = extracted.get("customer_name") or extracted.get("company") or "customer"
        step_msg = f"✓ Extracted: {who} · {n_items} item{'s' if n_items != 1 else ''} — looking up customer…"
    else:
        step_msg = None

    out: dict = {
        "extracted": extracted,
        "missing_fields": missing,
        "extraction_confidence": confidence,
        "extraction_ambiguities": ambiguities,
    }
    if step_msg:
        out["final_response"] = step_msg
    return out


def _adjust_confidence(extracted: dict, llm_conf: float, ambiguities: list) -> float:
    """Combine LLM confidence with deterministic rules. Returns adjusted float 0.0–1.0.

    LLM confidence is treated as a routing signal, not ground truth.
    Deterministic penalties ensure the system asks when it genuinely can't be sure.
    """
    conf = llm_conf

    # Customer identity penalties
    has_email   = bool(extracted.get("email"))
    has_phone   = bool(extracted.get("phone"))
    has_name    = bool(extracted.get("customer_name"))
    has_company = bool(extracted.get("company"))

    if not has_email and not has_phone:
        conf -= 0.15   # can't uniquely identify without contact info
    if not has_name and not has_company:
        conf -= 0.20   # no customer identity at all

    # Item clarity penalties
    items = extracted.get("items", [])
    for item in items:
        name = str(item.get("product_name", "")).lower()
        if any(ref in name for ref in ("usual", "same", "last time", "regular")):
            conf -= 0.25   # reference to past order — can't resolve deterministically
            break
        if item.get("quantity") is None:
            conf -= 0.10
        if item.get("unit_type") not in ("case", "bottle"):
            conf -= 0.05

    # Ambiguity count penalty
    conf -= len(ambiguities) * 0.05

    return max(0.1, min(1.0, round(conf, 2)))


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# Item fields that mean "a price is already established" — checked before we ever
# prompt for one, so the operator is never asked for a price they already gave.
_PRICE_FIELDS = ("regular_unit_price_cents", "manual_price_cents", "unit_price")


def _has_price(item: dict) -> bool:
    return any(item.get(f) not in (None, "", 0) for f in _PRICE_FIELDS)


def _clarification_price_cents(text: str, exclude: str | None = None) -> int | None:
    """Extract a per-bottle price (cents) from a free-text clarification reply.

    A `$`-prefixed amount always wins. Otherwise a bare number counts as a price
    only when it can't be a vintage year — it has decimals, or sits outside the
    1900–2099 range — so "2023" is read as a vintage, not $2,023.
    """
    t = text.replace(",", "")
    m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", t)
    if m:
        return int(round(float(m.group(1)) * 100))
    for m in re.finditer(r"\b(\d+(?:\.\d{1,2})?)\b", t):
        tok = m.group(1)
        if exclude is not None and tok == exclude:
            continue
        if "." in tok or not (1900 <= float(tok) <= 2099):
            return int(round(float(tok) * 100))
    return None


def _apply_clarification_facts(text: str, extracted: dict) -> None:
    """Fold a plain-text clarification (a stated price and/or vintage) into the
    extracted items, so the value the operator already gave is not asked again.

    Price is stored as `regular_unit_price_cents` (per bottle, pre-discount — the
    selected tier's discount applies) and only when exactly one item still lacks a
    price, to avoid mis-assigning it across a multi-item order. Vintage fills in
    any item missing one.
    """
    items = extracted.get("items") or []
    if not items:
        return

    year_m = _YEAR_RE.search(text)
    if year_m:
        vintage = int(year_m.group(0))
        for it in items:
            if not it.get("vintage"):
                it["vintage"] = vintage

    price_cents = _clarification_price_cents(text, exclude=year_m.group(0) if year_m else None)
    if price_cents is not None:
        unpriced = [
            it for it in items
            if not _has_price(it)
        ]
        if len(unpriced) == 1:
            unpriced[0]["regular_unit_price_cents"] = price_cents


def ask_missing_fields(state: InvoiceState) -> InvoiceState:
    """Interrupt if required fields are missing OR confidence is too low.

    Generates one focused LLM question instead of a flat list.
    Triggers when:
      - Fields are literally missing, OR
      - extraction_confidence < 0.75
    """
    missing      = state.get("missing_fields", [])
    confidence   = state.get("extraction_confidence", 1.0)
    ambiguities  = state.get("extraction_ambiguities", [])

    needs_clarification = bool(missing) or confidence < 0.75
    if not needs_clarification:
        return {}

    # Generate a single focused question via LLM
    question = _generate_clarifying_question(
        missing=missing,
        ambiguities=ambiguities,
        extracted=state.get("extracted", {}),
    )

    response = interrupt({
        "type": "missing_fields",
        "missing": missing,
        "confidence": confidence,
        "ambiguities": ambiguities,
        "question": question,
    })

    # Resume value: JSON string with clarification data, or plain text
    existing = state.get("extracted", {})
    try:
        updates = json.loads(response) if isinstance(response, str) else response
        if isinstance(updates, dict):
            existing.update(updates)
        else:
            raise ValueError("not a dict")
    except Exception:
        # Plain text answer — fold back in as raw clarification AND capture any
        # price/vintage the operator stated, so we don't re-ask for the same
        # value later (e.g. at confirm_item_prices).
        if response:
            existing["_clarification"] = str(response)
            _apply_clarification_facts(str(response), existing)

    return {
        "extracted": existing,
        "missing_fields": [],
        "extraction_confidence": 1.0,   # reset after human clarification
        "extraction_ambiguities": [],
    }


def _generate_clarifying_question(missing: list, ambiguities: list, extracted: dict) -> str:
    """Ask Claude Haiku to generate ONE focused clarifying question."""
    context_parts = []
    if missing:
        context_parts.append(f"Missing: {', '.join(missing)}")
    if ambiguities:
        context_parts.append(f"Unclear: {'; '.join(ambiguities[:3])}")

    context = " | ".join(context_parts) or "some fields need confirmation"

    # Fast fallback if context is simple
    if not ambiguities and len(missing) == 1:
        if "customer" in missing[0]:
            return "Who is this order for? (name, company, or email)"
        if "items" in missing[0]:
            return "What wines and quantities? (e.g. '2 cases Cabernet Sauvignon')"

    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=(
                "You help a winery operations assistant clarify incomplete invoice requests. "
                "Given what is missing or unclear, write ONE short, direct question to ask the operator. "
                "Maximum 20 words. No filler. Return only the question."
            )),
            HumanMessage(content=f"Context: {context}\nPartial data: {json.dumps(extracted, default=str)[:300]}"),
        ])
        q = result.content.strip().strip('"')
        return q if q else f"Could you clarify: {context}?"
    except Exception:
        return f"Could you clarify: {context}?"


def resolve_customer(state: InvoiceState) -> InvoiceState:
    """Look up the customer in customers.json / Supabase."""
    from services.customer_service import lookup_customer

    ext = state.get("extracted", {})
    result = lookup_customer(
        name=ext.get("customer_name"),
        email=ext.get("email"),
        phone=ext.get("phone"),
        company=ext.get("company"),
    )

    match_type = result["match"]

    if match_type == "none":
        # Customer not in DB — carry forward what we extracted, no confirmation needed
        display_name = ext.get("customer_name") or ext.get("company") or "Unknown"
        customer = {
            "id": None,
            "full_name": display_name,
            "company": ext.get("company"),
            "email": ext.get("email"),
            "phone": ext.get("phone"),
            "tier_name": None,
            "square_customer_id": None,
        }
        step_msg = f"New customer: {display_name} — not in database, will create."
        return {"customer": customer, "customer_confirmed": True, "final_response": step_msg}

    customer = result["customer"]
    name = customer.get("full_name") or customer.get("company") or "Unknown"
    tier = customer.get("tier_name")
    tier_note = f" · {tier}" if tier else ""

    # Exact matches → auto-confirmed; fuzzy matches → ask Cecil
    if match_type in ("exact_email", "exact_phone", "name_company"):
        step_msg = f"✓ Customer: {name}{tier_note} (matched by {match_type.replace('_', ' ')})"
        return {"customer": customer, "customer_confirmed": True, "final_response": step_msg}
    else:
        # fuzzy_name / fuzzy_company — get LLM recommendation before asking Cecil
        llm_hint = _llm_fuzzy_recommend(
            message=state.get("raw_message", ""),
            candidate=customer,
        )
        # Store the hint in customer dict for clarify_customer_match to surface
        customer_with_hint = {**customer, "_llm_reason": llm_hint.get("reason", ""), "_llm_confidence": llm_hint.get("confidence", 0.0)}
        step_msg = f"Close match found: {name}{tier_note} — please confirm this is the right person."
        return {"customer": customer_with_hint, "customer_confirmed": False, "final_response": step_msg}


def _llm_fuzzy_recommend(message: str, candidate: dict) -> dict:
    """Ask Claude Haiku whether the fuzzy-matched candidate is likely correct.

    Returns {"reason": str, "confidence": float} — used to pre-highlight the
    match in the clarify_customer_match interrupt. Never auto-confirms.
    """
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        name = candidate.get("full_name") or candidate.get("company") or "Unknown"
        tier = candidate.get("tier_name") or "unknown tier"

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=(
                "You help a winery match order requests to customer records. "
                "Given the message and a candidate customer, say how likely this candidate is correct. "
                'Return ONLY JSON: {"reason": "brief explanation", "confidence": 0.0-1.0}'
            )),
            HumanMessage(content=f"Message: {message[:300]}\nCandidate: {name} ({tier})"),
        ])
        content = result.content.strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1].lstrip("json").strip().rsplit("```", 1)[0].strip()
        return json.loads(content)
    except Exception:
        return {"reason": "", "confidence": 0.0}


def clarify_customer_match(state: InvoiceState) -> InvoiceState:
    """Interrupt when the customer match is fuzzy — Cecil must confirm or deny.

    Resume with: "yes" to confirm, anything else to restart with a new lookup.
    """
    customer = state.get("customer", {})
    name = customer.get("full_name") or customer.get("company") or "Unknown"

    llm_reason = customer.get("_llm_reason", "")
    llm_conf   = customer.get("_llm_confidence", 0.0)
    hint_note  = f"\nAgent confidence: {int(llm_conf * 100)}% — {llm_reason}" if llm_reason else ""

    response = interrupt({
        "type": "confirm_customer",
        "customer": {k: v for k, v in customer.items() if not k.startswith("_")},
        "llm_recommendation": {"reason": llm_reason, "confidence": llm_conf},
        "question": (
            f"I found a close match: {name}.{hint_note}\n"
            "Is this the right customer? Reply 'yes' to confirm or 'no' to re-submit with email/phone."
        ),
    })

    tokens = set(str(response).strip().lower().split())
    if tokens & {"yes", "y", "correct", "confirmed"}:
        return {"customer_confirmed": True}

    # User said no — respond with instructions to retry
    return {
        "customer_confirmed": False,
        "final_response": (
            f"Understood — {name} was not the right match.\n"
            "Please re-submit the order with the customer's email or phone number so I can find the correct record."
        ),
    }


def confirm_tier_and_payment(state: InvoiceState) -> InvoiceState:
    """Interrupt to confirm pricing tier, payment schedule, and payment methods.

    The customer's existing tier from Supabase/JSON is surfaced as default_tier
    so the UI (wizard) can pre-select it. Human must always confirm before billing.

    Resume format (string): "Wholesale, NET_30, CARD+BANK_ACCOUNT"
    """
    customer = state.get("customer", {})
    default_tier = customer.get("tier_name")  # None if new/unknown customer
    name = customer.get("full_name") or customer.get("company") or "this customer"

    tier_note = f"tier on file: {default_tier}" if default_tier else "no tier on file — please select"

    response = interrupt({
        "type": "tier_and_payment_confirmation",
        "customer": {
            "name": name,
            "company": customer.get("company"),
            "email": customer.get("email"),
            "default_tier": default_tier,
            "tier_source": "db" if default_tier else "unknown",
        },
        "question": (
            f"Confirm pricing for {name} ({tier_note}):\n"
            "Tier: Wholesale | Corporate | Club Member | Employee | Direct | FOB/Export\n"
            "Terms: UPON_RECEIPT | NET_7 | NET_14 | NET_30\n"
            "Methods: CARD | BANK_ACCOUNT\n"
            'Reply: "Wholesale, NET_30, CARD+BANK_ACCOUNT"'
        ),
    })

    # Parse response — use DB tier as starting default, but user can override
    tier_name = default_tier or "Direct"
    payment_schedule = "NET_30"
    payment_methods = ["CARD", "BANK_ACCOUNT"]

    try:
        if isinstance(response, dict):
            tier_name = response.get("tier_name", tier_name)
            payment_schedule = response.get("payment_schedule", payment_schedule)
            payment_methods = response.get("payment_methods", payment_methods)
        elif isinstance(response, str):
            parts = [p.strip() for p in response.split(",")]
            if len(parts) >= 1 and parts[0]:
                tier_name = parts[0]
            if len(parts) >= 2 and parts[1]:
                import re as _re
                schedule_raw = parts[1].upper().strip()
                # Normalize "NET30" / "NET 30" / "NET_30" all → "NET_30"
                schedule_raw = _re.sub(r"NET\s*(\d+)", r"NET_\1", schedule_raw).replace(" ", "_")
                if schedule_raw in ("UPON_RECEIPT", "NET_7", "NET_14", "NET_30"):
                    payment_schedule = schedule_raw
            if len(parts) >= 3 and parts[2]:
                methods_raw = parts[2].upper()
                payment_methods = []
                if "CARD" in methods_raw:
                    payment_methods.append("CARD")
                if "BANK" in methods_raw or "ACH" in methods_raw:
                    payment_methods.append("BANK_ACCOUNT")
                if not payment_methods:
                    payment_methods = ["CARD", "BANK_ACCOUNT"]
    except Exception:
        pass

    return {
        "tier_name": tier_name,
        "payment_schedule": payment_schedule,
        "payment_methods": payment_methods,
    }


def resolve_products_and_prices(state: InvoiceState) -> InvoiceState:
    """Calculate prices using the confirmed tier. Deterministic — no LLM."""
    from services.product_service import calculate_invoice_prices

    tier_name = state.get("tier_name", "Direct")
    items = state.get("extracted", {}).get("items", [])

    result = calculate_invoice_prices(tier_name, items)

    out: InvoiceState = {
        "pricing_result": result,
        "line_items": result["line_items"],
        # Set the flag the control/UI layer reads to detect a price-confirmation
        # checkpoint. Cleared once every variable-priced item has a price.
        "awaiting_price": result.get("needs_price") or None,
    }

    # Hard blocks (product not found, tier unavailable, …) are unrecoverable —
    # surface them and stop. Variable-pricing items are NOT blocks: they route to
    # confirm_item_prices to ask the operator (see _route_after_pricing).
    if result["blocks"]:
        out["final_response"] = (
            "Could not price some items:\n" + "\n".join(f"  - {b}" for b in result["blocks"])
        )
    return out


def _parse_price_to_cents(text) -> int | None:
    """Parse an operator's typed price ('45', '$45', '45.00', '1,250') to cents."""
    if text is None:
        return None
    import re as _re
    m = _re.search(r"(\d+(?:\.\d{1,2})?)", str(text).replace(",", ""))
    if not m:
        return None
    return int(round(float(m.group(1)) * 100))


def confirm_item_prices(state: InvoiceState) -> InvoiceState:
    """Interrupt to ask the operator for a per-bottle price on a variable-pricing
    item, then attach it to the matching extracted item and re-price."""
    needs = (state.get("pricing_result", {}) or {}).get("needs_price", []) or []
    if not needs:
        return {"awaiting_price": None}

    target = needs[0]
    label = target.get("label") or target.get("product_name") or "this item"
    tier = state.get("tier_name", "")

    tier_note = f" the {tier}" if tier else " the selected"
    response = interrupt({
        "type": "price_confirmation",
        "item": label,
        "question": (
            f"I don't have a price on file for “{label}”, and none was stated in the order. "
            f"What's the regular price per bottle? Reply with the amount (e.g. 45 or $45.00) — "
            f"I'll apply{tier_note} pricing to it."
        ),
    })

    cents = _parse_price_to_cents(response)
    extracted = dict(state.get("extracted", {}) or {})
    items = list(extracted.get("items", []) or [])
    if cents is not None:
        idx = target.get("item_index")
        applied = False
        # Primary: apply by index (robust — the order may omit the vintage, so
        # name/vintage matching against the catalog product is unreliable).
        if isinstance(idx, int) and 0 <= idx < len(items):
            items[idx]["regular_unit_price_cents"] = cents
            applied = True
        else:
            # Fallback (e.g. checkpoint written before item_index existed):
            # match by product name, ignoring vintage — the order often omits it.
            pn = (target.get("product_name") or "").lower()
            for it in items:
                ipn = (it.get("product_name") or "").lower()
                if pn and (pn in ipn or ipn in pn) and not _has_price(it):
                    it["regular_unit_price_cents"] = cents
                    applied = True
                    break
        logging.info("[invoice] price_confirmation: %s = %s cents (applied=%s)",
                     label, cents, applied)
    else:
        logging.warning("[invoice] price_confirmation: could not parse price from %r", response)
    extracted["items"] = items
    # Clear the flag; resolve_products_and_prices will re-set it if another
    # variable-priced item still needs a price (loops until all are priced).
    return {"extracted": extracted, "awaiting_price": None}


def create_invoice_preview(state: InvoiceState) -> InvoiceState:
    """Format the priced draft into a human-readable approval message."""
    from services.approval_service import format_approval_request

    customer = state.get("customer", {})
    pricing = state.get("pricing_result", {})

    preview_text = format_approval_request(
        customer_name=customer.get("full_name", "Unknown"),
        customer_company=customer.get("company"),
        tier_name=state.get("tier_name", "?"),
        line_items=state.get("line_items", []),
        subtotal_cents=pricing.get("subtotal_cents", 0),
        discount_cents=pricing.get("discount_cents", 0),
        total_before_tax_cents=pricing.get("total_before_tax_cents", 0),
        shipping_cents=pricing.get("shipping_cents"),
        warnings=pricing.get("warnings", []),
        missing_fields=pricing.get("blocks", []),
    )

    preview = {
        "customer": customer,
        "tier_name": state.get("tier_name"),
        "line_items": state.get("line_items", []),
        "subtotal_cents": pricing.get("subtotal_cents", 0),
        "discount_cents": pricing.get("discount_cents", 0),
        "total_before_tax_cents": pricing.get("total_before_tax_cents", 0),
        "shipping_cents": pricing.get("shipping_cents"),
        "payment_schedule": state.get("payment_schedule", "NET_30"),
        "payment_methods": state.get("payment_methods", ["CARD", "BANK_ACCOUNT"]),
        "preview_text": preview_text,
    }
    return {"invoice_preview": preview, "final_response": preview_text}


# Strict token sets for high-risk gates (approve/send).
# Token matching only — no substring search — so "not approved" never matches "approved".
_STRICT_APPROVE_TOKENS = {"approve", "approved", "yes", "ok", "confirm", "confirmed"}
_STRICT_REJECT_TOKENS  = {"reject", "rejected", "no", "cancel", "stop", "abort", "nevermind"}
_STRICT_EDIT_TOKENS    = {"edit", "change", "modify", "update", "fix", "adjust", "revise"}


def _parse_approval(raw: str) -> str:
    """Strict token-based parser for high-risk approval gate.

    Splits input into individual tokens and checks set membership.
    Substring patterns like 'not approved' will NOT match because
    'not' will appear alongside 'approved' and 'not' is a reject signal.
    Safe default: rejected.
    """
    tokens = set(raw.lower().strip().rstrip(".").split())
    # If any reject token present alongside an approve token, reject wins
    has_approve = bool(tokens & _STRICT_APPROVE_TOKENS)
    has_reject  = bool(tokens & _STRICT_REJECT_TOKENS)
    has_edit    = bool(tokens & _STRICT_EDIT_TOKENS)

    if has_reject:
        return "rejected"
    if has_edit:
        return "edit_requested"
    if has_approve:
        return "approved"
    return "rejected"  # safe default for unrecognized input


def approval_gate(state: InvoiceState) -> InvoiceState:
    """Pause until Cecil/Audrey approves, rejects, or requests edits.

    Resume with: "approved" | "rejected" | "edit_requested"
    Also understands natural language: "looks good", "go ahead", "cancel", etc.
    """
    decision = interrupt({
        "type": "invoice_approval_required",
        "invoice_preview": state.get("invoice_preview", {}),
        "question": "Approve creating this invoice draft in Square?",
    })
    return {"approval": _parse_approval(str(decision))}


def _ikey(case_id: str, action: str) -> str:
    """Deterministic idempotency key for Square mutations.

    Same case_id + action always returns the same 45-char hex string.
    Guarantees that a retry after a timeout never creates a duplicate order/invoice.
    """
    from services.square_service import _ikey as _sq_ikey
    return _sq_ikey(case_id, action)


def create_square_invoice_draft(state: InvoiceState) -> InvoiceState:
    """Create Square order + invoice draft after approval."""
    from services.tool_registry import tool_registry, ToolError
    from services.approval_service import log_approval_event

    approval = state.get("approval", "rejected")
    if approval != "approved":
        try:
            log_approval_event(
                draft_summary=str(state.get("invoice_preview", {}))[:200],
                action=approval,
            )
        except Exception:
            pass
        if approval == "edit_requested":
            return {
                "final_response": (
                    "Invoice set aside — nothing sent to Square.\n"
                    "What needs to change? Paste the corrected order (include customer + items) and I'll re-process it."
                ),
            }
        return {"final_response": "Invoice rejected — nothing created in Square."}

    customer = state.get("customer", {})
    email = customer.get("email") or state.get("extracted", {}).get("email") or ""
    full_name = customer.get("full_name", "Unknown Customer")
    case_id = state.get("_case_id", "")

    # Step 1: get or create Square customer
    try:
        if email:
            sq_customer = tool_registry.dispatch(
                "square_create_customer",
                {"email": email, "full_name": full_name,
                 "idempotency_key": _ikey(case_id, "create_customer")},
                case_id=case_id,
            )
            customer_id = sq_customer["customer_id"]
        elif customer.get("square_customer_id"):
            customer_id = customer["square_customer_id"]
        else:
            return {"final_response": "Cannot create Square invoice: no email or Square customer ID."}
    except ToolError as e:
        return {"final_response": f"Square customer error: {e.reason}"}

    # Step 2: create order
    try:
        order_result = tool_registry.dispatch(
            "square_create_order",
            {"customer_name": full_name, "line_items": state.get("line_items", []),
             "idempotency_key": _ikey(case_id, "create_order")},
            case_id=case_id,
        )
    except ToolError as e:
        return {"final_response": f"Square order error: {e.reason}"}

    order_id = order_result["order_id"]

    # Step 3: create invoice draft via tool registry (SHARE_MANUALLY)
    try:
        invoice_result = tool_registry.dispatch(
            "square_create_invoice_draft",
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "title": "Winefornia Invoice",
                "payment_schedule": state.get("payment_schedule", "NET_30"),
                "accepted_payment_methods": state.get("payment_methods", ["CARD", "BANK_ACCOUNT"]),
                "idempotency_key": _ikey(case_id, "create_invoice"),
            },
            case_id=case_id,
        )
    except ToolError as e:
        return {"final_response": f"Square invoice error: {e.reason}"}

    # Persist to Supabase — observability-tier, best-effort.
    # If this fails after Square draft was created, flag for manual reconciliation.
    _recon_needed = False
    try:
        tool_registry.dispatch(
            "supabase_log_invoice",
            {"record": _build_invoice_log(state, customer, email, full_name, order_id, invoice_result)},
            case_id=case_id,
        )
    except Exception as _log_err:
        _recon_needed = True
        import logging as _logging
        _logging.warning(
            "[invoice] Supabase log failed after Square draft created — reconciliation needed. "
            "invoice_id=%s order_id=%s error=%s",
            invoice_result.get("invoice_id"), order_id, _log_err,
        )

    try:
        log_approval_event(
            draft_summary=f"{full_name} / {state.get('tier_name')}",
            action="approved",
            approver=state.get("sender_id", "unknown"),
        )
    except Exception:
        pass

    out: dict = {
        "square_order_id": order_id,
        "square_invoice_id": invoice_result["invoice_id"],
        "square_invoice_version": invoice_result.get("invoice_version", 0),
    }
    if _recon_needed:
        out["reconciliation_needed"] = True
        out["reconciliation_reason"] = (
            f"Square draft created (invoice_id={invoice_result['invoice_id']}, "
            f"order_id={order_id}) but Supabase invoice log failed. "
            "Manually verify and log this invoice."
        )
    return out


def _build_invoice_log(state, customer, email, full_name, order_id, invoice_result):
    from db.models import InvoiceLog
    pricing = state.get("pricing_result", {})
    return InvoiceLog(
        thread_id=state.get("sender_id", invoice_result["invoice_id"]),
        sender_id=state.get("sender_id"),
        raw_message=state.get("raw_message"),
        customer_id=customer.get("square_customer_id") or customer.get("id"),
        customer_name=full_name,
        customer_email=email or None,
        tier_name=state.get("tier_name"),
        line_items=state.get("line_items", []),
        subtotal_cents=pricing.get("subtotal_cents"),
        discount_cents=pricing.get("discount_cents"),
        total_before_tax_cents=pricing.get("total_before_tax_cents"),
        shipping_cents=pricing.get("shipping_cents"),
        payment_schedule=state.get("payment_schedule"),
        payment_methods=state.get("payment_methods", []),
        approval="approved",
        square_order_id=order_id,
        square_invoice_id=invoice_result["invoice_id"],
    )


def confirm_send(state: InvoiceState) -> InvoiceState:
    """Interrupt: decide whether to publish (send to client) or keep as draft.

    Resume with: "send" | "draft"
    """
    preview = state.get("invoice_preview", {})
    customer_name = state.get("customer", {}).get("full_name", "customer")
    total = preview.get("total_before_tax_cents", 0) / 100

    decision = interrupt({
        "type": "confirm_send_to_client",
        "invoice_id": state.get("square_invoice_id"),
        "customer": customer_name,
        "total": f"${total:.2f}",
        "question": (
            f"Draft created for {customer_name} (${total:.2f}).\n"
            f"Square Invoice ID: {state.get('square_invoice_id')}\n\n"
            "Type 'send' to publish and send to client, or 'draft' to keep as draft only."
        ),
    })

    tokens = set(str(decision).strip().lower().split())
    send_decision = "send" if tokens & {"send", "yes", "publish", "ship"} else "draft"
    return {"send_decision": send_decision}


def publish_invoice_node(state: InvoiceState) -> InvoiceState:
    """Publish the Square invoice draft — this sends it to the client."""
    from services.square_service import publish_invoice

    result = publish_invoice(
        invoice_id=state["square_invoice_id"],
        invoice_version=state.get("square_invoice_version", 0),
        idempotency_key=_ikey(state.get("_case_id", ""), "publish_invoice"),
    )
    if "error" in result:
        return {"final_response": f"Publish failed: {result['error']}. Invoice draft still saved in Square."}

    customer_name = state.get("customer", {}).get("full_name", "customer")
    preview = state.get("invoice_preview", {})
    total = preview.get("total_before_tax_cents", 0) / 100
    pay_url = result.get("public_url") or ""
    msg = (
        f"Invoice sent to {customer_name}.\n"
        f"Total: ${total:.2f}\n"
        f"Square Invoice ID: {state['square_invoice_id']}"
    )
    if pay_url:
        msg += f"\nPay link: {pay_url}"
    return {
        "square_invoice_url": pay_url,
        "final_response": msg,
    }


def offer_email_receipt(state: InvoiceState) -> InvoiceState:
    """Interrupt: offer to send a receipt email after Square invoice is published."""
    customer = state.get("customer", {})
    customer_email = customer.get("email") or state.get("extracted", {}).get("email")

    if not customer_email:
        # No email on file — skip this step silently
        return {"email_receipt_decision": "skip"}

    customer_name = customer.get("full_name") or customer.get("company") or "customer"
    sq_id = state.get("square_invoice_id", "")

    decision = interrupt({
        "type": "offer_email_receipt",
        "invoice_id": sq_id,
        "customer": customer_name,
        "customer_email": customer_email,
        "question": (
            f"Invoice published.\n"
            f"Send receipt email to {customer_name} at {customer_email}?\n"
            "Type 'send' to email the receipt, or 'skip' to finish."
        ),
    })

    tokens = set(str(decision).strip().lower().split())
    send_it = bool(tokens & {"send", "yes", "email", "sure", "go"})
    return {"email_receipt_decision": "send" if send_it else "skip"}


def send_receipt_email_node(state: InvoiceState) -> InvoiceState:
    """Send a Claude-composed receipt email to the customer via Gmail."""
    from services.tool_registry import tool_registry, ToolError

    try:
        result = tool_registry.dispatch(
            "gmail_send_receipt",
            {"state": state},
            case_id=state.get("_case_id", ""),
        )
    except ToolError as e:
        return {"final_response": state.get("final_response", "") + f"\n(Email not sent: {e.reason})"}

    to = result.get("to", "")
    base = state.get("final_response", "")
    return {
        "receipt_sent_to": to,
        "final_response": base + f"\nReceipt email sent to {to}.",
    }


def interpret_edit(state: InvoiceState) -> InvoiceState:
    """Ask Cecil what to change, then call Claude Haiku to produce a structured patch.

    The LLM produces the patch. Deterministic apply_patch applies it.
    Max 2 edit rounds — after that Cecil must resubmit from scratch.
    """
    rounds = state.get("edit_rounds", 0)
    if rounds >= 2:
        return {
            "final_response": (
                "Two edit rounds used — please resubmit the full corrected order "
                "so I can start fresh."
            ),
            "approval": "rejected",
        }

    # Ask Cecil what to change
    preview = state.get("invoice_preview", {})
    preview_text = preview.get("preview_text", "")

    instruction = interrupt({
        "type": "edit_instruction",
        "invoice_preview": preview,
        "question": (
            "What would you like to change?\n"
            "Describe the edit (e.g. 'make it 6 bottles', 'change to NET_14', "
            "'different customer — use Oak Barrel instead')."
        ),
    })

    patch = _llm_parse_edit(str(instruction), state)
    return {
        "edit_instruction": str(instruction),
        "edit_patch": patch,
        "edit_rounds": rounds + 1,
        "approval": None,           # clear old approval so graph can re-enter approval_gate
    }


def _llm_parse_edit(instruction: str, state: InvoiceState) -> dict:
    """Call Claude Haiku to parse the edit instruction into a structured patch dict."""
    customer = state.get("customer", {})
    line_items = state.get("line_items", [])
    tier = state.get("tier_name", "")

    context = json.dumps({
        "customer": customer.get("full_name"),
        "tier": tier,
        "line_items": [
            {"product": i.get("product_name"), "qty": i.get("quantity"), "unit": i.get("unit_type")}
            for i in line_items[:5]
        ],
        "payment_schedule": state.get("payment_schedule"),
    }, default=str)

    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
        result = llm.invoke([
            SystemMessage(content=(
                "You help a winery parse invoice edit instructions into structured patches. "
                "Given the current invoice and a natural language edit, return a JSON patch:\n"
                '{"field_changes": [{"field": "...", "old_value": ..., "new_value": ..., "confidence": 0.0-1.0}], '
                '"requires_price_recalculation": true/false}\n'
                "Field paths: line_items[N].quantity, line_items[N].unit_type, line_items[N].product_name, "
                "tier_name, payment_schedule, customer_name. "
                "Return ONLY the JSON, no markdown."
            )),
            HumanMessage(content=f"Current invoice: {context}\nEdit instruction: {instruction}"),
        ])
        content = result.content.strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1].lstrip("json").strip().rsplit("```", 1)[0].strip()
        return json.loads(content)
    except Exception:
        return {"field_changes": [], "requires_price_recalculation": False, "_parse_error": True}


def apply_patch(state: InvoiceState) -> InvoiceState:
    """Deterministically apply the structured patch from interpret_edit.

    If any field_change has confidence < 0.80, interrupt with a targeted
    clarification question for that specific field only.
    Applies the patch to extracted/line_items/tier_name/payment_schedule.
    """
    patch = state.get("edit_patch", {})
    changes = patch.get("field_changes", [])

    if not changes or patch.get("_parse_error"):
        return {
            "final_response": (
                "I couldn't parse that edit — could you rephrase? "
                "(e.g. 'change quantity to 6 cases', 'switch to NET_14')"
            ),
            "approval": None,
        }

    # Check for low-confidence changes
    low_conf = [c for c in changes if c.get("confidence", 1.0) < 0.80]
    if low_conf:
        field = low_conf[0].get("field", "field")
        old_v = low_conf[0].get("old_value", "?")
        new_v = low_conf[0].get("new_value", "?")
        clarification = interrupt({
            "type": "edit_clarification",
            "field": field,
            "question": f"Just to confirm: change {field} from {old_v!r} to {new_v!r}?",
        })
        tokens = set(str(clarification).strip().lower().split())
        if not (tokens & {"yes", "y", "correct", "confirmed"}):
            return {"final_response": "Edit cancelled — nothing changed.", "approval": None}

    # Apply changes
    extracted   = dict(state.get("extracted", {}))
    line_items  = [dict(i) for i in state.get("line_items", [])]
    tier_name   = state.get("tier_name")
    sched       = state.get("payment_schedule")

    for change in changes:
        field   = change.get("field", "")
        new_val = change.get("new_value")
        try:
            if field.startswith("line_items["):
                # e.g. "line_items[0].quantity"
                parts = field.split(".", 1)
                idx   = int(parts[0].replace("line_items[", "").replace("]", ""))
                attr  = parts[1] if len(parts) > 1 else ""
                if 0 <= idx < len(line_items) and attr:
                    line_items[idx][attr] = new_val
            elif field == "tier_name":
                tier_name = new_val
            elif field == "payment_schedule":
                sched = new_val
            elif field == "customer_name":
                extracted["customer_name"] = new_val
        except Exception:
            pass

    out: dict = {
        "extracted": extracted,
        "line_items": line_items,
        "approval": None,           # will go back to approval_gate after re-pricing
    }
    if tier_name:
        out["tier_name"] = tier_name
    if sched:
        out["payment_schedule"] = sched

    return out


def respond(state: InvoiceState) -> InvoiceState:
    recon_suffix = ""
    if state.get("reconciliation_needed"):
        recon_suffix = (
            "\n\n⚠️ RECONCILIATION NEEDED: "
            + state.get("reconciliation_reason", "Partial success — verify in Square and Supabase.")
        )

    if state.get("send_decision") == "draft" and state.get("square_invoice_id"):
        preview = state.get("invoice_preview", {})
        total = preview.get("total_before_tax_cents", 0) / 100
        customer_name = state.get("customer", {}).get("full_name", "customer")
        return {
            "final_response": (
                f"Draft saved for {customer_name} (${total:.2f}).\n"
                f"Square Invoice ID: {state['square_invoice_id']}\n"
                f"(NOT sent — share manually from Square when ready.)"
                + recon_suffix
            )
        }
    base = state.get("final_response", "Done.")
    return {"final_response": base + recon_suffix}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _route_after_classification(state: InvoiceState) -> str:
    if state.get("intent") == "invoice_request":
        return "extract_invoice_fields"
    return "chat_response"


def _route_after_missing_fields(state: InvoiceState) -> str:
    return "respond" if state.get("missing_fields") else "resolve_customer"


def _route_after_customer(state: InvoiceState) -> str:
    # Explicit False = fuzzy match that needs Cecil's confirmation
    if state.get("customer_confirmed") is False:
        return "clarify_customer_match"
    return "confirm_tier_and_payment"


def _route_after_customer_confirmation(state: InvoiceState) -> str:
    if state.get("customer_confirmed"):
        return "confirm_tier_and_payment"
    return "respond"


def _route_after_draft_created(state: InvoiceState) -> str:
    return "confirm_send" if state.get("square_invoice_id") else "respond"


def _route_after_send_decision(state: InvoiceState) -> str:
    return "publish_invoice" if state.get("send_decision") == "send" else "respond"


def _route_after_email_offer(state: InvoiceState) -> str:
    return "send_receipt_email" if state.get("email_receipt_decision") == "send" else "respond"


def _route_after_pricing(state: InvoiceState) -> str:
    pr = state.get("pricing_result", {}) or {}
    if pr.get("blocks"):
        return "respond"
    if pr.get("needs_price"):
        return "confirm_item_prices"   # ask the operator for a price, then re-price
    return "create_invoice_preview"


def _route_after_approval(state: InvoiceState) -> str:
    approval = state.get("approval", "rejected")
    if approval == "approved":
        return "create_square_invoice_draft"
    if approval == "edit_requested":
        return "interpret_edit"
    return "create_square_invoice_draft"   # create_square_invoice_draft handles rejected too


def _route_after_patch(state: InvoiceState) -> str:
    """After apply_patch: re-price if needed, else go straight to preview."""
    patch = state.get("edit_patch", {})
    if patch.get("requires_price_recalculation", False):
        return "resolve_products_and_prices"
    return "create_invoice_preview"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_invoice_graph(checkpointer=None):
    g = StateGraph(InvoiceState)

    g.add_node("classify_intent", classify_intent)
    g.add_node("chat_response", chat_response)
    g.add_node("extract_invoice_fields", extract_invoice_fields)
    g.add_node("ask_missing_fields", ask_missing_fields)
    g.add_node("resolve_customer", resolve_customer)
    g.add_node("clarify_customer_match", clarify_customer_match)
    g.add_node("confirm_tier_and_payment", confirm_tier_and_payment)
    g.add_node("resolve_products_and_prices", resolve_products_and_prices)
    g.add_node("confirm_item_prices", confirm_item_prices)
    g.add_node("create_invoice_preview", create_invoice_preview)
    g.add_node("approval_gate", approval_gate)
    g.add_node("interpret_edit", interpret_edit)
    g.add_node("apply_patch", apply_patch)
    g.add_node("create_square_invoice_draft", create_square_invoice_draft)
    g.add_node("confirm_send", confirm_send)
    g.add_node("publish_invoice", publish_invoice_node)
    g.add_node("offer_email_receipt", offer_email_receipt)
    g.add_node("send_receipt_email", send_receipt_email_node)
    g.add_node("respond", respond)

    g.add_edge(START, "classify_intent")
    g.add_conditional_edges(
        "classify_intent",
        _route_after_classification,
        {"extract_invoice_fields": "extract_invoice_fields", "chat_response": "chat_response"},
    )
    g.add_edge("chat_response", "respond")
    g.add_edge("extract_invoice_fields", "ask_missing_fields")
    g.add_conditional_edges(
        "ask_missing_fields",
        _route_after_missing_fields,
        {"resolve_customer": "resolve_customer", "respond": "respond"},
    )
    g.add_conditional_edges(
        "resolve_customer",
        _route_after_customer,
        {"clarify_customer_match": "clarify_customer_match", "confirm_tier_and_payment": "confirm_tier_and_payment"},
    )
    g.add_conditional_edges(
        "clarify_customer_match",
        _route_after_customer_confirmation,
        {"confirm_tier_and_payment": "confirm_tier_and_payment", "respond": "respond"},
    )
    g.add_edge("confirm_tier_and_payment", "resolve_products_and_prices")
    g.add_conditional_edges(
        "resolve_products_and_prices",
        _route_after_pricing,
        {
            "create_invoice_preview": "create_invoice_preview",
            "confirm_item_prices": "confirm_item_prices",
            "respond": "respond",
        },
    )
    # After the operator confirms a price, re-price (loops until all priced).
    g.add_edge("confirm_item_prices", "resolve_products_and_prices")
    g.add_edge("create_invoice_preview", "approval_gate")
    g.add_conditional_edges(
        "approval_gate",
        _route_after_approval,
        {
            "create_square_invoice_draft": "create_square_invoice_draft",
            "interpret_edit": "interpret_edit",
        },
    )
    g.add_edge("interpret_edit", "apply_patch")
    g.add_conditional_edges(
        "apply_patch",
        _route_after_patch,
        {
            "resolve_products_and_prices": "resolve_products_and_prices",
            "create_invoice_preview": "create_invoice_preview",
        },
    )
    g.add_conditional_edges(
        "create_square_invoice_draft",
        _route_after_draft_created,
        {"confirm_send": "confirm_send", "respond": "respond"},
    )
    g.add_conditional_edges(
        "confirm_send",
        _route_after_send_decision,
        {"publish_invoice": "publish_invoice", "respond": "respond"},
    )
    g.add_edge("publish_invoice", "offer_email_receipt")
    g.add_conditional_edges(
        "offer_email_receipt",
        _route_after_email_offer,
        {"send_receipt_email": "send_receipt_email", "respond": "respond"},
    )
    g.add_edge("send_receipt_email", "respond")
    g.add_edge("respond", END)

    return g.compile(checkpointer=checkpointer)


def _make_checkpointer():
    """Return a Postgres checkpointer via Supabase pgBouncer (port 6543).

    pgBouncer runs in transaction pooling mode, so we must:
      - set prepare_threshold=None (disables server-side prepared statements;
        prepare_threshold=0 means "prepare on first use" and WILL collide here)
      - set autocommit=True      (LangGraph manages its own transactions)
      - append sslmode=require   (Supabase requires TLS)

    Production mode (PRODUCTION_MODE=true):
      - POSTGRES_CONNECTION_STRING is required. Missing or failed → RuntimeError.
      - MemorySaver fallback is DISABLED. A process restart would silently erase
        paused approval checkpoints, causing duplicate Square mutations on resume.

    Dev mode (default):
      - Falls back to MemorySaver when DB is unavailable.
    """
    import logging
    from app.config import POSTGRES_CONNECTION_STRING, PRODUCTION_MODE

    if not POSTGRES_CONNECTION_STRING:
        if PRODUCTION_MODE:
            raise RuntimeError(
                "[checkpointer] POSTGRES_CONNECTION_STRING is required in production mode. "
                "Invoice flow disabled until Postgres is reachable. "
                "Set PRODUCTION_MODE=false to allow MemorySaver fallback in dev."
            )
        logging.info("[checkpointer] POSTGRES_CONNECTION_STRING not set — using MemorySaver (dev only)")
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()

    try:
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver

        # Ensure Supabase TLS requirement is met
        conninfo = POSTGRES_CONNECTION_STRING
        if "sslmode" not in conninfo:
            sep = "&" if "?" in conninfo else "?"
            conninfo = f"{conninfo}{sep}sslmode=require"

        # Pool sizing/health is the difference between a stable bot and the
        # "couldn't get a connection after 30.00 sec" / "SSL SYSCALL error:
        # Operation timed out" failures seen in production:
        #   - check=check_connection  → validate (and discard) a connection
        #     before handing it out, so Supabase/pgBouncer-killed idle sockets
        #     never reach the agent as a dead connection.
        #   - max_idle / max_lifetime → proactively recycle connections the
        #     pooler would otherwise reap server-side and leave us holding.
        #   - timeout=10              → fail fast instead of hanging 30s when
        #     the pool is genuinely exhausted (surfaces the real problem).
        #   - max_size=10             → a single bot never needs 20; a smaller
        #     ceiling stays well under Supabase pooler client limits.
        pool = ConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=10,
            timeout=10.0,            # max wait for a free connection (was 30s default)
            max_idle=300.0,          # close connections idle > 5 min
            max_lifetime=1800.0,     # recycle every 30 min (beat pooler idle-kill)
            check=ConnectionPool.check_connection,  # validate before handing out
            kwargs={
                "autocommit": True,
                # pgBouncer/Supabase TRANSACTION pooler shares physical
                # connections across clients, so server-side prepared statements
                # collide ('prepared statement "_pg3_0" already exists').
                # None = never use named prepared statements. (0 would mean
                # "prepare on first use" — the exact opposite, and the bug.)
                "prepare_threshold": None,
            },
            open=False,
        )
        # Retry the connect+setup a few times. On a rolling deploy, all machines
        # reconnect at once and the Supabase pooler can momentarily refuse a
        # connection (transient PoolTimeout). Without this, a single hiccup would
        # crash the process in production mode and trigger a restart storm.
        import time as _time
        last_exc = None
        for attempt in range(1, 5):
            try:
                pool.open(wait=True, timeout=10.0)
                checkpointer = PostgresSaver(pool)
                checkpointer.setup()   # creates langgraph checkpoint tables (idempotent)
                logging.info(
                    "[checkpointer] Connected to Postgres via pgBouncer "
                    "(pool min=1 max=10, check+recycle enabled, attempt %d) — persistent",
                    attempt,
                )
                return checkpointer
            except Exception as e:
                last_exc = e
                logging.warning("[checkpointer] connect attempt %d/4 failed: %r", attempt, e)
                if attempt < 4:
                    _time.sleep(3.0)
        raise last_exc

    except Exception as exc:
        if PRODUCTION_MODE:
            raise RuntimeError(
                f"[checkpointer] Cannot connect to Postgres in production mode ({exc!r}). "
                "Invoice flow disabled — unsafe to continue with MemorySaver when paused "
                "checkpoints must survive process restarts. Fix POSTGRES_CONNECTION_STRING "
                "or set PRODUCTION_MODE=false to allow MemorySaver fallback."
            ) from exc
        logging.warning(
            f"[checkpointer] Postgres unavailable ({exc!r}), falling back to MemorySaver (dev only). "
            "Paused conversations will not survive a restart."
        )
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


checkpointer = _make_checkpointer()
invoice_graph = build_invoice_graph(checkpointer=checkpointer)
