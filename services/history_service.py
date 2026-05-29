"""Customer invoice and order history lookup.

Used by the agent when creating a new invoice to provide context:
- What has this customer ordered before?
- What tier are they?
- Any outstanding invoices?
"""
from typing import Optional

from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY

_client = None


def _get_client():
    global _client
    if _client:
        return _client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client
    _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


def get_customer_history(
    square_customer_id: Optional[str] = None,
    email: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Return recent invoices and orders for a customer.

    Looks up by square_customer_id first, then by email.
    Returns a summary dict the agent can include in context.
    """
    sb = _get_client()
    if not sb:
        return {"status": "unavailable", "invoices": [], "orders": []}

    # Resolve customer DB id
    cust_id = None
    sq_cust_id = square_customer_id

    if not sq_cust_id and email:
        result = sb.table("customers").select("id, square_customer_id, tier_name") \
            .ilike("email", email).limit(1).execute()
        rows = result.data or []
        if rows:
            cust_id = rows[0]["id"]
            sq_cust_id = rows[0].get("square_customer_id")

    if sq_cust_id and not cust_id:
        result = sb.table("customers").select("id").eq("square_customer_id", sq_cust_id).limit(1).execute()
        rows = result.data or []
        if rows:
            cust_id = rows[0]["id"]

    if not cust_id and not sq_cust_id:
        return {"status": "not_found", "invoices": [], "orders": []}

    # Fetch recent invoices
    inv_query = sb.table("square_invoices") \
        .select("invoice_number, title, status, total_money_cents, due_date, invoice_created_at") \
        .order("invoice_created_at", desc=True) \
        .limit(limit)

    if cust_id:
        inv_query = inv_query.eq("customer_id", cust_id)
    elif sq_cust_id:
        inv_query = inv_query.eq("square_customer_id", sq_cust_id)

    invoices = (inv_query.execute().data or [])

    # Fetch recent orders
    ord_query = sb.table("square_orders") \
        .select("square_order_id, state, total_money_cents, line_items, order_created_at") \
        .order("order_created_at", desc=True) \
        .limit(limit)

    if cust_id:
        ord_query = ord_query.eq("customer_id", cust_id)
    elif sq_cust_id:
        ord_query = ord_query.eq("square_customer_id", sq_cust_id)

    orders = (ord_query.execute().data or [])

    # Format for agent consumption
    invoice_summaries = [
        {
            "invoice_number": inv.get("invoice_number"),
            "status": inv.get("status"),
            "total": f"${(inv.get('total_money_cents') or 0) / 100:.2f}",
            "due_date": inv.get("due_date"),
            "date": (inv.get("invoice_created_at") or "")[:10],
        }
        for inv in invoices
    ]

    order_summaries = [
        {
            "order_id": o.get("square_order_id"),
            "state": o.get("state"),
            "total": f"${(o.get('total_money_cents') or 0) / 100:.2f}",
            "items": [li.get("name") for li in (o.get("line_items") or [])],
            "date": (o.get("order_created_at") or "")[:10],
        }
        for o in orders
    ]

    return {
        "status": "found",
        "square_customer_id": sq_cust_id,
        "invoice_count": len(invoice_summaries),
        "order_count": len(order_summaries),
        "invoices": invoice_summaries,
        "orders": order_summaries,
    }


def get_outstanding_invoices(square_customer_id: str) -> list[dict]:
    """Return unpaid/scheduled invoices for a customer."""
    sb = _get_client()
    if not sb:
        return []
    result = sb.table("square_invoices") \
        .select("invoice_number, status, total_money_cents, due_date") \
        .eq("square_customer_id", square_customer_id) \
        .in_("status", ["UNPAID", "SCHEDULED", "PARTIALLY_PAID"]) \
        .order("due_date") \
        .execute()
    return result.data or []


def search_invoices_by_customer_name(name: str, limit: int = 5) -> list[dict]:
    """Search invoice history by customer name (agent use: 'what did X order before?')."""
    sb = _get_client()
    if not sb:
        return []

    # Find matching customers first
    result = sb.table("customers") \
        .select("id, full_name, company, square_customer_id") \
        .ilike("full_name", f"%{name}%") \
        .limit(5) \
        .execute()

    customers = result.data or []
    if not customers:
        # Try company name
        result = sb.table("customers") \
            .select("id, full_name, company, square_customer_id") \
            .ilike("company", f"%{name}%") \
            .limit(5) \
            .execute()
        customers = result.data or []

    if not customers:
        return []

    all_invoices = []
    for c in customers[:2]:  # limit to top 2 matches
        history = get_customer_history(square_customer_id=c.get("square_customer_id"), limit=limit)
        for inv in history.get("invoices", []):
            inv["customer_name"] = c.get("full_name") or c.get("company")
            all_invoices.append(inv)

    return all_invoices
