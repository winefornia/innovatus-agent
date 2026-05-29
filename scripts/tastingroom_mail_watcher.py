"""Always-on Gmail watcher for tasting room reservation coordination."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import GMAIL_TASTING_POLL_SECONDS
from services.tastingroom_mailbox import poll_once


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting tastingroom Gmail watcher; interval=%ss", GMAIL_TASTING_POLL_SECONDS)
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
        time.sleep(GMAIL_TASTING_POLL_SECONDS)


if __name__ == "__main__":
    main()
