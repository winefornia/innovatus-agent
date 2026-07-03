"""Read + write tools for the invoice Google Chat assistant.

This is the invoicing counterpart to vertex_agent.chat_actions (the tasting-room
assistant). The invoice chat agent understands free-form intent — "what's
wholesale on the 2023 Viognier?", "set the FOB price to $41", "make Corporate 25%
off", "make the 2023 Viognier unavailable for wholesale", "invoice Oak Barrel for
3 cases of Cabernet at wholesale and send it" — but it can ONLY do a tight,
allow-listed set of things. It can't do "a lot of random shit": every tool maps
to one concrete, named action, and anything that touches money or live pricing is
CONFIRM-FIRST.

Safety model (mirrors chat_actions):
- READ tools (find_products, get_pricing, list_tiers, recent_invoices,
  price_order) answer immediately and never mutate.
- WRITE tools (stage_*) record the intent in a per-user pending store and return a
  one-line "reply yes to confirm" summary. The real mutation only runs when
  confirm_pending_action() fires on the user's affirming reply. Because each chat
  turn is a fresh, memory-less agent, the pending action is held server-side and
  re-injected into the next message by invoice_chat_agent.discuss().

Pricing edits write to BOTH Supabase (the live source services.product_service
reads first) and the app/data JSON in lockstep, so the two never drift — the
team's rule is "JSON edits need a Supabase sync to go live." Supabase is written
FIRST; if it fails we leave the JSON untouched and report, so a chat edit never
introduces drift on its own.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import unicodedata
import uuid
from typing import Any

log = logging.getLogger(__name__)

# Set by invoice_chat_agent.discuss() at the start of each turn so tools know which
# allow-listed approver is acting (audit trail + keys the pending-confirm store).
_CURRENT_USER: "contextvars.ContextVar[str]" = contextvars.ContextVar("inv_chat_user", default="")

# user -> {"kind": str, "params": dict, "summary": str, "ts": float}
_PENDING: dict[str, dict[str, Any]] = {}
_PENDING_TTL = 600  # seconds; a staged-but-unconfirmed action expires after 10 min

# tier_prices channels carried per-variety on the Retail Accounts SKU sheet.
# "retail" is MSRP (edit that via stage_set_msrp, not here).
_CHANNELS = {"club_member", "wholesale", "fob", "ex_cellar"}
_CHANNEL_ALIASES = {
    "club": "club_member", "club member": "club_member", "member": "club_member",
    "whsl": "wholesale", "wsl": "wholesale",
    "export": "fob", "fob/export": "fob",
    "ex-cellar": "ex_cellar", "excellar": "ex_cellar", "employee": "ex_cellar",
}


def set_current_user(user: str) -> None:
    _CURRENT_USER.set(user or "")


def _user() -> str:
    return _CURRENT_USER.get() or "gchat_unknown"


def _money(cents: int | None) -> str:
    if cents in (None, ""):
        return "—"
    return f"${cents / 100:,.2f}"


_ALLOWED_SCHEDULES = {"UPON_RECEIPT", "NET_7", "NET_14", "NET_30"}


def _amount_to_cents(value: Any) -> int | None:
    """Dollars (number or "$58.00" / "1,200") → integer cents, or None if unparseable.

    The agent is told to pass numbers, but staff phrasing leaks through ("$58"), so
    we strip currency/grouping defensively rather than crash on float("$58").
    """
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
            if not value:
                return None
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _shipping_to_cents(value: Any) -> int | None:
    """Parse invoice-chat shipping input.

    Returns None when shipping is genuinely unanswered. "free"/"waived" and 0
    are valid answers and return 0.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if any(tok in text for tok in ("free", "waive", "waived", "no shipping")):
            return 0
        if text in {"-1", "unknown", "tbd", "not sure"}:
            return None
    try:
        cents = _amount_to_cents(value)
    except Exception:
        return None
    if cents is None:
        return None
    return max(0, cents)


