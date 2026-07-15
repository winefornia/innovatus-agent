"""Weekly incremental sync: Square → Supabase.

Pulls only records created/updated since the last successful sync.
Sync state (cursor + timestamp) is stored in the `sync_state` table.

Usage:
    python scripts/sync.py                    # sync all entities
    python scripts/sync.py --entity customers # sync one entity
    python scripts/sync.py --full             # ignore sync_state, pull ALL history

Automation: .github/workflows/square-sync.yml runs this weekly (Sundays
09:00 UTC). It previously ran from a personal machine — see
docs/ownership-and-migration.md §4.

Failure contract: an entity that errors does NOT stamp sync_state (so the
next run retries the same window) and the process exits non-zero so the
scheduler surfaces the failure. A step that writes nothing while Square
returned data is treated as an error, never as success — that exact silent
failure left `square_invoices` empty for weeks in July 2026 (the old code
read `.invoices` off what is now a pager object, got None, and declared
victory).
"""
import argparse
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


def _iso(v):
    """Timestamps/dates from the Square SDK may be strings or date(time)
    objects depending on SDK version — normalize to ISO strings so string
    comparisons against sync_state and PostgREST payloads are always valid."""
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


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


def _table_count(sb, table: str) -> int:
    resp = sb.table(table).select("id", count="exact").limit(1).execute()
    return resp.count or 0


def _since_or_none(sb, entity: str, table: str, full: bool) -> str | None:
    """The incremental window, or None for a full pull.

    Forces a full pull when the target table is empty: an incremental window
    on an empty table can only ever extend history that was never written —
    it can't reconstruct it. This is what turns a wiped/never-filled table
    into a self-healing one instead of a permanently hollow one.
    """
    if full:
        print(f"[{entity}] --full: ignoring sync_state, pulling all history.")
        return None
    if _table_count(sb, table) == 0:
        print(f"[{entity}] {table} is EMPTY — forcing full backfill instead of incremental.")
        return None
    return get_last_synced(sb, entity)


# ---------------------------------------------------------------------------
# Sync customers
# ---------------------------------------------------------------------------

def sync_customers(sb, sq, full: bool = False):
    since = _since_or_none(sb, "customers", "customers", full)
    print(f"\n[customers] Syncing since {since or 'the beginning'}...")

    # Get existing tier assignments from DB (preserve them)
    existing = sb.table("customers").select("square_customer_id, tier_name").execute()
    tier_map = {r["square_customer_id"]: r["tier_name"] for r in (existing.data or [])}

    fetched = 0
    rows = []

    for c in sq.customers.list():
        fetched += 1
        created = _iso(getattr(c, "created_at", None))
        updated = _iso(getattr(c, "updated_at", None))
        check = updated or created or ""
        if since and check and check < since:
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

    _finish_entity(sb, "customers", "customers", fetched, rows, "square_customer_id", since)


# ---------------------------------------------------------------------------
# Sync orders
# ---------------------------------------------------------------------------

def sync_orders(sb, sq, full: bool = False):
    since = _since_or_none(sb, "orders", "square_orders", full)
    print(f"\n[orders] Syncing since {since or 'the beginning'}...")
    cust_map = _build_customer_map(sb)

    fetched = 0
    rows = []
    cursor = None

    while True:
        query = {"sort": {"sort_field": "UPDATED_AT", "sort_order": "ASC"}}
        if since:
            query["filter"] = {"date_time_filter": {"updated_at": {"start_at": since}}}
        body = {
            "location_ids": [SQUARE_PROD_LOCATION_ID],
            "query": query,
            "limit": 500,
        }
        if cursor:
            body["cursor"] = cursor

        resp = sq.orders.search(**body)
        orders = getattr(resp, "orders", None) or []
        if not orders:
            break

        for o in orders:
            fetched += 1
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
                "order_created_at": _iso(o.created_at),
                "order_updated_at": _iso(o.updated_at),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            })

        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break
        time.sleep(0.3)

    _finish_entity(sb, "orders", "square_orders", fetched, rows, "square_order_id", since)


