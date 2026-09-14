"""Prevent a new public backend table from silently missing its RLS statement."""
from pathlib import Path
import re

from scripts.database_security import violations

ROOT = Path(__file__).resolve().parents[2]


def test_every_schema_table_has_explicit_rls():
    schema = (ROOT / "db/schema.sql").read_text().lower()
    tables = set(re.findall(r"create table if not exists (\w+)", schema))
    protected = set(re.findall(r"alter table (\w+)\s+enable row level security", schema))
    assert tables <= protected, f"Tables without RLS: {tables - protected}"


def test_migration_covers_every_repository_table():
    repository = (ROOT / "db/repository.py").read_text()
    tables = set(re.findall(r'\.table\("(\w+)"\)', repository))
    migration = (ROOT / "db/migrations/20260914_backend_table_security.sql").read_text()
    assert all(f"'{table}'" in migration for table in tables)
    assert "force row level security" not in migration


def test_audit_fails_for_rls_disabled_or_residual_client_grants():
    secure = dict(table_name="secure", rls_enabled=True, anon_grants=False, authenticated_grants=False)
    report = {"tables": [secure, {**secure, "table_name": "open", "rls_enabled": False},
                         {**secure, "table_name": "granted", "anon_grants": True}]}
    assert violations(report) == ["open", "granted"]