def _to_float(value: Any) -> float | None:
    """Parse a number that may arrive as "25%", "0.7", or 25. None if unparseable."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
            if not value:
                return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_schedule(schedule: str) -> str:
    s = (schedule or "NET_30").upper().replace(" ", "_").replace("-", "_")
    return s if s in _ALLOWED_SCHEDULES else "NET_30"


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode("ascii")


# ── pending-confirmation store ───────────────────────────────────────────────

def peek_pending(user: str) -> dict | None:
    """Return the live (non-expired) pending action for a user, or None.

    Called by discuss() to decide whether to inject a confirmation note.
    """
    entry = _PENDING.get(user or "")
    if not entry:
        return None
    if time.time() - entry["ts"] > _PENDING_TTL:
        _PENDING.pop(user, None)
        return None
    return entry


def _stage(kind: str, params: dict, summary: str) -> str:
    _PENDING[_user()] = {"kind": kind, "params": params, "summary": summary, "ts": time.time()}
    return summary


def confirm_pending_action() -> str:
    """Execute the action the user previously staged and just confirmed.

    Call this ONLY when the user's latest message affirms a pending action
    (e.g. "yes", "do it", "send it", "go ahead"). It performs the real mutation —
    writing the price/tier/availability change, or creating/sending the invoice.
    """
    entry = peek_pending(_user())
    if not entry:
        return "There's nothing waiting for confirmation right now."
    _PENDING.pop(_user(), None)
    kind, params = entry["kind"], entry["params"]
    try:
        if kind == "set_channel_price":
            return _exec_set_channel_price(**params)
        if kind == "set_msrp":
            return _exec_set_msrp(**params)
        if kind == "set_tier":
            return _exec_set_tier(**params)
        if kind == "set_availability":
            return _exec_set_availability(**params)
        if kind == "invoice":
            return _exec_invoice(**params)
        if kind == "send_existing":
            return _exec_send_existing(**params)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[inv:chat-actions] confirm failed (%s): %s", kind, exc)
        return f"That didn't go through — {exc}"
    return "I'm not sure what I was confirming — try again."


def cancel_pending_action() -> str:
    """Discard the staged action when the user declines (e.g. "no", "never mind")."""
    had = _PENDING.pop(_user(), None)
    return "Okay — left it as is, nothing changed." if had else "Nothing was staged, so nothing changed."


# ── product / tier resolution ────────────────────────────────────────────────

def _catalog() -> list[dict]:
    """The live catalog: Supabase first (source of truth), JSON as fallback."""
    from services.product_service import _load_catalog, _load_catalog_from_supabase

    return _load_catalog_from_supabase() or _load_catalog()


def _all_matches(query: str) -> list[dict]:
    """Every catalog product whose name matches the query (alias-aware)."""
    from services.product_service import _ALIASES

    q = _ascii(query)
    q = _ALIASES.get(q, q)
    if not q:
        return []
    return [p for p in _catalog() if q in _ascii(p.get("name", "")) or _ascii(p.get("name", "")) in q]


def _resolve_product(query: str, vintage: int | None = None, size: str = "") -> dict:
    """Resolve a name to one catalog product.

    Returns {"product": p} on a unique match, {"error": msg} when nothing matches,
    or {"ambiguous": [...]} when the name spans several vintages and none was given.
    When no size is given, prefers the standard 750ml bottle (and a non-variable
    entry) over half-bottles/variable-pricing SKUs — that's what staff mean.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "Which wine? Give me a name (and a vintage if there are several)."}
    matches = _all_matches(q)
    if vintage is not None:
        matches = [p for p in matches if p.get("vintage") == vintage]
    if not matches:
        return {"error": f"I couldn't find a wine matching \"{q}\"{f' {vintage}' if vintage else ''}."}

    # "cabernet" matches both Cabernet Sauvignon and Cabernet Franc — don't guess.
    if len({_ascii(p.get("name", "")) for p in matches}) > 1:
        return {"ambiguous": matches}
    if vintage is None and len({p.get("vintage") for p in matches}) > 1:
        return {"ambiguous": matches}

    if size:
        sized = [p for p in matches if str(p.get("size", "750ml")).lower() == size.lower()]
        matches = sized or matches
    elif len({str(p.get("size", "750ml")) for p in matches}) > 1:
        std = [p for p in matches if str(p.get("size", "750ml")).lower() == "750ml"]
        matches = std or matches

    # Prefer a normally-priced entry over a variable-pricing one.
    matches.sort(key=lambda p: (bool(p.get("variable_pricing")), str(p.get("size", "750ml"))))
    return {"product": matches[0]}


def _ambiguous_product_msg(hits: list[dict]) -> str:
    opts = ", ".join(
        f"{p.get('name')} {p.get('vintage') or ''} ({p.get('size', '750ml')})".strip()
        for p in hits[:6]
    )
    return f"There are a few — which one? {opts}"


def _resolve_tier(name: str) -> dict:
    from services.product_service import get_tier_by_name

    t = get_tier_by_name((name or "").strip())
    if not t:
        return {"error": f"I don't recognize the tier \"{name}\". Tiers: FOB/Export, Wholesale, "
                         f"Corporate, Employee, Wholesale Ambassadors, Corporate Lead, Club Member, "
                         f"Other, Direct."}
    return {"tier": t}


