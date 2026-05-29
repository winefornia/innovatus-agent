"""Replay a reservation case from DB raw_email_events through the case_desk_graph.

Usage:
    python scripts/replay_case.py --case-id TASTING-MIRA-20260607-1430
    python scripts/replay_case.py --case-id TASTING-... --dry-run
    python scripts/replay_case.py --case-id TASTING-... --verbose

--dry-run  : pass disable_actions=True (default True — always safe in replay)
--verbose  : print full judgment JSON
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("POSTGRES_CONNECTION_STRING", "")
os.environ.setdefault("TELEGRAM_APPROVAL_CHAT_ID", "")
os.environ.setdefault("TELEGRAM_TASTINGROOM_BOT_TOKEN", "")

# Patch Gmail send so nothing goes out.
import services.gmail_service as _gmail
_gmail.send_email = lambda *a, **k: {"message_id": "replay-fake", "thread_id": "replay-fake", "dry_run": True}


def _load_events(case_id: str) -> list[dict]:
    """Return raw email events for the case, ordered chronologically."""
    from db import repository
    raw = repository.list_raw_email_events_for_case(case_id)
    if not raw:
        print(f"  [warn] No raw_email_events in DB for {case_id}.")
        print("         Run the live poller first, or replay_mira_case.py to seed DB.")
    return raw


def _run_replay(case_id: str, dry_run: bool, verbose: bool) -> None:
    from agents.case_desk_graph import case_desk_graph
    from services.case_judge import CaseJudgment

    events = _load_events(case_id)
    if not events:
        return

    print(f"\nReplaying {len(events)} events for case: {case_id}\n{'─' * 60}")

    for i, ev in enumerate(events):
        mid = ev.get("gmail_message_id", "")
        subject = ev.get("subject", "")
        from_email = ev.get("from_email", "")
        to_email = ev.get("to_email", "")
        body = ev.get("body", "")
        thread_id = ev.get("gmail_thread_id", "")

        out = case_desk_graph.invoke(
            {
                "raw_email": f"Subject: {subject}\nFrom: {from_email}\n\n{body}",
                "sender_id": from_email,
                "subject": subject,
                "from_email": from_email,
                "to_email": to_email,
                "body": body,
                "gmail_message_id": mid,
                "gmail_thread_id": thread_id,
                "disable_actions": True,
            },
            config={"configurable": {"thread_id": f"replay-{mid}"}},
        )

        j_data = out.get("_judgment", {})
        try:
            j = CaseJudgment.model_validate(j_data)
            print(f"[{i+1:02d}] {mid[:12]}…  type={out.get('message_type')}")
            print(f"       state={out.get('_reservation', {}).get('current_state', '?')}")
            print(f"       truth: client={j.current_truth.client_intent}  "
                  f"facility={j.current_truth.facility_status}  "
                  f"payment={j.current_truth.payment_status}")
            print(f"       action={j.next_best_action.tool_name}  "
                  f"conf={j.confidence:.0%}  interrupt={j.interrupt_level}")
            if j.uncertainties:
                print(f"       uncertainties: {len(j.uncertainties)} "
                      f"[{j.uncertainties[0].question[:60]}...]")
            if verbose:
                print(f"       judgment JSON:\n{json.dumps(j_data, indent=4, default=str)}")
        except Exception as ex:
            print(f"[{i+1:02d}] {mid[:12]}…  judgment parse error: {ex}")

    print(f"\n{'─' * 60}")
    from db import repository
    final = repository.get_reservation(case_id)
    if final:
        print(f"Final DB state: {final.get('current_state')} | "
              f"payment={final.get('payment_status')} | "
              f"booking={final.get('booking_status')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a case from DB through case_desk_graph.")
    parser.add_argument("--case-id", required=True, help="Reservation ID to replay.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Disable action creation (default True).")
    parser.add_argument("--verbose", action="store_true", help="Print full judgment JSON.")
    args = parser.parse_args()
    _run_replay(args.case_id, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
