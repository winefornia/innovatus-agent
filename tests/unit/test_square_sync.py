"""Regression tests for scripts/sync.py — the Square→Supabase sync.

Locks in the July 2026 fix: `invoices.list()` returns a pager that must be
iterated directly (the old code read a nonexistent `.invoices` attribute,
got None, and stamped success while writing 0 rows), plus the two guards
that make that class of silent failure impossible again:
  - an empty target table forces a full backfill instead of incremental;
  - a failed write never stamps sync_state.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("sync_script", ROOT / "scripts" / "sync.py")
sync_script = importlib.util.module_from_spec(_spec)
sys.modules["sync_script"] = sync_script
_spec.loader.exec_module(sync_script)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class FakeTable:
    def __init__(self, db, name):
        self.db = db
        self.name = name
        self._eq = None
        self._range = None
        self._order = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def limit(self, n):
        return self

    def order(self, col):
        self._order = col
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def upsert(self, rows, on_conflict=None, default_to_null=True):
        if self.name in self.db.fail_upserts:
            raise RuntimeError("simulated write failure")
        if isinstance(rows, dict):
            rows = [rows]
        store = self.db.tables.setdefault(self.name, {})
        for r in rows:
            store[r[on_conflict]] = {**store.get(r[on_conflict], {}), **r}
        return self

    def execute(self):
        rows = list(self.db.tables.get(self.name, {}).values())
        if self._eq:
            col, val = self._eq
            rows = [r for r in rows if r.get(col) == val]
        count = len(rows)
        if self._order:
            rows = sorted(rows, key=lambda r: r[self._order])
        if self._range:
            start, end = self._range
            rows = rows[start:end + 1]
        return FakeResp(data=rows, count=count)


class FakeDB:
    def __init__(self):
        self.tables = {}
        self.fail_upserts = set()

    def table(self, name):
        return FakeTable(self, name)

    def seed_sync_state(self, entity, last_synced):
        self.tables.setdefault("sync_state", {})[entity] = {
            "entity": entity, "last_synced": last_synced,
        }

    def sync_state_entities(self):
        return set(self.tables.get("sync_state", {}))


def _invoice(inv_id, number, updated_at):
    return SimpleNamespace(
        id=inv_id,
        order_id=f"ord_{inv_id}",
        primary_recipient=SimpleNamespace(customer_id="SQCUST1"),
        payment_requests=[SimpleNamespace(
            due_date="2026-08-01",
            total_completed_amount_money=None,
            computed_amount_money=SimpleNamespace(amount=12000),
        )],
        invoice_number=number,
        title="Wine order",
        status="PAID",
        delivery_method="EMAIL",
        created_at="2026-06-01T00:00:00Z",
        updated_at=updated_at,
    )


class FakeSquare:
    """invoices.list returns a plain iterator — pager-shaped, with NO
    `.invoices` attribute. This is exactly the shape the old code mishandled."""

    def __init__(self, invoices):
        self.invoices = SimpleNamespace(list=lambda **kwargs: iter(invoices))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_table_forces_full_backfill_and_writes_all_invoices():
    db = FakeDB()
    # sync_state claims a recent sync (the lying state the bug left behind),
    # but the table is empty — old invoices must still be written.
    db.seed_sync_state("invoices", "2026-07-05T09:00:00+00:00")
    sq = FakeSquare([
        _invoice("inv_old", "202464", "2026-06-05T00:00:00Z"),
        _invoice("inv_new", "202470", "2026-07-10T00:00:00Z"),
    ])

    sync_script.sync_invoices(db, sq)

    written = db.tables["square_invoices"]
    assert set(written) == {"inv_old", "inv_new"}
    assert written["inv_old"]["invoice_number"] == "202464"
    assert written["inv_old"]["total_money_cents"] == 12000
    assert written["inv_old"]["square_customer_id"] == "SQCUST1"


def test_incremental_skips_only_older_invoices_when_table_populated():
    db = FakeDB()
    db.tables["square_invoices"] = {"inv_seed": {"square_invoice_id": "inv_seed", "id": "x"}}
    db.seed_sync_state("invoices", "2026-07-05T00:00:00+00:00")
    sq = FakeSquare([
        _invoice("inv_before", "202464", "2026-07-01T00:00:00Z"),
        _invoice("inv_after", "202470", "2026-07-10T00:00:00Z"),
    ])

    sync_script.sync_invoices(db, sq)

    written = db.tables["square_invoices"]
    assert "inv_after" in written
    assert "inv_before" not in written  # older than the incremental window


def test_failed_write_never_stamps_sync_state():
    db = FakeDB()
    db.fail_upserts.add("square_invoices")
    sq = FakeSquare([_invoice("inv_1", "202471", "2026-07-10T00:00:00Z")])

    with pytest.raises(RuntimeError):
        sync_script.sync_invoices(db, sq)

    assert "invoices" not in db.sync_state_entities()


def test_iso_normalizes_datetimes_and_passes_strings_through():
    from datetime import date, datetime, timezone
    assert sync_script._iso(None) is None
    assert sync_script._iso("2026-07-10T00:00:00Z") == "2026-07-10T00:00:00Z"
    assert sync_script._iso(date(2026, 8, 1)) == "2026-08-01"
    assert sync_script._iso(
        datetime(2026, 7, 10, tzinfo=timezone.utc)
    ) == "2026-07-10T00:00:00+00:00"


def test_sync_watermark_is_start_not_finish(monkeypatch):
    from datetime import datetime, timezone
    class Clock(datetime):
        ticks = iter([datetime(2026, 9, 14, 1, tzinfo=timezone.utc),
                      datetime(2026, 9, 14, 2, tzinfo=timezone.utc)])
        @classmethod
        def now(cls, tz=None):
            return next(cls.ticks, datetime(2026, 9, 14, 3, tzinfo=timezone.utc))
    monkeypatch.setattr(sync_script, "datetime", Clock)
    db = FakeDB()
    sync_script.sync_invoices(db, FakeSquare([_invoice("one", "1", "2026-09-14T01:30:00Z")]), full=True)
    assert db.tables["sync_state"]["invoices"]["last_synced"] == "2026-09-14T01:00:00+00:00"


def test_overlap_and_timezone_offsets_do_not_drop_updates():
    db = FakeDB()
    db.tables["square_invoices"] = {"seed": {"id": "seed", "square_invoice_id": "seed"}}
    db.seed_sync_state("invoices", "2026-09-14T01:00:00Z")
    sq = FakeSquare([_invoice("overlap", "1", "2026-09-14T00:55:00Z"),
                     _invoice("offset", "2", "2026-09-13T21:00:00-04:00"),
                     _invoice("old", "3", "2026-09-14T00:40:00Z")])
    sync_script.sync_invoices(db, sq)
    assert set(db.tables["square_invoices"]) == {"seed", "overlap", "offset"}


def test_missing_state_forces_full_history_even_for_populated_table():
    db = FakeDB()
    db.tables["square_invoices"] = {"seed": {"id": "seed", "square_invoice_id": "seed"}}
    assert sync_script._since_or_none(db, "invoices", "square_invoices", False) is None


def test_customer_tiers_not_sent_and_map_reads_beyond_1000():
    db = FakeDB()
    db.tables["customers"] = {f"c{i}": {"id": f"id{i:04}", "square_customer_id": f"c{i}", "tier_name": "wholesale"}
                              for i in range(1201)}
    assert len(sync_script._build_customer_map(db)) == 1201
    customer = SimpleNamespace(id="c1200", given_name="Test", family_name="Customer", updated_at="2026-09-14T00:00:00Z")
    square = SimpleNamespace(customers=SimpleNamespace(list=lambda **kw: iter([customer])))
    sync_script.sync_customers(db, square, full=True)
    assert db.tables["customers"]["c1200"]["tier_name"] == "wholesale"


def test_invoice_total_uses_due_amount_across_installments():
    db = FakeDB()
    inv = _invoice("unpaid", "4", "2026-09-14T00:00:00Z")
    inv.payment_requests[0].total_completed_amount_money = SimpleNamespace(amount=0)
    inv.payment_requests.append(SimpleNamespace(computed_amount_money=SimpleNamespace(amount=3000)))
    sync_script.sync_invoices(db, FakeSquare([inv]), full=True)
    assert db.tables["square_invoices"]["unpaid"]["total_money_cents"] == 15000


def test_orders_continue_on_empty_page_and_reject_cursor_loop(monkeypatch):
    monkeypatch.setattr(sync_script.time, "sleep", lambda _: None)
    db = FakeDB()
    calls = []
    def search(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(orders=[], errors=None, cursor="repeated")
    sq = SimpleNamespace(orders=SimpleNamespace(search=search))
    with pytest.raises(RuntimeError, match="repeated"):
        sync_script.sync_orders(db, sq, full=True)
    assert len(calls) == 2
    assert "orders" not in db.sync_state_entities()


def test_square_error_response_does_not_stamp_success():
    db = FakeDB()
    sq = SimpleNamespace(orders=SimpleNamespace(search=lambda **kw: SimpleNamespace(errors=["unauthorized"])))
    with pytest.raises(RuntimeError, match="error response"):
        sync_script.sync_orders(db, sq, full=True)
    assert "orders" not in db.sync_state_entities()


def test_partial_upsert_acknowledgement_is_not_success(monkeypatch):
    monkeypatch.setattr(FakeTable, "execute", lambda self: FakeResp(data=[], count=10))
    with pytest.raises(RuntimeError, match="incomplete"):
        sync_script._batch_upsert(FakeDB(), "square_invoices", [{"square_invoice_id": "one"}], "square_invoice_id")


def test_transient_upsert_retries_but_permanent_errors_do_not(monkeypatch):
    import httpx
    monkeypatch.setattr(sync_script.time, "sleep", lambda _: None)
    original = FakeTable.upsert
    attempts = []
    def flaky(self, *args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("temporary")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FakeTable, "upsert", flaky)
    sync_script._batch_upsert(FakeDB(), "square_invoices", [{"square_invoice_id": "one"}], "square_invoice_id")
    assert len(attempts) == 2
    assert not sync_script._retryable(RuntimeError("bad schema"))