def _norm_channel(channel: str) -> str | None:
    c = (channel or "").strip().lower().replace(" ", "_").replace("-", "_")
    c = _CHANNEL_ALIASES.get((channel or "").strip().lower(), c)
    return c if c in _CHANNELS else None


# ── READ tools ───────────────────────────────────────────────────────────────

def find_products(query: str) -> list[dict]:
    """Find wines in the catalog by name (aliases like "cab", "sb" work).

    Use this to turn a name into concrete products before quoting or editing a
    price. Returns name, vintage, size, MSRP and per-channel prices.

    Args:
        query: a wine name or alias (e.g. "viognier", "cab").
    """
    hits = _all_matches(query)
    out = []
    for p in hits[:10]:
        out.append({
            "name": p.get("name"),
            "vintage": p.get("vintage"),
            "size": p.get("size", "750ml"),
            "msrp": _money(p.get("msrp_bottle_cents")),
            "tier_prices": {k: _money(v) for k, v in (p.get("tier_prices") or {}).items()},
            "tier_unavailable": p.get("tier_unavailable") or [],
            "variable_pricing": bool(p.get("variable_pricing")),
        })
    return out


def get_pricing(product: str, vintage: int = 0) -> dict:
    """Show the current prices for one wine: MSRP plus every per-channel price.

    Use for "what's wholesale on the Viognier?", "how much is the 2021 Cab at
    FOB?". Read-only.

    Args:
        product: wine name or alias.
        vintage: 4-digit year, or 0 if unspecified (will ask if ambiguous).
    """
    res = _resolve_product(product, vintage or None)
    if res.get("error"):
        return {"error": res["error"]}
    if res.get("ambiguous"):
        return {"need_vintage": _ambiguous_product_msg(res["ambiguous"])}
    p = res["product"]
    return {
        "name": p.get("name"),
        "vintage": p.get("vintage"),
        "size": p.get("size", "750ml"),
        "msrp": _money(p.get("msrp_bottle_cents")),
        "channel_prices": {k: _money(v) for k, v in (p.get("tier_prices") or {}).items()},
        "unavailable_for": p.get("tier_unavailable") or [],
        "variable_pricing": bool(p.get("variable_pricing")),
    }


def list_tiers() -> list[dict]:
    """List the pricing tiers with their discount % and MSRP multiplier. Read-only."""
    from services.product_service import _load_tiers, _load_tiers_from_supabase

    tiers = _load_tiers_from_supabase() or _load_tiers()
    return [
        {
            "name": t.get("name"),
            "discount_percent": t.get("discount_percent"),
            "msrp_multiplier": t.get("msrp_multiplier"),
            "channel": t.get("channel"),
        }
        for t in tiers
    ]


def recent_invoices(limit: int = 10) -> list[dict]:
    """List recent invoices (customer, tier, total, status). Read-only.

    Args:
        limit: how many to return (default 10).
    """
    from db.repository import list_recent_invoices

    try:
        rows = list_recent_invoices(limit=max(1, min(limit, 25)))
    except Exception as exc:  # pragma: no cover - defensive
        return [{"error": str(exc)}]
    out = []
    for r in rows:
        out.append({
            "customer": r.get("customer_name"),
            "tier": r.get("tier_name"),
            "total": _money(r.get("total_before_tax_cents")),
            "status": r.get("approval"),
            "square_invoice_id": r.get("square_invoice_id"),
            "created_at": r.get("created_at"),
        })
    return out


def price_order(customer_name: str, tier: str, items_json: str, customer_email: str = "") -> dict:
    """Compute a priced quote for an order WITHOUT creating anything. Read-only.

    Use this to show the customer total before staging an invoice, and to catch
    unpriced/unavailable items early.

    Args:
        customer_name: who the invoice is for.
        tier: pricing tier name (e.g. "Wholesale").
        items_json: a JSON array of items, each:
            {"product_name": str, "vintage": int|null, "quantity": number,
             "unit_type": "case"|"bottle", "unit_price": number|null}
            unit_price is the per-unit dollar price stated on the order, if any.
        customer_email: optional, for context only.
    """
    return _quote(tier, items_json)


# ── WRITE tools: pricing edits (confirm-first, both Supabase + JSON) ───────────

