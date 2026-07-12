"""Hardening tests for the July 2026 lost-booking incident.

A Squarespace booking (Paige Kim, 7/16) was silently dropped because
(a) the live reservations table was missing columns the code writes, so the
    case-creating upsert failed (PGRST204), and
(b) the failed email was labeled processed and then permanently starved out of
    the fetch window by newer already-processed mail.
These tests pin the graceful-degradation and windowing fixes.
"""

from types import SimpleNamespace

import pytest

from db import repository
from db.models import Reservation
from services import tastingroom_mailbox


class _FakeAPIError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _missing_column_error(column):
    return _FakeAPIError(
        f"{{'message': \"Could not find the '{column}' column of 'reservations' "
        "in the schema cache\", 'code': 'PGRST204'}"
    )


class _FakeTable:
    """Upsert accepts only rows whose keys are all in `known_columns`."""

    def __init__(self, known_columns, writes):
        self._known = known_columns
        self._writes = writes
        self._row = None

    def upsert(self, row, **kwargs):
        self._row = row
        return self

    def execute(self):
        for key in self._row:
            if key not in self._known:
                raise _missing_column_error(key)
        self._writes.append(self._row)
        return SimpleNamespace(data=[self._row])


class TestUpsertReservationSchemaDrift:
    def _run(self, monkeypatch, known_columns):
        writes = []
        client = SimpleNamespace(table=lambda name: _FakeTable(known_columns, writes))
        monkeypatch.setattr(repository, "_get_client", lambda: client)
        monkeypatch.setattr(repository, "_alerted_missing_columns", set())
        alerts = []
        monkeypatch.setattr(
            repository, "_alert_schema_drift",
            lambda table, column, context: alerts.append(column),
        )
        return writes, alerts

    def test_missing_columns_are_dropped_and_write_succeeds(self, monkeypatch):
        record = Reservation(reservation_id="TASTING-TEST", client_name="Paige kim")
        full_row = repository._reservation_to_row(record)
        known = set(full_row) - {"calendar_event_id", "calendar_event_url"}
        writes, alerts = self._run(monkeypatch, known)

        repository.upsert_reservation(record)

        assert len(writes) == 1, "the case must be saved despite the drift"
        assert "calendar_event_id" not in writes[0]
        assert "calendar_event_url" not in writes[0]
        assert writes[0]["client_name"] == "Paige kim"
        assert set(alerts) == {"calendar_event_id", "calendar_event_url"}

    def test_non_schema_errors_still_raise(self, monkeypatch):
        writes = []

        class _Boom:
            def upsert(self, row, **kwargs):
                return self

            def execute(self):
                raise _FakeAPIError("connection refused")

        client = SimpleNamespace(table=lambda name: _Boom())
        monkeypatch.setattr(repository, "_get_client", lambda: client)

        with pytest.raises(Exception, match="connection refused"):
            repository.upsert_reservation(Reservation(reservation_id="TASTING-TEST"))
        assert not writes

    def test_missing_column_parser_ignores_other_tables(self):
        exc = _FakeAPIError(
            "Could not find the 'foo' column of 'invoice_logs' in the schema cache"
        )
        assert repository._missing_column(exc, "reservations") is None
        assert repository._missing_column(exc, "invoice_logs") == "foo"


class TestCandidateWindowStarvation:
    def _msg(self, mid, labels, subject="Form Submission - Wine tasting Booking"):
        return {
            "message_id": mid,
            "subject": subject,
            "from": "Squarespace <form-submission@squarespace.info>",
            "to": "contact@innovatuswine.com",
            "labels": labels,
        }

    def test_processed_mail_cannot_crowd_out_unprocessed(self, monkeypatch):
        """An old unprocessed form must survive the truncation to max_results
        even when newer already-processed messages outnumber the window."""
        processed = tastingroom_mailbox.GMAIL_TASTING_PROCESSED_LABEL
        newest_first = [
            self._msg(f"done_{i}", ["INNOVATUS", processed]) for i in range(10)
        ] + [self._msg("paige", ["INNOVATUS"])]

        import services.gmail_service as gmail_service

        monkeypatch.setattr(
            gmail_service, "list_emails_multi",
            lambda **kwargs: {"messages": newest_first[: kwargs["max_results"]]},
        )
        monkeypatch.setattr(
            tastingroom_mailbox, "_active_reservation_thread_ids", lambda **kwargs: []
        )

        candidates = tastingroom_mailbox.list_candidate_messages(max_results=10)
        ids = [msg["message_id"] for msg in candidates]
        assert ids == ["paige"]

    def test_query_excludes_processed_label(self, monkeypatch):
        seen_kwargs = {}

        import services.gmail_service as gmail_service

        def fake_list(**kwargs):
            seen_kwargs.update(kwargs)
            return {"messages": []}

        monkeypatch.setattr(gmail_service, "list_emails_multi", fake_list)
        monkeypatch.setattr(
            tastingroom_mailbox, "_active_reservation_thread_ids", lambda **kwargs: []
        )

        tastingroom_mailbox.list_candidate_messages(max_results=10)
        assert "-label:tasting-room-processed" in seen_kwargs["query"]
        assert seen_kwargs["max_results"] >= 30

    def test_processed_thread_messages_are_filtered(self, monkeypatch):
        processed = tastingroom_mailbox.GMAIL_TASTING_PROCESSED_LABEL

        import services.gmail_service as gmail_service

        monkeypatch.setattr(
            gmail_service, "list_emails_multi", lambda **kwargs: {"messages": []}
        )
        monkeypatch.setattr(
            tastingroom_mailbox, "_active_reservation_thread_ids",
            lambda **kwargs: ["aabbccddeeff11"],
        )
        monkeypatch.setattr(
            gmail_service, "list_thread_messages",
            lambda thread_id, **kwargs: [
                self._msg("old_reply", ["INBOX", processed]),
                self._msg("new_reply", ["INBOX"]),
            ],
        )

        ids = [m["message_id"] for m in tastingroom_mailbox.list_candidate_messages()]
        assert ids == ["new_reply"]


class TestIntakeFailureAlerts:
    def test_intake_error_posts_chat_alert(self, monkeypatch):
        from vertex_agent import intake

        def boom(**kwargs):
            raise RuntimeError("Could not find the 'calendar_event_id' column")

        alerts = []
        monkeypatch.setattr(intake, "intake_email", boom)
        monkeypatch.setattr(intake, "_record_intake_failure", lambda *a, **k: None)
        monkeypatch.setattr(intake, "_notify_alert", lambda text: alerts.append(text))

        result = intake.coordinate_email(
            subject="Form Submission - Wine tasting Booking",
            sender="Squarespace <form-submission@squarespace.info>",
            body="Name: Paige kim",
            gmail_message_id="m1",
        )

        assert result["status"] == "intake_error"
        assert alerts and "FAILED" in alerts[0]
        assert "Form Submission - Wine tasting Booking" in alerts[0]
