"""Tests for get_request_email — the chat tool that surfaces the source
email(s) behind a tasting-room case with Gmail deep links as evidence."""

import pytest

import vertex_agent.tools as tools


RID = "TASTING-20260710-4G-MIRA"


def _raw(mid, thread="a1b2c3d4e5f60718", subject="Tasting request", body="Hi, we'd love to visit", ingested="2026-07-10T09:00:00+00:00"):
    return {
        "gmail_message_id": mid,
        "gmail_thread_id": thread,
        "subject": subject,
        "from_email": "mira@example.com",
        "body": body,
        "ingested_at": ingested,
    }


@pytest.fixture
def _case(monkeypatch):
    monkeypatch.setattr(tools, "get_reservation", lambda rid: {
        "reservation_id": rid,
        "client_name": "Mira Park",
        "gmail_thread_ids": ["a1b2c3d4e5f60718", "manual-entry"],
    })
    monkeypatch.setenv("GOOGLE_DELEGATED_USER_EMAIL", "contact@innovatuswine.com")


def test_links_and_evidence_oldest_first(monkeypatch, _case):
    monkeypatch.setattr(tools, "list_raw_email_events_for_case",
                        lambda rid: [_raw("msg2", subject="Re: Tasting request",
                                          ingested="2026-07-11T09:00:00+00:00")])
    monkeypatch.setattr(tools, "list_raw_email_events_by_thread",
                        lambda tid: [_raw("msg1"), _raw("msg2", ingested="2026-07-11T09:00:00+00:00")])

    out = tools.get_request_email(RID)

    assert [e["role"] for e in out["emails"]] == ["original request", "follow-up"]
    first = out["emails"][0]
    assert first["subject"] == "Tasting request"
    assert first["from"] == "mira@example.com"
    assert first["received"] == "2026-07-10T09:00:00+00:00"
    assert "we'd love to visit" in first["excerpt"]
    assert first["gmail_link"] == (
        "https://mail.google.com/mail/?authuser=contact@innovatuswine.com#all/msg1")
    # msg2 came back from both sources but appears once
    assert [e["gmail_link"].rsplit("/", 1)[-1] for e in out["emails"]] == ["msg1", "msg2"]
    assert out["mailbox"] == "contact@innovatuswine.com"


def test_synthetic_thread_ids_are_skipped(monkeypatch, _case):
    monkeypatch.setattr(tools, "list_raw_email_events_for_case", lambda rid: [])
    calls = []
    monkeypatch.setattr(tools, "list_raw_email_events_by_thread",
                        lambda tid: calls.append(tid) or [])

    tools.get_request_email(RID)

    assert calls == ["a1b2c3d4e5f60718"]  # "manual-entry" never queried


def test_no_stored_mail_returns_note(monkeypatch, _case):
    monkeypatch.setattr(tools, "list_raw_email_events_for_case", lambda rid: [])
    monkeypatch.setattr(tools, "list_raw_email_events_by_thread", lambda tid: [])

    out = tools.get_request_email(RID)

    assert out["emails"] == []
    assert "No stored source email" in out["note"]


def test_unknown_reservation(monkeypatch):
    monkeypatch.setattr(tools, "get_reservation", lambda rid: None)
    out = tools.get_request_email("TASTING-NOPE")
    assert "error" in out


def test_link_without_delegated_mailbox(monkeypatch, _case):
    monkeypatch.delenv("GOOGLE_DELEGATED_USER_EMAIL")
    monkeypatch.setattr(tools, "list_raw_email_events_for_case", lambda rid: [_raw("msg1")])
    monkeypatch.setattr(tools, "list_raw_email_events_by_thread", lambda tid: [])

    out = tools.get_request_email(RID)

    assert out["emails"][0]["gmail_link"] == "https://mail.google.com/mail/#all/msg1"
    assert out["mailbox"] == "the winery mailbox"