def stage_set_channel_price(product: str, channel: str, price: float, vintage: int = 0) -> str:
    """Stage changing one wine's per-channel price (wholesale / fob / club_member /
    ex_cellar), then ask the user to confirm. Does NOT write until they say yes.

    Use for "set the 2023 Viognier wholesale to $53", "FOB on the Cab to $41".

    Args:
        product: wine name or alias.
        channel: one of wholesale, fob, club_member, ex_cellar (aliases ok).
        price: new per-bottle price in DOLLARS (e.g. 53 or 53.00).
        vintage: 4-digit year, or 0 if unspecified (will ask if ambiguous).
    """
    chan = _norm_channel(channel)
    if not chan:
        return f"\"{channel}\" isn't a channel I can set. Use wholesale, fob, club_member, or ex_cellar. (MSRP/retail → use the MSRP tool.)"
    cents = _amount_to_cents(price)
    if cents is None or cents <= 0:
        return "That price doesn't look right — give me a positive dollar amount like 53 or 53.00."
    res = _resolve_product(product, vintage or None)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_product_msg(res["ambiguous"])
    p = res["product"]
    cur = (p.get("tier_prices") or {}).get(chan)
    label = f"{p['name']} {p.get('vintage') or ''} ({p.get('size', '750ml')})".strip()
    summary = (
        f"Set *{chan}* on {label} from {_money(cur)} to *{_money(cents)}*?\n"
        f"Writes to Supabase + the catalog JSON. Reply *yes* to confirm."
    )
    return _stage("set_channel_price", {
        "name": p["name"], "vintage": p.get("vintage"), "size": p.get("size", "750ml"),
        "channel": chan, "cents": cents,
    }, summary)


def stage_set_msrp(product: str, price: float, vintage: int = 0) -> str:
    """Stage changing one wine's MSRP (retail bottle price), then ask to confirm.
    MSRP feeds the flat tier multipliers, so this moves every tier that has no
    explicit sheet price. Does NOT write until the user says yes.

    Args:
        product: wine name or alias.
        price: new MSRP per bottle in DOLLARS.
        vintage: 4-digit year, or 0 if unspecified.
    """
    cents = _amount_to_cents(price)
    if cents is None or cents <= 0:
        return "That MSRP doesn't look right — give me a positive dollar amount like 80 or 80.00."
    res = _resolve_product(product, vintage or None)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_product_msg(res["ambiguous"])
    p = res["product"]
    label = f"{p['name']} {p.get('vintage') or ''} ({p.get('size', '750ml')})".strip()
    summary = (
        f"Set *MSRP* on {label} from {_money(p.get('msrp_bottle_cents'))} to *{_money(cents)}*?\n"
        f"This shifts tiers priced off the multiplier. Writes to Supabase + JSON. Reply *yes* to confirm."
    )
    return _stage("set_msrp", {
        "name": p["name"], "vintage": p.get("vintage"), "size": p.get("size", "750ml"),
        "cents": cents,
    }, summary)


def stage_set_tier(tier: str, discount_percent: float = -1, msrp_multiplier: float = -1) -> str:
    """Stage changing a whole pricing tier's discount % and/or MSRP multiplier, then
    ask to confirm. This affects EVERY product priced off that tier. Pass -1 for a
    field to leave it unchanged. Does NOT write until the user says yes.

    Use for "make Corporate 25% off" or "set Wholesale multiplier to 0.65".

    Args:
        tier: tier name (e.g. "Corporate").
        discount_percent: new discount percent (0-100), or -1 to leave as is.
        msrp_multiplier: new multiplier (0-1), or -1 to leave as is.
    """
    res = _resolve_tier(tier)
    if res.get("error"):
        return res["error"]
    t = res["tier"]
    fields: dict[str, Any] = {}
    dp = _to_float(discount_percent)
    mm = _to_float(msrp_multiplier)
    if dp is not None and dp >= 0:
        if dp > 100:
            return "Discount percent can't exceed 100."
        fields["discount_percent"] = dp
    if mm is not None and mm >= 0:
        if not (0 < mm <= 2):
            return "MSRP multiplier should be between 0 and 2 (e.g. 0.7 for 30% off)."
        fields["msrp_multiplier"] = mm
    if not fields:
        return "Tell me the new discount % and/or MSRP multiplier to set (pass -1 to leave one unchanged)."
    parts = []
    if "discount_percent" in fields:
        parts.append(f"discount {t.get('discount_percent')}% → *{fields['discount_percent']}%*")
    if "msrp_multiplier" in fields:
        parts.append(f"multiplier {t.get('msrp_multiplier')} → *{fields['msrp_multiplier']}*")
    summary = (
        f"Change tier *{t['name']}*: {', '.join(parts)}?\n"
        f"Affects every product priced off this tier. Writes to Supabase + JSON. Reply *yes* to confirm."
    )
    return _stage("set_tier", {"name": t["name"], "fields": fields}, summary)


