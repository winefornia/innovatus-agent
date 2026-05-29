"""Create and backfill Gmail labels for tasting room reservation messages."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import GMAIL_TASTING_ROOT_LABEL
from services.gmail_service import _ensure_label, _get_service, apply_message_labels
from services.tastingroom_mailbox import labels_for_result


MESSAGE_ID_RE = re.compile(r"^19[a-f0-9]{10,}$", re.I)


BASE_LABELS = [
    GMAIL_TASTING_ROOT_LABEL,
    f"{GMAIL_TASTING_ROOT_LABEL}/Inbox",
    f"{GMAIL_TASTING_ROOT_LABEL}/Processed",
    f"{GMAIL_TASTING_ROOT_LABEL}/Needs Review",
    f"{GMAIL_TASTING_ROOT_LABEL}/Action Needed",
    f"{GMAIL_TASTING_ROOT_LABEL}/Awaiting Reply",
    f"{GMAIL_TASTING_ROOT_LABEL}/Facility",
    f"{GMAIL_TASTING_ROOT_LABEL}/New Requests",
    f"{GMAIL_TASTING_ROOT_LABEL}/Payment",
    f"{GMAIL_TASTING_ROOT_LABEL}/Confirmed",
    f"{GMAIL_TASTING_ROOT_LABEL}/Sent",
]


def ensure_base_labels() -> None:
    service = _get_service()
    for label in BASE_LABELS:
        _ensure_label(service, label)


def main() -> None:
    from db.repository import _get_client

    client = _get_client()
    ensure_base_labels()
    created = set()
    labeled = []

    events = (
        client.table("reservation_events")
        .select("event_type,source_message_id,reservation_id,raw_payload")
        .order("created_at", desc=True)
        .limit(200)
        .execute()
        .data
        or []
    )
    reservations = {
        row["reservation_id"]: row
        for row in (
            client.table("reservations")
            .select("reservation_id,current_state")
            .execute()
            .data
            or []
        )
    }

    for event in events:
        message_id = event.get("source_message_id") or ""
        if not MESSAGE_ID_RE.match(message_id):
            continue
        state = (reservations.get(event.get("reservation_id")) or {}).get("current_state")
        labels = labels_for_result(event.get("event_type"), state)
        for base in BASE_LABELS:
            if base not in created:
                labels.append(base)
                created.add(base)
        try:
            apply_message_labels(message_id, add_labels=list(dict.fromkeys(labels)))
            labeled.append({"message_id": message_id, "labels": labels})
        except HttpError as exc:
            labeled.append({"message_id": message_id, "error": str(exc)[:180]})
            continue

        payload = event.get("raw_payload") or {}
        send_result = payload.get("send_result") if isinstance(payload, dict) else None
        sent_message_id = (send_result or {}).get("message_id") if isinstance(send_result, dict) else None
        if sent_message_id and MESSAGE_ID_RE.match(sent_message_id):
            sent_labels = [
                GMAIL_TASTING_ROOT_LABEL,
                f"{GMAIL_TASTING_ROOT_LABEL}/Sent",
                f"{GMAIL_TASTING_ROOT_LABEL}/Processed",
            ]
            try:
                apply_message_labels(sent_message_id, add_labels=sent_labels)
                labeled.append({"message_id": sent_message_id, "labels": sent_labels})
            except HttpError as exc:
                labeled.append({"message_id": sent_message_id, "error": str(exc)[:180]})

    print({"labeled_count": len(labeled), "labeled": labeled[:30]})


if __name__ == "__main__":
    main()
