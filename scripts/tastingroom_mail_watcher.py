"""Always-on Gmail watcher for tasting room reservation coordination."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import GMAIL_TASTING_POLL_SECONDS
from services.tastingroom_mailbox import poll_once, sweep_stale_cases

# Run the stale-case sweep every Nth poll (default ~30 min at a 60s interval), so
# cases waiting too long on a reply get a follow-up card instead of hanging.
_SWEEP_EVERY = max(1, int(os.getenv("TR_STALE_SWEEP_EVERY_POLLS", "30")))

# Hard ceiling on a single poll/sweep. The downstream Gmail / Supabase / Chat
# calls have no socket timeout, so without this a single hung response wedges the
# `while True` loop forever and the watcher silently stops processing mail. On
# timeout we raise, the per-iteration except logs it, and the loop moves on.
_WATCHDOG_SECONDS = max(30, int(os.getenv("TR_WATCHDOG_SECONDS", str(GMAIL_TASTING_POLL_SECONDS * 5))))


class _WatchdogTimeout(Exception):
    pass


@contextmanager
def _deadline(seconds: int):
    """Raise _WatchdogTimeout if the wrapped block runs longer than `seconds`.

    SIGALRM-based, so it only interrupts the main thread (where this loop runs)
    and only on Unix — both true on Fly's Linux VM. No-op elsewhere (dev box).
    """
    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - non-Unix dev box
        yield
        return

    def _fire(signum, frame):
        raise _WatchdogTimeout(f"operation exceeded {seconds}s watchdog")

    prev = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def _schema_preflight() -> None:
    """Verify the live DB has every column the reservation writer uses; alert
    loudly (log + Chat) on drift. Never blocks startup — the upsert itself
    degrades gracefully — but drift must be impossible to miss."""
    try:
        from db.repository import verify_reservations_schema

        error = verify_reservations_schema()
    except Exception as exc:
        logging.warning("Schema preflight could not run: %s", exc)
        return
    if not error:
        logging.info("Schema preflight OK — reservations table matches the code.")
        return
    logging.critical("SCHEMA DRIFT: reservations table does not match the code: %s", error)
    try:
        from app.adapters.google_chat_tastingroom import post_text

        post_text(
            "🚨 Tasting-room watcher started with SCHEMA DRIFT — the reservations "
            f"table does not match the code: {error[:300]}\n"
            "Apply the pending alters in db/schema.sql to Supabase."
        )
    except Exception as exc:
        logging.warning("Schema drift Chat alert failed: %s", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting tastingroom Gmail watcher; interval=%ss", GMAIL_TASTING_POLL_SECONDS)
    _schema_preflight()
    polls = 0
    while True:
        try:
            with _deadline(_WATCHDOG_SECONDS):
                result = poll_once(max_results=10)
            changed = [
                item for item in result.get("processed", [])
                if item.get("status") not in {"skipped"}
            ]
            if changed:
                logging.info("Tastingroom watcher processed: %s", changed)
        except _WatchdogTimeout as exc:
            logging.error("Tastingroom watcher poll hung — %s; skipping to next cycle", exc)
        except Exception as exc:
            logging.exception("Tastingroom watcher poll failed: %s", exc)

        # Invoice validation loop — consumes the Square notification mail the
        # tasting intake rejects, confirming invoice cases actually reached
        # Square (and got paid). Same mailbox, fully separate pipeline.
        try:
            with _deadline(_WATCHDOG_SECONDS):
                from services.invoice_mail_validator import poll_once as validate_invoices
                v = validate_invoices(max_results=20)
            confirmed = [i for i in v.get("processed", []) if i.get("result") not in {"ignored"}]
            if confirmed:
                logging.info("Invoice mail validator processed: %s", confirmed)
        except _WatchdogTimeout as exc:
            logging.error("Invoice validator poll hung — %s; skipping to next cycle", exc)
        except Exception as exc:
            logging.exception("Invoice validator poll failed: %s", exc)
        polls += 1
        # Liveness: stamp a heartbeat each poll so the web app's monitor can tell
        # the watcher is alive (and alert if it goes silent). Best-effort.
        try:
            from db.repository import record_heartbeat
            record_heartbeat("tastingroom_watcher", {"polls": polls})
        except Exception as exc:
            logging.warning("Tastingroom heartbeat write failed: %s", exc)
        if polls % _SWEEP_EVERY == 0:
            try:
                with _deadline(_WATCHDOG_SECONDS):
                    sweep_stale_cases()
            except _WatchdogTimeout as exc:
                logging.error("Tastingroom stale sweep hung — %s; skipping", exc)
            except Exception as exc:
                logging.exception("Tastingroom stale sweep failed: %s", exc)
        time.sleep(GMAIL_TASTING_POLL_SECONDS)


if __name__ == "__main__":
    main()