def stage_set_availability(product: str, tier: str, available: bool, vintage: int = 0) -> str:
    """Stage making a wine available / unavailable for a tier, then ask to confirm.
    This edits the product's tier_unavailable list. Does NOT write until yes.

    Use for "make the 2023 Viognier unavailable for Wholesale" (available=false) or
    "open up the Cab for Wholesale again" (available=true).

    Args:
        product: wine name or alias.
        tier: tier name.
        available: true to make it sellable at the tier, false to block it.
        vintage: 4-digit year, or 0 if unspecified.
    """
    tres = _resolve_tier(tier)
    if tres.get("error"):
        return tres["error"]
    tier_name = tres["tier"]["name"]
    res = _resolve_product(product, vintage or None)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_product_msg(res["ambiguous"])
    p = res["product"]
    blocked = list(p.get("tier_unavailable") or [])
    is_blocked = tier_name in blocked
    if available and not is_blocked:
        return f"{p['name']} {p.get('vintage') or ''} is already available for {tier_name}."
    if not available and is_blocked:
        return f"{p['name']} {p.get('vintage') or ''} is already unavailable for {tier_name}."
    label = f"{p['name']} {p.get('vintage') or ''} ({p.get('size', '750ml')})".strip()
    verb = "available for" if available else "*unavailable* for"
    summary = (
        f"Make {label} {verb} *{tier_name}*?\n"
        f"Writes to Supabase + JSON. Reply *yes* to confirm."
    )
    return _stage("set_availability", {
        "name": p["name"], "vintage": p.get("vintage"), "size": p.get("size", "750ml"),
        "tier_name": tier_name, "available": bool(available),
    }, summary)


# ── WRITE tool: create / send a Square invoice (confirm-first) ─────────────────

def stage_invoice(customer_name: str, customer_email: str, tier: str, items_json: str,
                  payment_schedule: str = "NET_30", shipping_fee: float = -1,
                  send: bool = False) -> str:
    """Stage creating a Square invoice for an order, then ask the user to confirm.
    Prices the order first and shows the total. Nothing is created in Square until
    the user says yes. Set send=true only when staff clearly want it SENT to the
    customer (publish); otherwise it's saved as a draft.

    Use for "invoice Oak Barrel for 3 cases of Cabernet at wholesale" (send=false)
    or "...and send it" (send=true). For a PDF/forwarded order, read the digested
    text, pull out the customer + items, then call this.

    Args:
        customer_name: who the invoice is for.
        customer_email: their email (required to create the Square customer).
        tier: pricing tier name.
        items_json: JSON array of items — same shape as price_order's items_json.
        payment_schedule: UPON_RECEIPT | NET_7 | NET_14 | NET_30 (default NET_30).
        shipping_fee: shipping charge in dollars; use 0 for free/waived shipping,
            or -1 when unknown so the tool asks the user.
        send: true to PUBLISH (send to customer), false to keep a draft.
    """
    quote = _quote(tier, items_json)
    if quote.get("error"):
        return quote["error"]
    if quote.get("blocks"):
        return "Can't price this yet — " + "; ".join(quote["blocks"])
    if quote.get("needs_price"):
        labels = ", ".join(n["label"] for n in quote["needs_price"])
        return f"I need a price for: {labels}. Tell me the per-bottle price and I'll re-quote."
    if not (customer_email or "").strip():
        return "I need the customer's email to create the invoice. What is it?"
    shipping_cents = _shipping_to_cents(shipping_fee)
    if shipping_cents is None:
        return "Shipping for this invoice — is it free/waived, or what custom amount should I add (for example $30)?"
    sched = _norm_schedule(payment_schedule)
    verb = "create AND SEND" if send else "create a draft of"
    total_cents = (quote.get("total_cents") or 0) + shipping_cents
    shipping_line = "Shipping: free/waived" if shipping_cents == 0 else f"Shipping: {_money(shipping_cents)}"
    summary = (
        f"Ready to {verb} this invoice:\n"
        f"*{customer_name}* — {tier}, {sched}\n"
        f"{quote['summary']}\n"
        f"{shipping_line}\n"
        f"Total: *{_money(total_cents)}*\n\n"
        f"Reply *yes* to confirm" + (" and send it." if send else " (draft only).")
    )
    # A stable token so an accidental double-confirm/retry dedupes at Square rather
    # than creating a second invoice.
    return _stage("invoice", {
        "customer_name": customer_name, "customer_email": customer_email.strip(),
        "tier": tier, "items_json": items_json,
        "payment_schedule": sched, "shipping_cents": shipping_cents,
        "send": bool(send), "idem": uuid.uuid4().hex,
    }, summary)


# ── WRITE tool: send an EXISTING draft invoice (confirm-first) ────────────────

