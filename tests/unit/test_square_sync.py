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

    def select(self, *args, **kwargs):
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def limit(self, n):
        return self

    def upsert(self, rows, on_conflict=None):
        if self.name in self.db.fail_upserts:
            raise RuntimeError("simulated write failure")
        if isinstance(rows, dict):
            rows = [rows]
        store = self.db.tables.setdefault(self.name, {})
        for r in rows:
            store[r[on_conflict]] = r
        return self

    def execute(self):
        rows = list(self.db.tables.get(self.name, {}).values())
        if self._eq:
            col, val = self._eq
            rows = [r for r in rows if r.get(col) == val]
        return FakeResp(data=rows, count=len(rows))


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
