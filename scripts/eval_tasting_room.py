"""Golden eval runner for tasting room cases.

Reads fixture files from db/eval_cases/tasting_*.json and replays each
email sequence up to each checkpoint, then asserts judgment fields.

Usage:
    python scripts/eval_tasting_room.py
    python scripts/eval_tasting_room.py --case-id TASTING-MIRA-20260607-1430
    python scripts/eval_tasting_room.py --verbose

Exit code 0 = all PASS, 1 = any FAIL.
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

import services.gmail_service as _gmail
_gmail.send_email = lambda *a, **k: {"message_id": "eval-fake", "dry_run": True}

EVAL_DIR = ROOT / "db" / "eval_cases"


def _load_fixtures(case_id: str | None) -> list[dict]:
    fixtures = []
    for path in sorted(EVAL_DIR.glob("tasting_*.json")):
        with open(path) as f:
            data = json.load(f)
        if case_id and data.get("case_id") != case_id:
            continue
        fixtures.append(data)
    return fixtures


def _run_fixture(fixture: dict, verbose: bool) -> tuple[int, int]:
    """Returns (passed, failed) counts."""
    from agents.case_desk_graph import case_desk_graph
    from services.case_judge import CaseJudgment
    from db import repository

    case_id = fixture["case_id"]
    description = fixture.get("description", case_id)
    checkpoints = fixture.get("checkpoints", [])

    print(f"\n{'═' * 60}")
    print(f"CASE: {description}")
    print(f"{'═' * 60}")

    # Load events from DB for this case.
    raw_events = repository.list_raw_email_events_for_case(case_id)
    if not raw_events:
        print(f"  [SKIP] No raw_email_events in DB for {case_id}.")
        print("         Run the live poller or replay_mira_case.py first.")
        return 0, 0

    # Build a map: source_message_id → raw_email_event
    event_map = {e["gmail_message_id"]: e for e in raw_events}

    # Also get reservoir events to know chronological order.
    res_events = repository.list_reservation_events(case_id, limit=100)
    ordered_mids = []
    seen: set[str] = set()
    for re in res_events:
        mid = re.get("source_message_id")
        if mid and mid not in seen:
            ordered_mids.append(mid)
            seen.add(mid)

    passed = 0
    failed = 0

    for cp in checkpoints:
        target_mid = cp["after_source_message_id"]
        if target_mid not in event_map:
            print(f"  [SKIP] Checkpoint {target_mid[:12]}… — not in raw_email_events.")
            continue

        # Replay up to and including this message.
        mids_to_replay = []
        for mid in ordered_mids:
            mids_to_replay.append(mid)
            if mid == target_mid:
                break
        if target_mid not in mids_to_replay:
            mids_to_replay.append(target_mid)

        last_out: dict = {}
        for mid in mids_to_replay:
            ev = event_map.get(mid)
            if not ev:
                continue
            last_out = case_desk_graph.invoke(
                {
                    "raw_email": f"Subject: {ev.get('subject','')}\nFrom: {ev.get('from_email','')}\n\n{ev.get('body','')}",
                    "sender_id": ev.get("from_email", ""),
                    "subject": ev.get("subject", ""),
                    "from_email": ev.get("from_email", ""),
                    "to_email": ev.get("to_email", ""),
                    "body": ev.get("body", ""),
                    "gmail_message_id": mid,
                    "gmail_thread_id": ev.get("gmail_thread_id", ""),
                    "disable_actions": True,
                },
                config={"configurable": {"thread_id": f"eval-{case_id}-{mid}"}},
            )

        j_data = last_out.get("_judgment", {})
        try:
            j = CaseJudgment.model_validate(j_data)
        except Exception as ex:
            print(f"  [FAIL] {target_mid[:12]}… — judgment parse error: {ex}")
            failed += 1
            continue

        truth = j.current_truth
        action = j.next_best_action.tool_name
        assertions: list[tuple[str, bool, str, str]] = []  # (field, ok, expected, actual)

        def check(field: str, expected, actual) -> None:
            ok = actual == expected if expected is not None else True
            assertions.append((field, ok, str(expected), str(actual)))

        check("message_type", cp.get("expected_message_type"), last_out.get("message_type"))
        check("client_intent", cp.get("expected_client_intent"), truth.client_intent)
        check("facility_status", cp.get("expected_facility_status"), truth.facility_status)
        check("payment_status", cp.get("expected_payment_status"), truth.payment_status)
        check("confirmation_status", cp.get("expected_confirmation_status"), truth.confirmation_status)
        check("next_best_action", cp.get("expected_action"), action)

        # Guard check: if guard_must_block_if_facility_inferred_below is set,
        # verify that weak inferred evidence would block final_confirmation.
        guard_threshold = cp.get("guard_must_block_if_facility_inferred_below")
        if guard_threshold is not None:
            from services.safety_guards import validate_plan
            from services.case_judge import CaseJudgment as CJ, ToolPlan, EvidenceRef, CurrentTruth
            test_j = j.model_copy(update={
                "next_best_action": ToolPlan(
                    tool_name="draft_final_confirmation",
                    reason="test",
                    requires_human_approval=True,
                ),
                "evidence": [
                    EvidenceRef(
                        source_message_id="test-inferred",
                        claim="Josh confirmed booking",
                        evidence_type="inferred_match",
                        confidence=guard_threshold - 0.01,
                    )
                ],
            })
            guard_allowed, _ = validate_plan(test_j)
            guard_ok = not guard_allowed  # guard MUST block
            assertions.append(("guard_blocks_weak_inferred", guard_ok, "blocked", "allowed" if guard_allowed else "blocked"))

        # Final expected state.
        if "expected_final_state" in cp:
            from db import repository as repo
            final = repo.get_reservation(case_id) or {}
            check("final_state", cp["expected_final_state"], final.get("current_state"))

        cp_passed = all(ok for _, ok, _, _ in assertions)
        label = "PASS" if cp_passed else "FAIL"
        print(f"\n  [{label}] after {target_mid[:12]}…")
        for field, ok, expected, actual in assertions:
            if expected == "None":
                continue
            tick = "✓" if ok else "✗"
            if not ok:
                print(f"         {tick} {field}: expected={expected}  actual={actual}")
            elif verbose:
                print(f"         {tick} {field}: {actual}")
        if cp_passed:
            passed += 1
        else:
            failed += 1

    return passed, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run golden eval cases for tasting room.")
    parser.add_argument("--case-id", help="Run only this case ID.")
    parser.add_argument("--verbose", action="store_true", help="Print passing assertions too.")
    args = parser.parse_args()

    fixtures = _load_fixtures(args.case_id)
    if not fixtures:
        print(f"No fixtures found in {EVAL_DIR}.")
        sys.exit(0)

    total_passed = 0
    total_failed = 0
    for fixture in fixtures:
        p, f = _run_fixture(fixture, verbose=args.verbose)
        total_passed += p
        total_failed += f

    print(f"\n{'═' * 60}")
    print(f"TOTAL: {total_passed} PASS  {total_failed} FAIL")
    sys.exit(0 if total_failed == 0 else 1)


if __name__ == "__main__":
    main()