def stage_send_invoice(customer_name: str = "", invoice_number: str = "") -> str:
    """Send an ALREADY-DRAFTED invoice to the customer (publishes the Square
    draft). Use when staff ask to send an invoice that exists as a draft —
    "send Christina's invoice", "publish the Oak Barrel draft", "send it" after
    a draft was created earlier. Confirm-first: stages, then the user replies yes.

    The draft is found in the durable invoice log (Supabase) and verified
    against Square, so this works even for drafts created in an earlier
    conversation or before a redeploy.

    Args:
        customer_name: whose draft to send (name as staff say it; fuzzy ok).
        invoice_number: exact Square invoice id if staff gave one (optional).
    """
    from db.repository import get_recent_invoice_for_customer
    from services import square_service

    invoice_id = (invoice_number or "").strip()
    if not invoice_id:
        if not (customer_name or "").strip():
            return "Whose invoice should I send? Give me the customer name (or an invoice number)."
        try:
            row = get_recent_invoice_for_customer(customer_name=customer_name.strip())
        except Exception as exc:  # pragma: no cover - defensive
            return f"Couldn't look up recent invoices — {exc}"
        if not row or not row.get("square_invoice_id"):
            return (f"I couldn't find a drafted invoice for {customer_name}. "
                    "Check recent_invoices, or give me the invoice number.")
        invoice_id = row["square_invoice_id"]

    inv = square_service.get_invoice(invoice_id)
    if inv.get("error"):
        return f"Couldn't fetch that invoice from Square — {inv['error']}"
    status = (inv.get("status") or "").upper()
    label = inv.get("invoice_number") or invoice_id
    if status != "DRAFT":
        url = inv.get("public_url")
        if status in ("UNPAID", "SCHEDULED", "PARTIALLY_PAID", "PAID"):
            return (f"Invoice {label} was already sent (status {status})."
                    + (f"\nPayment link: {url}" if url else ""))
        return f"Invoice {label} is {status or 'in an unknown state'} — I can only send drafts."

    total = _money(inv.get("total_money_cents"))
    summary = (
        f"Ready to SEND invoice *{label}*"
        + (f" for *{customer_name}*" if (customer_name or "").strip() else "")
        + f" — total {total}. This publishes it to the customer.\n\n"
        "Reply *yes* to send it."
    )
    return _stage("send_existing", {
        "invoice_id": invoice_id, "customer_name": (customer_name or "").strip(),
        "idem": uuid.uuid4().hex,
    }, summary)


# ── pricing quote (shared by price_order + stage_invoice) ─────────────────────

def _quote(tier: str, items_json: str) -> dict:
    from services.product_service import calculate_invoice_prices

    try:
        items = json.loads(items_json) if isinstance(items_json, str) else (items_json or [])
        if not isinstance(items, list):
            raise ValueError("items must be a list")
    except Exception as exc:
        return {"error": f"I couldn't read the item list ({exc}). Give me the order again?"}
    if not items:
        return {"error": "No items to price — what wines and quantities?"}
    try:
        priced = calculate_invoice_prices((tier or "").strip(), items)
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"I couldn't price that ({exc}). Check the wine names/quantities?"}
    lines = []
    for li in priced.get("line_items", []):
        qty, ut = li["quantity"], li.get("unit_type", "bottle")
        lines.append(
            f"• {qty} {ut}{'s' if qty != 1 else ''} {li['product_name']} "
            f"{li.get('vintage') or ''} @ {_money(li['final_unit_price_cents'])}/btl "
            f"= {_money(li['line_total_cents'])}".replace("  ", " ")
        )
    return {
        "summary": "\n".join(lines) if lines else "(no priced lines)",
        "line_items": priced.get("line_items", []),
        "subtotal": _money(priced.get("subtotal_cents")),
        "discount": _money(priced.get("discount_cents")),
        "total": _money(priced.get("total_before_tax_cents")),
        "total_cents": priced.get("total_before_tax_cents"),
        "warnings": priced.get("warnings", []),
        "blocks": priced.get("blocks", []),
        "needs_price": priced.get("needs_price", []),
        "tier": tier,
    }


# ── execution: pricing writes (Supabase first, then JSON; report drift) ────────

def _sb_client():
    from db.repository import _get_client
    return _get_client()


