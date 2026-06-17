"""Google Calendar — create the tasting-visit event and invite all three parties.

The end state of a successful coordination: a calendar event for the confirmed
slot with attendees Winefornia (Lisa), the customer, and Josh — even for a
standard tasting. Uses the same service account + domain-wide delegation as Gmail
(the calendar scope is authorized for innovatuswine.com), so the event is created
as Lisa and invites are sent to everyone.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_TZ = os.getenv("TASTINGROOM_TIMEZONE", "America/Los_Angeles")
_DURATION_MIN = int(os.getenv("TASTINGROOM_VISIT_MINUTES", "90"))


def _calendar_service():
    """Build a Calendar API client via the SA + DWD (calendar scope is granted)."""
    try:
        from googleapiclient.discovery import build
        from services.gmail_service import _get_service_account_creds
        creds = _get_service_account_creds()
        if not creds:
            log.warning("[calendar] no service-account/DWD creds — cannot create event")
            return None
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        log.error("[calendar] could not build service: %s", e)
        return None


def _parse_start(date_str: str | None, time_str: str | None) -> datetime | None:
    """Combine 'YYYY-MM-DD' + 'HH:MM[:SS]' into a naive local datetime."""
    if not date_str:
        return None
    d = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(date_str))
    if not d:
        return None
    hh, mm = 11, 0  # sensible default if no time captured
    t = re.search(r"(\d{1,2}):(\d{2})", str(time_str or ""))
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
    try:
        return datetime(int(d.group(1)), int(d.group(2)), int(d.group(3)), hh, mm)
    except ValueError:
        return None


def create_tasting_event(*, reservation_id: str, summary: str, date_str: str | None,
                         time_str: str | None, attendees: list[str],
                         description: str = "", location: str = "") -> str | None:
    """Create the visit event and invite attendees (sendUpdates='all'). Returns the
    event link, or None on any failure. Never raises."""
    svc = _calendar_service()
    if not svc:
        return None
    start = _parse_start(date_str, time_str)
    if not start:
        log.warning("[calendar] no valid date/time for %s (date=%r time=%r) — skipping invite",
                    reservation_id, date_str, time_str)
        return None
    end = start + timedelta(minutes=_DURATION_MIN)
    clean = [e for e in dict.fromkeys(a.strip() for a in attendees if a and "@" in a)]
    body = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start.isoformat(), "timeZone": _TZ},
        "end": {"dateTime": end.isoformat(), "timeZone": _TZ},
        "attendees": [{"email": e} for e in clean],
    }
    try:
        ev = svc.events().insert(calendarId="primary", body=body, sendUpdates="all").execute()
        log.info("[calendar] created event for %s with %d attendees → %s",
                 reservation_id, len(clean), ev.get("htmlLink"))
        return ev.get("htmlLink")
    except Exception as e:
        log.error("[calendar] event create failed for %s: %s", reservation_id, e)
        return None
