#!/usr/bin/env python3
"""Reconcile orphaned control-layer cases.

Marks any agent_cases row still in status='running' older than --hours as
'abandoned' (status + outcome + closed_at + error_summary). This does NOT
delete any rows or data — it only finalizes runtime state left behind by a
process kill or an unresumed interrupt.

The bots also run this automatically at startup and hourly (see
ControlLayer.reap_stale_cases); this script is for manual/one-off cleanup.

Usage:
    .venv/bin/python scripts/reap_stale_cases.py            # default: >6h, apply
    .venv/bin/python scripts/reap_stale_cases.py --hours 1
    .venv/bin/python scripts/reap_stale_cases.py --dry-run
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=6.0,
                    help="Reap 'running' cases older than this many hours (default 6).")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be reaped without writing.")
    args = ap.parse_args()

    from db.repository import list_stale_running_cases

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()
    stale = list_stale_running_cases(cutoff)

    if not stale:
        print(f"No 'running' cases older than {args.hours}h. Nothing to reap.")
        return 0

    print(f"Found {len(stale)} stale 'running' case(s) older than {args.hours}h:")
    for r in stale:
        print(f"  {r['created_at']}  {r['case_id']}  sender={r.get('sender_id')}  intent={r.get('intent')}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return 0

    from services.control_layer import control
    reaped = control.reap_stale_cases(max_age_hours=args.hours)
    print(f"\nReaped {reaped} case(s) → status='abandoned' (data preserved).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