# ---------------------------------------------------------------------------
# Sync invoices
# ---------------------------------------------------------------------------

def sync_invoices(sb, sq, full: bool = False):
    since = _since_or_none(sb, "invoices", "square_invoices", full)
    print(f"\n[invoices] Syncing since {since or 'the beginning'}...")
    cust_map = _build_customer_map(sb)

    fetched = 0
    rows = []

    # invoices.list returns a pager — iterate it directly, exactly like
    # customers.list above. (The old code read a nonexistent `.invoices`
    # attribute off this pager, got None, and silently synced nothing.)
    for inv in sq.invoices.list(location_id=SQUARE_PROD_LOCATION_ID, limit=200):
        fetched += 1
        updated = _iso(getattr(inv, "updated_at", None))
        if since and updated and updated < since:
            continue

        cid = None
        if inv.primary_recipient:
            cid = getattr(inv.primary_recipient, "customer_id", None)

        payment_req = (inv.payment_requests or [None])[0]
        due_date = _iso(getattr(payment_req, "due_date", None)) if payment_req else None
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
            "invoice_created_at": _iso(getattr(inv, "created_at", None)),
            "invoice_updated_at": updated,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })

    _finish_entity(sb, "invoices", "square_invoices", fetched, rows, "square_invoice_id", since)


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


def _finish_entity(sb, entity: str, table: str, fetched: int, rows: list,
                   conflict_col: str, since: str | None):
    """Write, verify, and only then stamp sync_state.

    The verification is the anti-silent-failure guard: if we fetched data from
    Square but the table is still empty after our upsert, something is wrong
    with the write path and we refuse to stamp success.
    """
    if rows:
        _batch_upsert(sb, table, rows, conflict_col)
        if _table_count(sb, table) == 0:
            raise RuntimeError(
                f"[{entity}] upsert of {len(rows)} rows reported success but "
                f"{table} is still empty — refusing to stamp sync_state."
            )
    elif fetched == 0 and since is None:
        # A full pull that fetched nothing at all is suspicious (Square has
        # data for every other entity). Warn loudly but don't fail: a truly
        # empty Square account is legitimate on day one.
        print(f"    WARNING: full pull fetched 0 {entity} from Square — "
              f"verify the location/token if this is unexpected.")

    set_last_synced(sb, entity, datetime.now(timezone.utc).isoformat())
    print(f"    fetched {fetched} from Square, wrote {len(rows)} to {table}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity", choices=["customers", "orders", "invoices"],
                        help="Sync only one entity")
    parser.add_argument("--full", action="store_true",
                        help="Ignore sync_state and pull all history (idempotent upserts)")
    args = parser.parse_args()

    if not SQUARE_PROD_ACCESS_TOKEN:
        sys.exit("ERROR: SQUARE_PROD_ACCESS_TOKEN not set in .env")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        sys.exit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not set in .env")

    sb = _supabase()
    sq = _square()

    start = datetime.now()
    print(f"Sync started at {start.strftime('%Y-%m-%d %H:%M:%S')}")

    failures = []
    for name, fn in [("customers", sync_customers),
                     ("orders", sync_orders),
                     ("invoices", sync_invoices)]:
        if args.entity and args.entity != name:
            continue
        try:
            fn(sb, sq, full=args.full)
        except Exception as exc:  # noqa: BLE001 — one entity failing must not hide the others
            print(f"    ERROR [{name}]: {exc} — sync_state NOT stamped; next run retries.")
            failures.append(name)

    elapsed = (datetime.now() - start).seconds
    if failures:
        sys.exit(f"Sync FAILED for: {', '.join(failures)} (after {elapsed}s). See errors above.")
    print(f"\nSync complete in {elapsed}s.")


if __name__ == "__main__":
    main()
