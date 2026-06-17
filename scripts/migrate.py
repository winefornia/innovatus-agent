"""One-time historical migration from Square + local JSON files → Supabase.

Migrates (in order):
  1. pricing_tiers     ← from app/data/pricing_tiers.json
  2. products          ← from app/data/product_catalog.json
  3. customers         ← from Square API (all customers) + tier from customers.json
  4. square_orders     ← from Square API (past 5 years)
  5. square_invoices   ← from Square API (past 5 years)

Usage:
    python scripts/migrate.py
    python scripts/migrate.py --skip-orders     # skip orders/invoices (faster)
    python scripts/migrate.py --entity customers # run only one entity
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import app.config  # noqa: F401 — triggers load_dotenv
from app.config import (
    DATA_DIR,
    SQUARE_PROD_ACCESS_TOKEN,
    SQUARE_PROD_LOCATION_ID,
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
)
from supabase import create_client

FIVE_YEARS_AGO = (datetime.now(timezone.utc) - timedelta(days=5 * 365)).isoformat()


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def _square():
    from square import Square
    from square.environment import SquareEnvironment
    return Square(token=SQUARE_PROD_ACCESS_TOKEN, environment=SquareEnvironment.PRODUCTION)


def _supabase():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ---------------------------------------------------------------------------
# 1. Pricing tiers
# ---------------------------------------------------------------------------

def migrate_pricing_tiers(sb):
    print("\n[1/5] Migrating pricing tiers...")
    tiers = json.loads((DATA_DIR / "pricing_tiers.json").read_text())
    rows = [
        {
            "tier_number": t.get("tier_number"),
            "name": t["name"],
            "channel": t.get("channel"),
            "msrp_multiplier": t["msrp_multiplier"],
            "discount_percent": t["discount_percent"],
            "requires_human_confirmation": t.get("requires_human_confirmation", False),
            "notes": t.get("notes"),
        }
        for t in tiers
    ]
    sb.table("pricing_tiers").upsert(rows, on_conflict="name").execute()
    print(f"    {len(rows)} tiers upserted.")


# ---------------------------------------------------------------------------
# 2. Products
# ---------------------------------------------------------------------------

def migrate_products(sb):
    print("\n[2/5] Migrating product catalog...")
    catalog = json.loads((DATA_DIR / "product_catalog.json").read_text())
    rows = []
    for p in catalog:
        rows.append({
            "sku": p.get("sku") or f"{p['name'].lower().replace(' ', '-')}-{p.get('vintage', 'nv')}",
            "name": p["name"],
            "vintage": p.get("vintage"),
            "size": p.get("size", "750ml"),
            "bottles_per_case": p.get("bottles_per_case", 12),
            "msrp_bottle_cents": p.get("msrp_bottle_cents"),
            "tier_prices": p.get("tier_prices", {}),
            "variable_pricing": p.get("variable_pricing", False),
            "tier_unavailable": p.get("tier_unavailable", []),
        })
    # Deduplicate by SKU (product_catalog.json may have duplicate entries)
    seen = {}
    for r in rows:
        seen[r["sku"]] = r
    rows = list(seen.values())
    sb.table("products").upsert(rows, on_conflict="sku").execute()
    print(f"    {len(rows)} products upserted.")


# ---------------------------------------------------------------------------
# 3. Customers
# ---------------------------------------------------------------------------

def migrate_customers(sb, sq):
    print("\n[3/5] Migrating customers from Square...")

    # Build tier lookup from local customers.json (has tier assignments)
    local_customers = json.loads((DATA_DIR / "customers.json").read_text())
    known_tiers = json.loads((DATA_DIR / "pricing_tiers.json").read_text())
    valid_tier_names = {t["name"] for t in known_tiers}

    def _clean_tier(tier):
        return tier if tier in valid_tier_names else None

    tier_by_email = {
        c["email"].lower().strip(): _clean_tier(c.get("tier_name"))
        for c in local_customers
        if c.get("email")
    }
    tier_by_name = {
        (c.get("full_name") or "").lower().strip(): _clean_tier(c.get("tier_name"))
        for c in local_customers
        if c.get("full_name")
    }

    rows = []
    count = 0

    # Square SDK v44+ returns a SyncPager — iterate directly
    for c in sq.customers.list():
        email = getattr(c, "email_address", None) or ""
        full_name = " ".join(filter(None, [
            getattr(c, "given_name", None),
            getattr(c, "family_name", None),
        ])).strip() or getattr(c, "company_name", None) or ""

        tier = _clean_tier(
            tier_by_email.get(email.lower())
            or tier_by_name.get(full_name.lower())
        )

        created = getattr(c, "created_at", None)
        rows.append({
            "square_customer_id": c.id,
            "full_name": full_name or None,
            "company": getattr(c, "company_name", None),
            "email": email or None,
            "phone": getattr(c, "phone_number", None),
            "tier_name": tier,
            "square_created_at": created,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })
        count += 1
        if count % 100 == 0:
            print(f"    {count} customers fetched...", end="\r")

    # Deduplicate by square_customer_id
    seen = set()
    rows = [r for r in rows if r["square_customer_id"] not in seen and not seen.add(r["square_customer_id"])]

    # Upsert in batches of 500
    _batch_upsert(sb, "customers", rows, "square_customer_id", batch_size=500)
    print(f"\n    {len(rows)} customers upserted.")


# ---------------------------------------------------------------------------
# 4. Orders
# ---------------------------------------------------------------------------

def migrate_orders(sb, sq):
    print("\n[4/5] Migrating Square orders (past 5 years)...")

    # Build square_customer_id → DB uuid map for FK
    cust_map = _build_customer_map(sb)

    rows = []
    cursor = None
    page = 0

    while True:
        body = {
            "location_ids": [SQUARE_PROD_LOCATION_ID],
            "query": {
                "filter": {
                    "date_time_filter": {
                        "created_at": {"start_at": FIVE_YEARS_AGO}
                    }
                },
                "sort": {"sort_field": "CREATED_AT", "sort_order": "ASC"},
            },
            "limit": 500,
        }
        if cursor:
            body["cursor"] = cursor

        resp = sq.orders.search(**body)
        orders = getattr(resp, "orders", None) or []
        if not orders:
            break

        for o in orders:
            cid = getattr(o, "customer_id", None)
            line_items = [
                {
                    "name": li.name,
                    "quantity": li.quantity,
                    "base_price_cents": li.base_price_money.amount if li.base_price_money else None,
                    "total_money_cents": li.total_money.amount if li.total_money else None,
                }
                for li in (o.line_items or [])
            ]
            rows.append({
                "square_order_id": o.id,
                "square_customer_id": cid,
                "customer_id": cust_map.get(cid),
                "location_id": o.location_id,
                "state": o.state,
                "total_money_cents": o.total_money.amount if o.total_money else None,
                "currency": o.total_money.currency if o.total_money else "USD",
                "line_items": line_items,
                "order_created_at": o.created_at,
                "order_updated_at": o.updated_at,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            })

        page += 1
        cursor = getattr(resp, "cursor", None)
        print(f"    page {page}: {len(orders)} orders | running total: {len(rows)}", end="\r")
        if not cursor:
            break
        time.sleep(0.3)

    _batch_upsert(sb, "square_orders", rows, "square_order_id", batch_size=500)
    print(f"\n    {len(rows)} orders upserted.")


# ---------------------------------------------------------------------------
# 5. Invoices
# ---------------------------------------------------------------------------

def migrate_invoices(sb, sq):
    print("\n[5/5] Migrating Square invoices (past 5 years)...")
    cust_map = _build_customer_map(sb)

    rows = []
    cursor = None
    page = 0

    while True:
        kwargs = {"location_id": SQUARE_PROD_LOCATION_ID, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor

        resp = sq.invoices.list(**kwargs)
        invoices = getattr(resp, "invoices", None) or []
        if not invoices:
            break

        cutoff = datetime.fromisoformat(FIVE_YEARS_AGO)

        for inv in invoices:
            created = getattr(inv, "created_at", None)
            # Filter client-side to 5yr window
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if dt < cutoff:
                        continue
                except Exception:
                    pass

            cid = None
            if inv.primary_recipient:
                cid = getattr(inv.primary_recipient, "customer_id", None)

            payment_req = (inv.payment_requests or [None])[0]
            due_date = getattr(payment_req, "due_date", None) if payment_req else None
            total_cents = None
            if payment_req and getattr(payment_req, "total_completed_amount_money", None):
                total_cents = payment_req.total_completed_amount_money.amount
            elif inv.payment_requests:
                for pr in inv.payment_requests:
                    if getattr(pr, "computed_amount_money", None):
                        total_cents = pr.computed_amount_money.amount
                        break

            rows.append({
                "square_invoice_id": inv.id,
                "square_order_id": getattr(inv, "order_id", None),
                "square_customer_id": cid,
                "customer_id": cust_map.get(cid) if cid else None,
                "invoice_number": getattr(inv, "invoice_number", None),
                "title": getattr(inv, "title", None),
                "status": getattr(inv, "status", None),
                "delivery_method": getattr(inv, "delivery_method", None),
                "total_money_cents": total_cents,
                "due_date": due_date,
                "invoice_created_at": created,
                "invoice_updated_at": getattr(inv, "updated_at", None),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            })

        page += 1
        cursor = getattr(resp, "cursor", None)
        print(f"    page {page}: {len(invoices)} invoices | running total: {len(rows)}", end="\r")
        if not cursor:
            break
        time.sleep(0.3)

    _batch_upsert(sb, "square_invoices", rows, "square_invoice_id", batch_size=500)
    print(f"\n    {len(rows)} invoices upserted.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_customer_map(sb) -> dict:
    """Return {square_customer_id: uuid} for all customers in Supabase."""
    result = sb.table("customers").select("id, square_customer_id").execute()
    return {r["square_customer_id"]: r["id"] for r in (result.data or []) if r["square_customer_id"]}


def _batch_upsert(sb, table: str, rows: list, conflict_col: str, batch_size=500):
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        sb.table(table).upsert(batch, on_conflict=conflict_col).execute()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity", choices=["tiers", "products", "customers", "orders", "invoices"],
                        help="Run only one entity")
    parser.add_argument("--skip-orders", action="store_true",
                        help="Skip orders and invoices (faster for customer/product sync)")
    args = parser.parse_args()

    if not SQUARE_PROD_ACCESS_TOKEN:
        sys.exit("ERROR: SQUARE_PROD_ACCESS_TOKEN not set in .env")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        sys.exit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not set in .env")

    sb = _supabase()
    sq = _square()

    entity = args.entity
    skip_orders = args.skip_orders

    start = datetime.now()
    print(f"Migration started at {start.strftime('%Y-%m-%d %H:%M:%S')}")

    if not entity or entity == "tiers":
        migrate_pricing_tiers(sb)
    if not entity or entity == "products":
        migrate_products(sb)
    if not entity or entity == "customers":
        migrate_customers(sb, sq)
    if not skip_orders:
        if not entity or entity == "orders":
            migrate_orders(sb, sq)
        if not entity or entity == "invoices":
            migrate_invoices(sb, sq)

    elapsed = (datetime.now() - start).seconds
    print(f"\nMigration complete in {elapsed}s.")


if __name__ == "__main__":
    main()
