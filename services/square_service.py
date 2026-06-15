"""Square API service layer (Square SDK v44+).

All Square API calls go here. The agent graph calls these functions,
never the Square SDK directly.
"""
import hashlib
import uuid
from datetime import date, timedelta
from typing import Optional


def _ikey(case_id: str, action: str) -> str:
    """Deterministic Square idempotency key.

    Same case_id + action always produces the same key, so retries after a
    timeout/network error are safe — Square deduplicates on this key.
    Format: winefornia:{case_id}:{action}:v1 → SHA-256 hex (64 chars, within Square's 45-char limit truncated to 45).
    Falls back to a random UUID only when case_id is empty (shouldn't happen in prod).
    """
    if not case_id:
        return str(uuid.uuid4())
    raw = f"winefornia:{case_id}:{action}:v1"
    return hashlib.sha256(raw.encode()).hexdigest()[:45]

from app.config import (
    SQUARE_PROD_ACCESS_TOKEN,
    SQUARE_PROD_LOCATION_ID,
    SQUARE_ACCESS_TOKEN,
    SQUARE_LOCATION_ID,
    SQUARE_ENVIRONMENT,
)

_client = None


def _use_sandbox() -> bool:
    """Use Square sandbox ONLY when explicitly requested AND a sandbox token is
    present. Production-safe: prod (which has no sandbox token) always stays on
    production even if SQUARE_ENVIRONMENT is unset/misread."""
    return (SQUARE_ENVIRONMENT or "").strip().lower() == "sandbox" and bool(SQUARE_ACCESS_TOKEN)


def _active_location() -> str:
    return (SQUARE_LOCATION_ID if _use_sandbox() else SQUARE_PROD_LOCATION_ID) or ""


def _get_client():
    global _client
    if _client:
        return _client
    try:
        from square import Square
        from square.environment import SquareEnvironment
        if _use_sandbox():
            if not SQUARE_ACCESS_TOKEN:
                return None
            _client = Square(token=SQUARE_ACCESS_TOKEN, environment=SquareEnvironment.SANDBOX)
        else:
            if not SQUARE_PROD_ACCESS_TOKEN:
                return None
            _client = Square(token=SQUARE_PROD_ACCESS_TOKEN, environment=SquareEnvironment.PRODUCTION)
        return _client
    except Exception:
        return None


def get_or_create_square_customer(email: str, full_name: str, idempotency_key: str = "") -> dict:
    """Look up a customer in Square by email, or create them if not found.

    Returns dict with customer_id and status ('found' or 'created'), or error key.
    """
    client = _get_client()
    if not client:
        return {"error": "Square not configured. Set SQUARE_PROD_ACCESS_TOKEN in .env"}
    try:
        response = client.customers.search(
            query={
                "filter": {
                    "email_address": {
                        "exact": email,
                    }
                }
            }
        )
        customers = response.customers or []
        if customers:
            return {"status": "found", "customer_id": customers[0].id, "email": email}

        parts = full_name.strip().split(" ", 1)
        given = parts[0]
        family = parts[1] if len(parts) > 1 else ""
        create_resp = client.customers.create(
            idempotency_key=idempotency_key or str(uuid.uuid4()),
            given_name=given,
            family_name=family,
            email_address=email,
        )
        return {"status": "created", "customer_id": create_resp.customer.id, "email": email}
    except Exception as e:
        return {"error": str(e)}


SCHEDULE_TO_DAYS = {
    "UPON_RECEIPT": 0,
    "NET_7": 7,
    "NET_14": 14,
    "NET_30": 30,
}


