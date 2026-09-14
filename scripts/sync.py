"""Weekly incremental sync: Square → Supabase.

Pulls only records created/updated since the last successful sync.
Sync start watermarks and results are stored in `sync_state`. Incremental
windows overlap by ten minutes so timestamps and concurrent updates are replayed.

Usage:
    python scripts/sync.py                    # sync all entities
    python scripts/sync.py --entity customers # sync one entity
    python scripts/sync.py --full             # ignore sync_state, pull ALL history

Automation: .github/workflows/square-sync.yml runs this weekly (Sundays
09:17 UTC). It previously ran from a personal machine — see
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
import json
import random
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

OVERLAP = timedelta(minutes=10)
REQUEST_OPTIONS = {"timeout_in_seconds": 60, "max_retries": 3}


def _square():
    from square import Square
    from square.environment import SquareEnvironment
    return Square(token=SQUARE_PROD_ACCESS_TOKEN, environment=SquareEnvironment.PRODUCTION, timeout=60)


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

def _timestamp(value) -> datetime:
    value = datetime.fromisoformat(_iso(value).replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def get_last_synced(sb, entity: str) -> str | None:
    """Replay a small overlap; missing checkpoints require a full reconciliation."""
    result = sb.table("sync_state").select("last_synced").eq("entity", entity).execute()
    rows = result.data or []
    if rows and rows[0].get("last_synced"):
        return (_timestamp(rows[0]["last_synced"]) - OVERLAP).isoformat()
    return None


def set_last_synced(sb, entity: str, ts: str, *, fetched=0, written=0):
    sb.table("sync_state").upsert(
        {"entity": entity, "last_synced": ts, "notes": json.dumps({
            "status": "success", "finished_at": datetime.now(timezone.utc).isoformat(),
            "fetched": fetched, "written": written,
        })}, on_conflict="entity",
    ).execute()


def _table_count(sb, table: str) -> int:
    resp = sb.table(table).select("id", count="exact").limit(1).execute()
    if resp.count is None:
        raise RuntimeError(f"[{table}] database did not return an exact count")
    return resp.count


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
    started_at = datetime.now(timezone.utc).isoformat()
    since = _since_or_none(sb, "customers", "customers", full)
    print(f"\n[customers] Syncing since {since or 'the beginning'}...")

    # Staff own tier_name. Never send it in an upsert: reading then writing
    # tiers can both truncate at PostgREST's page limit and overwrite an edit
    # that happened while this sync was running.
    fetched = 0
    rows = []

    for c in sq.customers.list(request_options=REQUEST_OPTIONS):
        fetched += 1
        created = _iso(getattr(c, "created_at", None))
        updated = _iso(getattr(c, "updated_at", None))
        check = updated or created or ""
        if since and check and _timestamp(check) < _timestamp(since):
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
            "square_created_at": created,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })

    _finish_entity(sb, "customers", "customers", fetched, rows, "square_customer_id", since, started_at)


# ---------------------------------------------------------------------------
# Sync orders
# ---------------------------------------------------------------------------

def sync_orders(sb, sq, full: bool = False):
    started_at = datetime.now(timezone.utc).isoformat()
    since = _since_or_none(sb, "orders", "square_orders", full)
    print(f"\n[orders] Syncing since {since or 'the beginning'}...")
    cust_map = _build_customer_map(sb)

    fetched = 0
    rows = []
    cursor = None
    seen_cursors = set()

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

        resp = sq.orders.search(**body, request_options=REQUEST_OPTIONS)
        if getattr(resp, "errors", None):
            raise RuntimeError("[orders] Square returned an error response; checkpoint unchanged")
        orders = getattr(resp, "orders", None) or []

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
        if cursor in seen_cursors:
            raise RuntimeError("[orders] Square repeated a pagination cursor")
        seen_cursors.add(cursor)
        time.sleep(0.3)

    _finish_entity(sb, "orders", "square_orders", fetched, rows, "square_order_id", since, started_at)


# ---------------------------------------------------------------------------
# Sync invoices
# ---------------------------------------------------------------------------

def sync_invoices(sb, sq, full: bool = False):
    started_at = datetime.now(timezone.utc).isoformat()
    since = _since_or_none(sb, "invoices", "square_invoices", full)
    print(f"\n[invoices] Syncing since {since or 'the beginning'}...")
    cust_map = _build_customer_map(sb)

    fetched = 0
    rows = []

    # invoices.list returns a pager — iterate it directly, exactly like
    # customers.list above. (The old code read a nonexistent `.invoices`
    # attribute off this pager, got None, and silently synced nothing.)
    for inv in sq.invoices.list(location_id=SQUARE_PROD_LOCATION_ID, limit=200, request_options=REQUEST_OPTIONS):
        fetched += 1
        updated = _iso(getattr(inv, "updated_at", None))
        if since and updated and _timestamp(updated) < _timestamp(since):
            continue

        cid = None
        if inv.primary_recipient:
            cid = getattr(inv.primary_recipient, "customer_id", None)

        payment_req = (inv.payment_requests or [None])[0]
        due_date = _iso(getattr(payment_req, "due_date", None)) if payment_req else None
        # Invoice total is the computed amount due, not the amount paid so
        # far (often zero for an unpaid invoice). Include every installment.
        amounts = []
        for req in inv.payment_requests or []:
            money = (getattr(req, "computed_amount_money", None)
                     or getattr(req, "total_completed_amount_money", None))
            if money is not None and money.amount is not None:
                amounts.append(money.amount)
        total_cents = sum(amounts) if amounts else None

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

    _finish_entity(sb, "invoices", "square_invoices", fetched, rows, "square_invoice_id", since, started_at)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_customer_map(sb) -> dict:
    # Supabase caps a response at 1,000 rows. Fetch all pages with stable order.
    mapping = {}
    offset = 0
    while True:
        result = (sb.table("customers").select("id, square_customer_id")
                  .order("id").range(offset, offset + 999).execute())
        rows = result.data or []
        mapping.update({r["square_customer_id"]: r["id"] for r in rows if r["square_customer_id"]})
        if len(rows) < 1000:
            return mapping
        offset += len(rows)


def _retryable(exc):
    import httpx
    if isinstance(exc, httpx.TransportError):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return str(status) in {"408", "429", "500", "502", "503", "504"}


def _batch_upsert(sb, table: str, rows: list, conflict_col: str, batch_size=500):
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        for attempt in range(4):
            try:
                # Only provider-owned columns are written. Missing local fields
                # must not be replaced with null on existing customers.
                response = sb.table(table).upsert(
                    batch, on_conflict=conflict_col, default_to_null=False,
                ).execute()
                break
            except Exception as exc:
                if attempt == 3 or not _retryable(exc):
                    raise
                time.sleep(min(2 ** attempt + random.random(), 10))
        returned = {r[conflict_col] for r in response.data or []}
        expected = {r[conflict_col] for r in batch}
        if not expected <= returned:
            raise RuntimeError(f"[{table}] incomplete upsert acknowledgement; checkpoint unchanged")


def _finish_entity(sb, entity: str, table: str, fetched: int, rows: list,
                   conflict_col: str, since: str | None, started_at: str):
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

    # Store the START boundary, not completion time: updates occurring after
    # an early page was fetched must be eligible for the next run.
    set_last_synced(sb, entity, started_at, fetched=fetched, written=len(rows))
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
    if not SQUARE_PROD_LOCATION_ID:
        sys.exit("ERROR: SQUARE_PROD_LOCATION_ID is not configured")
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
            # Public Actions logs must never contain provider payloads or keys.
            print(f"    ERROR [{name}]: {type(exc).__name__} — checkpoint NOT advanced; next run retries.")
            try:
                sb.table("sync_state").upsert({
                    "entity": name, "notes": json.dumps({"status": "failed",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "error_type": type(exc).__name__}),
                }, on_conflict="entity", default_to_null=False).execute()
            except Exception:
                print(f"    ERROR [{name}]: could not persist failure status")
            failures.append(name)

    elapsed = (datetime.now() - start).seconds
    if failures:
        sys.exit(f"Sync FAILED for: {', '.join(failures)} (after {elapsed}s). See errors above.")
    print(f"\nSync complete in {elapsed}s.")


if __name__ == "__main__":
    main()
