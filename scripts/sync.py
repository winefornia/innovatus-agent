"""Weekly incremental sync: Square → Supabase.

Pulls only records created/updated since the last successful sync.
Sync state (cursor + timestamp) is stored in the `sync_state` table.

Usage:
    python scripts/sync.py                    # sync all entities
    python scripts/sync.py --entity customers # sync one entity

Automation (cron):
    # Run every Sunday at 2am
    0 2 * * 0 cd /path/to/winefornia-agent && .venv/bin/python scripts/sync.py >> logs/sync.log 2>&1
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
    SQUARE_PROD_ACCESS_TOKEN,
    SQUARE_PROD_LOCATION_ID,
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
)
from supabase import create_client

FALLBACK_LOOKBACK_DAYS = 8  # if no sync state, pull last 8 days


def _square():
    from square import Square
    from square.environment import SquareEnvironment
    return Square(token=SQUARE_PROD_ACCESS_TOKEN, environment=SquareEnvironment.PRODUCTION)


def _supabase():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ---------------------------------------------------------------------------
# Sync state helpers
# ---------------------------------------------------------------------------

def get_last_synced(sb, entity: str) -> str:
    """Return ISO timestamp of last sync, or 8 days ago as fallback."""
    result = sb.table("sync_state").select("last_synced").eq("entity", entity).execute()
    rows = result.data or []
    if rows and rows[0].get("last_synced"):
        return rows[0]["last_synced"]
    return (datetime.now(timezone.utc) - timedelta(days=FALLBACK_LOOKBACK_DAYS)).isoformat()


def set_last_synced(sb, entity: str, ts: str):
    sb.table("sync_state").upsert(
        {"entity": entity, "last_synced": ts, "notes": f"synced at {datetime.now(timezone.utc).isoformat()}"},
        on_conflict="entity",
    ).execute()


# ---------------------------------------------------------------------------
# Sync customers
# ---------------------------------------------------------------------------

def sync_customers(sb, sq):
    since = get_last_synced(sb, "customers")
    print(f"\n[customers] Syncing since {since}...")

    # Get existing tier assignments from DB (preserve them)
    existing = sb.table("customers").select("square_customer_id, tier_name").execute()
    tier_map = {r["square_customer_id"]: r["tier_name"] for r in (existing.data or [])}

    rows = []

    for c in sq.customers.list():
        created = getattr(c, "created_at", None)
        updated = getattr(c, "updated_at", None)
        check = updated or created or ""
        if check and check < since:
            continue

        email = getattr(c, "email_address", None) or ""
        full_name = " ".join(filter(None, [
            getattr(c, "given_name", None),
            getattr(c, "family_name", None),
        ])).strip() or getattr(c, "company_name", None) or ""

        rows.append({
            "square_customer_id": c.id,
            "full_name": full_name or None,
            "company": getattr(c, "company_name", None),
            "email": email or None,
            "phone": getattr(c, "phone_number", None),
            "tier_name": tier_map.get(c.id),
            "square_created_at": created,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })

    if rows:
        _batch_upsert(sb, "customers", rows, "square_customer_id")
    set_last_synced(sb, "customers", datetime.now(timezone.utc).isoformat())
    print(f"    {len(rows)} customers synced.")


# ---------------------------------------------------------------------------
# Sync orders
# ---------------------------------------------------------------------------

def sync_orders(sb, sq):
    since = get_last_synced(sb, "orders")
    print(f"\n[orders] Syncing since {since}...")
    cust_map = _build_customer_map(sb)

    rows = []
    cursor = None

    while True:
        body = {
            "location_ids": [SQUARE_PROD_LOCATION_ID],
            "query": {
                "filter": {
                    "date_time_filter": {
                        "updated_at": {"start_at": since}
                    }
                },
                "sort": {"sort_field": "UPDATED_AT", "sort_order": "ASC"},
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

        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break
        time.sleep(0.3)

    if rows:
        _batch_upsert(sb, "square_orders", rows, "square_order_id")
    set_last_synced(sb, "orders", datetime.now(timezone.utc).isoformat())
    print(f"    {len(rows)} orders synced.")


# ---------------------------------------------------------------------------
# Sync invoices
# ---------------------------------------------------------------------------

def sync_invoices(sb, sq):
    since = get_last_synced(sb, "invoices")
    print(f"\n[invoices] Syncing since {since}...")
    cust_map = _build_customer_map(sb)

    rows = []
    cursor = None

    while True:
        kwargs = {"location_id": SQUARE_PROD_LOCATION_ID, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor

        resp = sq.invoices.list(**kwargs)
        invoices = getattr(resp, "invoices", None) or []
        if not invoices:
            break

        for inv in invoices:
            updated = getattr(inv, "updated_at", None)
            if updated and updated < since:
                continue

            cid = None
            if inv.primary_recipient:
                cid = getattr(inv.primary_recipient, "customer_id", None)

            payment_req = (inv.payment_requests or [None])[0]
            due_date = getattr(payment_req, "due_date", None) if payment_req else None
            total_cents = None
            if payment_req:
                for attr in ["total_completed_amount_money", "computed_amount_money"]:
                    m = getattr(payment_req, attr, None)
                    if m:
                        total_cents = m.amount
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
                "invoice_created_at": getattr(inv, "created_at", None),
                "invoice_updated_at": updated,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            })

        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break
        time.sleep(0.3)

    if rows:
        _batch_upsert(sb, "square_invoices", rows, "square_invoice_id")
    set_last_synced(sb, "invoices", datetime.now(timezone.utc).isoformat())
    print(f"    {len(rows)} invoices synced.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_customer_map(sb) -> dict:
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
    parser.add_argument("--entity", choices=["customers", "orders", "invoices"],
                        help="Sync only one entity")
    args = parser.parse_args()

    if not SQUARE_PROD_ACCESS_TOKEN:
        sys.exit("ERROR: SQUARE_PROD_ACCESS_TOKEN not set in .env")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        sys.exit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not set in .env")

    sb = _supabase()
    sq = _square()

    entity = args.entity
    start = datetime.now()
    print(f"Sync started at {start.strftime('%Y-%m-%d %H:%M:%S')}")

    if not entity or entity == "customers":
        sync_customers(sb, sq)
    if not entity or entity == "orders":
        sync_orders(sb, sq)
    if not entity or entity == "invoices":
        sync_invoices(sb, sq)

    elapsed = (datetime.now() - start).seconds
    print(f"\nSync complete in {elapsed}s.")


if __name__ == "__main__":
    main()