def create_order(customer_name: str, line_items: list[dict], location_id: Optional[str] = None, idempotency_key: str = "") -> dict:
    """Create a Square order (OPEN state) from priced line items.

    line_items: list of dicts with product_name, quantity, unit_type,
                final_unit_price_cents, bottles_per_case
    """
    client = _get_client()
    if not client:
        return {"error": "Square not configured. Set SQUARE_PROD_ACCESS_TOKEN in .env"}
    loc = location_id or _active_location()
    if not loc:
        return {"error": "SQUARE_LOCATION_ID not set. Add it to .env"}

    sq_line_items = []
    for item in line_items:
        qty = item["quantity"]
        unit_type = item.get("unit_type", "bottle")
        bottles_per_case = item.get("bottles_per_case", 12)

        if unit_type == "case":
            total_bottles = int(qty * bottles_per_case)
            display_name = f"{item['product_name']} ({int(qty)} case{'s' if qty > 1 else ''} / {total_bottles} bottles)"
        else:
            total_bottles = int(qty)
            display_name = f"{item['product_name']} ({int(qty)} bottle{'s' if qty > 1 else ''})"

        sq_line_items.append({
            "name": display_name,
            "quantity": str(total_bottles),
            "base_price_money": {
                "amount": item["final_unit_price_cents"],
                "currency": "USD",
            },
        })

    try:
        response = client.orders.create(
            order={
                "location_id": loc,
                "reference_id": f"winefornia-{uuid.uuid4().hex[:8]}",
                "line_items": sq_line_items,
            },
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        order = response.order
        return {
            "status": "created",
            "order_id": order.id,
            "total_money": {
                "amount": order.total_money.amount,
                "currency": order.total_money.currency,
            } if order.total_money else None,
        }
    except Exception as e:
        return {"error": str(e)}


def create_invoice_draft(
    order_id: str,
    customer_id: str,
    title: str = "INNOVATUS Wine Purchase",
    message: str = "",
    payment_schedule: str = "NET_30",
    accepted_payment_methods: Optional[list[str]] = None,
    location_id: Optional[str] = None,
    idempotency_key: str = "",
) -> dict:
    """Create a Square invoice draft linked to an order. Saved only, NOT sent.

    delivery_method is SHARE_MANUALLY — never emailed automatically.
    """
    client = _get_client()
    if not client:
        return {"error": "Square not configured. Set SQUARE_PROD_ACCESS_TOKEN in .env"}
    loc = location_id or _active_location()

    days = SCHEDULE_TO_DAYS.get(payment_schedule, 30)
    due_date = (date.today() + timedelta(days=days)).isoformat()

    if accepted_payment_methods is None:
        accepted_payment_methods = ["CARD", "BANK_ACCOUNT"]

    methods = {
        "card": "CARD" in accepted_payment_methods,
        "bank_account": "BANK_ACCOUNT" in accepted_payment_methods,
        "square_gift_card": False,
        "cash_app_pay": False,
    }

    try:
        response = client.invoices.create(
            invoice={
                "order_id": order_id,
                "location_id": loc,
                "title": title,
                **({"description": message} if message else {}),
                "primary_recipient": {
                    "customer_id": customer_id,
                },
                "payment_requests": [
                    {
                        "request_type": "BALANCE",
                        "due_date": due_date,
                        "automatic_payment_source": "NONE",
                    }
                ],
                "delivery_method": "SHARE_MANUALLY",
                "accepted_payment_methods": methods,
            },
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        invoice = response.invoice
        return {
            "status": "draft_created",
            "invoice_id": invoice.id,
            "invoice_version": getattr(invoice, "version", 0),
            "invoice_number": invoice.invoice_number,
            "payment_schedule": payment_schedule,
            "accepted_payment_methods": accepted_payment_methods,
            "note": (
                "Invoice saved as DRAFT. NOT emailed. "
                "Call publish_invoice to send it and get a working public URL."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def publish_invoice(invoice_id: str, invoice_version: int = 0, idempotency_key: str = "") -> dict:
    """Publish a Square invoice draft. REQUIRES explicit human approval first.

    This sends the invoice to the customer.
    """
    client = _get_client()
    if not client:
        return {"error": "Square not configured. Set SQUARE_PROD_ACCESS_TOKEN in .env"}
    try:
        response = client.invoices.publish(
            invoice_id=invoice_id,
            version=invoice_version,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        invoice = response.invoice
        return {
            "status": "published",
            "invoice_id": invoice.id,
            "public_url": getattr(invoice, "public_url", None),
        }
    except Exception as e:
        return {"error": str(e)}


def get_customer_by_name(name: str) -> Optional[dict]:
    """Search Square customers by name. Returns first match or None."""
    # Use lookup_customer from customer_service for JSON-backed lookup;
    # this Square search is available as a fallback.
    client = _get_client()
    if not client:
        return None
    try:
        response = client.customers.search(
            query={
                "filter": {
                    "reference_id": {"exact": name},
                }
            }
        )
        customers = response.customers or []
        if customers:
            c = customers[0]
            return {"id": c.id, "name": f"{c.given_name} {c.family_name}".strip()}
        return None
    except Exception:
        return None