def _update_product_supabase(name: str, vintage, size: str, fields: dict) -> tuple[bool, str]:
    """Update one products row in Supabase. Matches by name (ci) + vintage + size."""
    try:
        c = _sb_client()
        rows = (c.table("products").select("*").ilike("name", name).execute().data) or []
        match = [
            r for r in rows
            if (r.get("vintage") == vintage)
            and (str(r.get("size", "750ml")).lower() == str(size).lower())
        ]
        if not match:
            return False, "no matching Supabase row"
        if len(match) > 1:
            return False, "multiple Supabase rows matched"
        c.table("products").update(fields).eq("id", match[0]["id"]).execute()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _update_product_json(name: str, vintage, size: str, mutate) -> tuple[bool, str]:
    """Apply `mutate(entry)` to the matching product_catalog.json entry and save."""
    from app.config import DATA_DIR

    path = DATA_DIR / "product_catalog.json"
    try:
        with open(path) as f:
            catalog = json.load(f)
        hit = None
        for entry in catalog:
            if (_ascii(entry.get("name")) == _ascii(name)
                    and entry.get("vintage") == vintage
                    and str(entry.get("size", "750ml")).lower() == str(size).lower()):
                hit = entry
                break
        if hit is None:
            return False, "no matching JSON entry"
        mutate(hit)
        with open(path, "w") as f:
            json.dump(catalog, f, indent=2)
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _apply_product(name, vintage, size, sb_fields: dict, mutate, what: str) -> str:
    """Write to Supabase first; only touch JSON if that succeeded (no drift)."""
    sb_ok, sb_msg = _update_product_supabase(name, vintage, size, sb_fields)
    if not sb_ok:
        return (f"Didn't change {what} — Supabase write failed ({sb_msg}), so I left the JSON "
                f"alone to avoid drift. Nothing changed.")
    json_ok, json_msg = _update_product_json(name, vintage, size, mutate)
    label = f"{name} {vintage or ''}".strip()
    if not json_ok:
        return (f"Updated {what} for {label} in Supabase ✅ — but the catalog JSON didn't update "
                f"({json_msg}); they may drift, so sync the JSON when you can.")
    return f"Done ✅ — updated {what} for {label} in Supabase + catalog JSON."


def _exec_set_channel_price(name, vintage, size, channel, cents) -> str:
    def mutate(entry):
        entry.setdefault("tier_prices", {})[channel] = cents

    # Supabase carries tier_prices as a jsonb column; send the merged dict.
    from services.product_service import find_product
    p = find_product(name, vintage, size) or {}
    merged = dict(p.get("tier_prices") or {})
    merged[channel] = cents
    return _apply_product(name, vintage, size, {"tier_prices": merged}, mutate, f"{channel} price")


def _exec_set_msrp(name, vintage, size, cents) -> str:
    def mutate(entry):
        entry["msrp_bottle_cents"] = cents

    return _apply_product(name, vintage, size, {"msrp_bottle_cents": cents}, mutate, "MSRP")


def _exec_set_availability(name, vintage, size, tier_name, available) -> str:
    from services.product_service import find_product

    p = find_product(name, vintage, size) or {}
    blocked = list(p.get("tier_unavailable") or [])
    if available:
        new_blocked = [t for t in blocked if t != tier_name]
    else:
        new_blocked = blocked + ([tier_name] if tier_name not in blocked else [])

    def mutate(entry):
        entry["tier_unavailable"] = new_blocked

    verb = "availability"
    return _apply_product(name, vintage, size, {"tier_unavailable": new_blocked}, mutate, verb)


def _exec_set_tier(name, fields) -> str:
    """Update a pricing_tiers row in Supabase first, then pricing_tiers.json."""
    from app.config import DATA_DIR

    # Supabase
    try:
        c = _sb_client()
        rows = (c.table("pricing_tiers").select("id,name").ilike("name", name).execute().data) or []
        if not rows:
            return f"Didn't change tier {name} — no matching Supabase row. Nothing changed."
        c.table("pricing_tiers").update(fields).eq("id", rows[0]["id"]).execute()
    except Exception as exc:
        return f"Didn't change tier {name} — Supabase write failed ({exc}). Left JSON alone."

    # JSON
    path = DATA_DIR / "pricing_tiers.json"
    try:
        with open(path) as f:
            tiers = json.load(f)
        for t in tiers:
            if _ascii(t.get("name")) == _ascii(name):
                t.update(fields)
                break
        with open(path, "w") as f:
            json.dump(tiers, f, indent=2)
    except Exception as exc:
        return (f"Updated tier {name} in Supabase ✅ — but pricing_tiers.json didn't update "
                f"({exc}); sync it when you can.")
    changed = ", ".join(f"{k}={v}" for k, v in fields.items())
    return f"Done ✅ — updated tier {name} ({changed}) in Supabase + JSON."


# ── execution: create / send Square invoice ───────────────────────────────────

