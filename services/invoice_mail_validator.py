"""Validation loop for the invoice pipeline, driven by Square's own emails.

The invoice graph/chat create Square invoices, but "the API call returned OK"
is not the same as "the process actually worked." Square independently emails
the merchant mailbox for every invoice event:

    "A new invoice was created for Christina Yoo (#202468)"   → creation reached Square
    "An invoice was paid by Christina Yoo! (#202468)"          → client paid
    "Payment processed: Invoice #202447 to Ina Lee"            → client paid

This module consumes exactly those emails — the ones the tasting-room intake
deliberately rejects (they belong to the invoice pipeline; see
services/tastingroom_mailbox.py) — and closes the loop:

  - an invoice case is OPEN (invoice_logs.verification_status='pending',
    workflow_records.status='pending_verification') from the moment the graph
    creates it;
  - the "created" email confirms it → 'created_confirmed', workflow closed as
    completed_draft_saved / completed_sent;
  - the "paid" email upgrades it → 'paid_confirmed', workflow completed_paid.

Emails are matched by the Square invoice number recorded on the log at
creation time. Processed mail is labeled so it is never re-read; Square mail
that matches no logged invoice (e.g. invoices made by hand in the Square
dashboard) is labeled Unmatched for human eyes and skipped thereafter.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

_QUERY = 'from:invoicing@messaging.squareup.com -label:"Invoice Validation/Processed" -label:"Invoice Validation/Unmatched"'
_LABEL_PROCESSED = "Invoice Validation/Processed"
_LABEL_UNMATCHED = "Invoice Validation/Unmatched"

# Subject shapes Square actually sends (observed in production mail).
_CREATED_RE = re.compile(r"new invoice was created for .+?\(#\s*(\d+)\)", re.I)
_PAID_RES = (
    re.compile(r"invoice was paid by .+?\(#\s*(\d+)\)", re.I),
    re.compile(r"payment processed:.*?#\s*(\d+)", re.I),
)


def classify_square_subject(subject: str) -> tuple[Optional[str], str]:
    """→ ("created"|"paid"|None, invoice_number). None = not a validation event
    (reminders, sales summaries, payment-initiated notices...)."""
    subject = subject or ""
    m = _CREATED_RE.search(subject)
    if m:
        return "created", m.group(1)
    for pat in _PAID_RES:
        m = pat.search(subject)
        if m:
            return "paid", m.group(1)
    return None, ""


def _confirm(row: dict[str, Any], kind: str) -> str:
    """Apply one confirmation to an invoice log + its workflow record."""
    from db.repository import mark_invoice_verification, update_workflow_record_status

    thread_id = row["thread_id"]
    invoice_id = row.get("square_invoice_id") or ""
    current = row.get("verification_status") or "pending"

    if kind == "created":
        if current == "paid_confirmed":
            return "already_paid_confirmed"
        mark_invoice_verification(thread_id, status="created_confirmed",
                                  stamp_field="verified_created_at")
        # sent invoices were approved with approval="approved" AND published; the
        # log's approval field doesn't distinguish, so close as the safe status —
        # the paid email upgrades it later.
        if invoice_id:
            update_workflow_record_status(
                invoice_id, "completed_draft_saved",
                summary="Verified by Square email: invoice creation confirmed.")
        return "created_confirmed"

    mark_invoice_verification(thread_id, status="paid_confirmed",
                              stamp_field="verified_paid_at")
    if invoice_id:
        update_workflow_record_status(
            invoice_id, "completed_paid",
            summary="Verified by Square email: client paid the invoice.")
    return "paid_confirmed"


def poll_once(max_results: int = 20) -> dict[str, Any]:
    """Read unprocessed Square notification mail and confirm invoice cases.

    Never raises; returns a summary dict for the watcher log / poll endpoint.
    """
    from db.repository import find_invoice_log_by_number
    from services.gmail_service import apply_message_labels, list_emails

    try:
        batch = list_emails(query=_QUERY, max_results=max_results)
    except Exception as exc:
        log.warning("[inv:validate] Gmail unavailable: %s", exc)
        return {"ok": False, "error": str(exc), "processed": []}

    processed: list[dict[str, Any]] = []
    for meta in batch.get("messages", []):
        mid = meta.get("message_id") or ""
        subject = meta.get("subject") or ""
        kind, number = classify_square_subject(subject)
        try:
            if not kind:
                # Not a validation event — mark processed so we never re-read it.
                apply_message_labels(mid, add_labels=[_LABEL_PROCESSED])
                processed.append({"message_id": mid, "subject": subject, "result": "ignored"})
                continue

            row = find_invoice_log_by_number(number)
            if not row:
                # No logged invoice carries this number (e.g. hand-made in the
                # Square dashboard). Label it visible-but-done for human eyes.
                apply_message_labels(mid, add_labels=[_LABEL_UNMATCHED])
                processed.append({"message_id": mid, "subject": subject,
                                  "result": "unmatched", "invoice_number": number})
                log.info("[inv:validate] no invoice log for #%s (%r)", number, subject[:80])
                continue

            outcome = _confirm(row, kind)
            apply_message_labels(mid, add_labels=[_LABEL_PROCESSED])
            processed.append({"message_id": mid, "subject": subject,
                              "result": outcome, "invoice_number": number,
                              "thread_id": row["thread_id"]})
            log.info("[inv:validate] #%s %s → %s (case %s)",
                     number, kind, outcome, row["thread_id"])
        except Exception as exc:
            # Leave the message unlabeled — it will be retried next poll.
            log.warning("[inv:validate] failed on %s (%r): %s", mid, subject[:80], exc)
            processed.append({"message_id": mid, "subject": subject,
                              "result": "error", "error": str(exc)})

    return {"ok": True, "count": len(processed), "processed": processed}
