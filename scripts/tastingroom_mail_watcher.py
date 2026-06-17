"""Always-on Gmail watcher for tasting room reservation coordination."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import GMAIL_TASTING_POLL_SECONDS
from services.tastingroom_mailbox import poll_once, sweep_stale_cases

# Run the stale-case sweep every Nth poll (default ~30 min at a 60s interval), so
# cases waiting too long on a reply get a follow-up card instead of hanging.
_SWEEP_EVERY = max(1, int(os.getenv("TR_STALE_SWEEP_EVERY_POLLS", "30")))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting tastingroom Gmail watcher; interval=%ss", GMAIL_TASTING_POLL_SECONDS)
    polls = 0
    while True:
        try:
            result = poll_once(max_results=10)
            changed = [
                item for item in result.get("processed", [])
                if item.get("status") not in {"skipped"}
            ]
            if changed:
                logging.info("Tastingroom watcher processed: %s", changed)
        except Exception as exc:
            logging.exception("Tastingroom watcher poll failed: %s", exc)
        polls += 1
        if polls % _SWEEP_EVERY == 0:
            try:
                sweep_stale_cases()
            except Exception as exc:
                logging.exception("Tastingroom stale sweep failed: %s", exc)
        time.sleep(GMAIL_TASTING_POLL_SECONDS)


if __name__ == "__main__":
    main()
