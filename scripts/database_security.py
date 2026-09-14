"""Audit backend table security; --apply installs the reviewed RLS migration.

Runs inside Fly with existing secrets. Prints metadata only, never credentials
or customer rows. Unknown public tables are reported, not automatically changed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT = "zlbixpklvejcuxifqzjk"
MIGRATION = Path(__file__).resolve().parents[1] / "db/migrations/20260914_backend_table_security.sql"

TABLE_AUDIT = """
select c.relname as table_name, c.relrowsecurity as rls_enabled,
       has_table_privilege('service_role', c.oid, 'SELECT,INSERT,UPDATE,DELETE') as service_access,
       (select count(*) from pg_policy p where p.polrelid=c.oid) as policy_count,
       has_table_privilege('anon', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
         or has_any_column_privilege('anon', c.oid, 'SELECT,INSERT,UPDATE,REFERENCES') as anon_grants,
       has_table_privilege('authenticated', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
         or has_any_column_privilege('authenticated', c.oid, 'SELECT,INSERT,UPDATE,REFERENCES') as authenticated_grants
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname='public' and c.relkind in ('r','p') order by c.relname
"""


def audit(conn):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(TABLE_AUDIT)
        tables = cur.fetchall()
        cur.execute("select rolbypassrls from pg_roles where rolname='service_role'")
        bypass = cur.fetchone()["rolbypassrls"]
        cur.execute("""select c.relname as name, c.relkind as kind
            from pg_class c join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='public' and c.relkind in ('v','m','f')
            and (has_table_privilege('anon',c.oid,'SELECT')
              or has_table_privilege('authenticated',c.oid,'SELECT'))""")
        exposed_relations = cur.fetchall()
    return {"tables": tables, "service_role_bypasses_rls": bypass,
            "exposed_views_or_foreign_tables": exposed_relations}


def violations(report):
    # Backend tables must deny API roles even if a future permissive policy is
    # added accidentally. RLS alone does not protect TRUNCATE privileges.
    return [t["table_name"] for t in report["tables"]
            if not t["rls_enabled"] or t["anon_grants"] or t["authenticated_grants"]]


def main():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if urlparse(os.environ.get("SUPABASE_URL", "")).hostname != f"{PROJECT}.supabase.co":
        raise RuntimeError("SUPABASE_URL does not match the intended project")
    conninfo = os.environ.get("POSTGRES_CONNECTION_STRING", "")
    if not conninfo:
        raise RuntimeError("POSTGRES_CONNECTION_STRING is missing")
    cfg = conninfo_to_dict(conninfo)
    if PROJECT not in (cfg.get("host", "") + " " + cfg.get("user", "")):
        raise RuntimeError("Database connection does not identify the intended project")
    if str(cfg.get("port", "")) != "6543":
        raise RuntimeError("Use the configured Supabase transaction pooler on port 6543")
    cfg.setdefault("sslmode", "require")
    with psycopg.connect(**cfg, connect_timeout=10, prepare_threshold=None) as conn:
        conn.execute("set local statement_timeout = '30s'")
        conn.execute("set local lock_timeout = '5s'")
        before = audit(conn)
        print(json.dumps({"phase": "before", **before}, default=str), flush=True)
        if args.apply:
            if not before["service_role_bypasses_rls"]:
                raise RuntimeError("Backend role cannot bypass RLS; refusing access changes")
            conn.execute(MIGRATION.read_text())
            after = audit(conn)
            # Roll back on incomplete protection or any lost backend privilege.
            old_access = {t['table_name']: t['service_access'] for t in before['tables']}
            if any(old_access[t['table_name']] and not t['service_access'] for t in after['tables']):
                raise RuntimeError("Backend table access changed; rolling back")
            if violations(after) or after["exposed_views_or_foreign_tables"]:
                raise RuntimeError("Unresolved public exposure; transaction rolled back; inspect audit")
            print(json.dumps({"phase": "after", **after}, default=str), flush=True)
        else:
            if violations(before) or before["exposed_views_or_foreign_tables"]:
                print("SECURITY AUDIT FAILED: public table exposure requires repair", flush=True)
                return 1
    print("Security verification passed" + ("; transaction committed" if args.apply else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Driver errors can contain connection strings or row contents. Do not
        # emit raw exceptions in public Actions logs.
        print(f"Database security operation failed ({type(exc).__name__}); no credentials logged", file=sys.stderr)
        raise SystemExit(1)
