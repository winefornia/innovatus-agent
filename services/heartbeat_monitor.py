"""Watcher-liveness monitor.

The tasting-room Gmail watcher is a separate Fly process; if it crash-loops or
dies, the only visible symptom is "cases stop moving" — silently. A dead process
can't report itself, so the always-up web process watches it instead: the watcher
stamps a `system_heartbeat` row each poll, and this monitor (started from the web
app) reads that row and posts a Google Chat alert if it goes silent past the
threshold, then a recovery note when it comes back.

Self-contained and defensive: it never raises out of its loop, only alerts on the
edge (silent→alerts once, recovered→notes once, no spam), and stays quiet before
the watcher's first-ever heartbeat so a fresh deploy doesn't false-alarm.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_WATCHER = "tastingroom_watcher"
_STALE_SECONDS = float(os.getenv("TR_HEARTBEAT_STALE_SECONDS", "600"))   # 10 min
_CHECK_SECONDS = float(os.getenv("TR_HEARTBEAT_CHECK_SECONDS", "120"))   # every 2 min

# Tracks whether we've already alerted for the current outage so we don't spam.
_alerted = False


def heartbeat_age_seconds() -> float | None:
    """Seconds since the watcher last stamped its heartbeat, or None if it never
    has (no row yet). Raises nothing the caller must handle — returns None on any
    read problem so callers can treat it as 'unknown'."""
    try:
        from db.repository import get_heartbeat

        row = get_heartbeat(_WATCHER)
        if not row or not row.get("last_beat_at"):
            return None
        ts = datetime.fromisoformat(str(row["last_beat_at"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception as exc:
        log.warning("[tr:heartbeat] age read failed: %s", exc)
        return None


def _alert(text: str) -> None:
    try:
        from app.adapters.google_chat_tastingroom import post_text

        post_text(text)
    except Exception as exc:
        log.warning("[tr:heartbeat] alert post failed: %s", exc)


async def _check_once() -> None:
    global _alerted
    age = heartbeat_age_seconds()
    if age is None:
        return  # watcher hasn't beat yet — stay quiet (no false alarm on deploy)
    if age > _STALE_SECONDS:
        if not _alerted:
            mins = int(age // 60)
            log.error("[tr:heartbeat] watcher silent for ~%dm — alerting", mins)
            _alert(
                f"⚠️ *Tasting-room watcher looks down.* No heartbeat for ~{mins} "
                f"min (threshold {int(_STALE_SECONDS // 60)} min). New website "
                f"requests may not be getting picked up — please check the Fly "
                f"`tastingroom_watcher` process."
            )
            _alerted = True
    else:
        if _alerted:
            log.info("[tr:heartbeat] watcher recovered (age %.0fs)", age)
            _alert("✅ *Tasting-room watcher is back.* Heartbeat resumed; "
                   "request processing has caught up.")
            _alerted = False


async def run_monitor() -> None:
    """Long-lived loop launched from the web app's startup hook."""
    log.info("[tr:heartbeat] monitor started (stale>%ss, check every %ss)",
             int(_STALE_SECONDS), int(_CHECK_SECONDS))
    while True:
        try:
            await _check_once()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[tr:heartbeat] monitor tick failed: %s", exc)
        await asyncio.sleep(_CHECK_SECONDS)