def _exec_invoice(customer_name, customer_email, tier, items_json, payment_schedule, shipping_cents=0, send=False, idem="") -> str:
    from services import square_service

    quote = _quote(tier, items_json)
    if quote.get("error"):
        return quote["error"]
    line_items = quote["line_items"]
    # Deterministic idempotency base so a retried confirm dedupes at Square instead
    # of creating a duplicate customer/order/invoice.
    ik = idem or uuid.uuid4().hex

    cust = square_service.get_or_create_square_customer(
        customer_email, customer_name, idempotency_key=f"{ik}-cust"[:45])
    if cust.get("error"):
        return f"Couldn't set up the Square customer — {cust['error']}"

    order = square_service.create_order(
        customer_name, line_items, idempotency_key=f"{ik}-order"[:45],
        shipping_cents=shipping_cents or 0)
    if order.get("error"):
        return f"Couldn't create the Square order — {order['error']}"

    draft = square_service.create_invoice_draft(
        order["order_id"], cust["customer_id"],
        message=f"{tier} pricing.", payment_schedule=payment_schedule,
        idempotency_key=f"{ik}-draft"[:45],
    )
    if draft.get("error"):
        return f"Couldn't create the invoice draft — {draft['error']}"

    _log_invoice_best_effort(customer_name, customer_email, tier, line_items, quote, draft, send, shipping_cents)

    if not send:
        total = _money((quote.get("total_cents") or 0) + (shipping_cents or 0))
        return (f"Draft created ✅ — {customer_name}, {total} ({tier}, {payment_schedule}). "
                f"Invoice {draft.get('invoice_number') or draft['invoice_id']}. "
                f"Say *send it* when you want it published to the customer.")

    pub = square_service.publish_invoice(
        draft["invoice_id"], draft.get("invoice_version", 0), idempotency_key=f"{ik}-pub"[:45])
    if pub.get("error"):
        return (f"Draft created, but publishing failed — {pub['error']}. "
                f"The draft is saved (invoice {draft.get('invoice_number') or draft['invoice_id']}).")
    url = pub.get("public_url")
    total = _money((quote.get("total_cents") or 0) + (shipping_cents or 0))
    return (f"Sent ✅ — {customer_name}, {total} ({tier}, {payment_schedule})."
            + (f"\nPayment link: {url}" if url else ""))


def _exec_send_existing(invoice_id, customer_name="", idem="") -> str:
    """Publish an existing Square draft after the user's yes. Re-fetches the
    invoice for its current version (Square rejects stale-version publishes)."""
    from services import square_service

    inv = square_service.get_invoice(invoice_id)
    if inv.get("error"):
        return f"Couldn't fetch the draft — {inv['error']}"
    label = inv.get("invoice_number") or invoice_id
    status = (inv.get("status") or "").upper()
    if status != "DRAFT":
        url = inv.get("public_url")
        return (f"Invoice {label} is {status or 'in an unknown state'} now — nothing sent."
                + (f"\nPayment link: {url}" if url else ""))

    pub = square_service.publish_invoice(
        invoice_id, invoice_version=inv.get("version") or 0,
        idempotency_key=f"{idem}-pub"[:45])
    if pub.get("error"):
        return f"Publishing failed — {pub['error']}. The draft is untouched."
    url = pub.get("public_url")
    who = f" to {customer_name}" if customer_name else ""
    total = _money(inv.get("total_money_cents"))
    return (f"Sent ✅ — invoice {label}{who}, {total}."
            + (f"\nPayment link: {url}" if url else ""))


def _log_invoice_best_effort(customer_name, customer_email, tier, line_items, quote, draft, send, shipping_cents=0) -> None:
    try:
        from db.models import InvoiceLog
        from db.repository import log_invoice

        log_invoice(InvoiceLog(
            thread_id=f"chat_{int(time.time())}",
            sender_id=_user(),
            customer_name=customer_name,
            customer_email=customer_email,
            tier_name=tier,
            line_items=line_items,
            subtotal_cents=None,
            total_before_tax_cents=(quote.get("total_cents") or 0) + (shipping_cents or 0),
            shipping_cents=shipping_cents or 0,
            payment_schedule=draft.get("payment_schedule"),
            payment_methods=draft.get("accepted_payment_methods") or [],
            approval="approved" if send else None,
            square_invoice_id=draft.get("invoice_id"),
        ))
    except Exception as exc:  # pragma: no cover - observability only
        log.info("[inv:chat-actions] invoice log skipped: %s", exc)


# Tools exposed to the ADK agent (docstring order = how staff think about it).
READ_TOOLS = [find_products, get_pricing, list_tiers, recent_invoices, price_order]
WRITE_TOOLS = [
    stage_set_channel_price,
    stage_set_msrp,
    stage_set_tier,
    stage_set_availability,
    stage_invoice,
    stage_send_invoice,
    confirm_pending_action,
    cancel_pending_action,
]
