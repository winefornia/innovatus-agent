"""Thread-aware LLM extraction for the tasting-room pipeline.

Locks in three guarantees:
  1. llm_extract_email sends the ENTIRE email to the LLM (the old 6000-char cut
     silently dropped form fields / the client's identity from long bodies);
  2. the LLM gets the case's earlier thread messages as context, oldest first,
     with the current message excluded;
  3. intake_email wires the two together per case, and everything degrades
     gracefully (no thread / DB down → extraction still runs without context).
"""
import json
from unittest.mock import MagicMock

import services.tastingroom_service as trs
import vertex_agent.intake as intake


def _capture_llm(mocker, reply: dict):
    """Patch ChatAnthropic so we can inspect exactly what the LLM was sent."""
    response = MagicMock()
    response.content = json.dumps(reply)
    fake_llm = MagicMock()
    fake_llm.invoke.return_value = response
    mocker.patch("langchain_anthropic.ChatAnthropic", return_value=fake_llm)
    return fake_llm


# ── 1. the entire mail reaches the LLM ───────────────────────────────────────

def test_llm_extract_sends_entire_body_not_first_6000_chars(mocker):
    fake_llm = _capture_llm(mocker, {"client_name": "Mira Park"})
    # Put the customer's identity BEYOND the old 6000-char truncation point.
    body = ("x" * 7000) + "\nEmail Address: mira@example.com\nPhone: 707-555-0100"

    out = trs.llm_extract_email("Form Submission", "form-submission@squarespace.info",
                                body, "squarespace_form")

    assert out == {"client_name": "Mira Park"}
    human = fake_llm.invoke.call_args[0][0][1].content
    assert "mira@example.com" in human            # would have been cut at 6000
    assert "707-555-0100" in human


def test_llm_extract_prompt_asks_for_full_customer_identity(mocker):
    fake_llm = _capture_llm(mocker, {})
    trs.llm_extract_email("s", "a@b.com", "body", "unclassified")
    system = fake_llm.invoke.call_args[0][0][0].content
    assert "client_name" in system and "client_email" in system and "phone" in system
    assert "ENTIRE email" in system


# ── 2. thread context: built per case, oldest first, current message excluded ─

def _thread_rows():
    return [
        {"gmail_message_id": "m1", "from_email": "mira@example.com",
         "subject": "Tasting request", "body": "Can we come July 10?",
         "created_at": "2026-07-01T10:00:00"},
        {"gmail_message_id": "m2", "from_email": "audrey@innovatuswine.com",
         "subject": "Re: Tasting request", "body": "We have 2pm or 4pm open.",
         "created_at": "2026-07-02T10:00:00"},
        {"gmail_message_id": "m3", "from_email": "mira@example.com",
         "subject": "Re: Tasting request", "body": "Yes, 2pm works!",
         "created_at": "2026-07-03T10:00:00"},
    ]


def test_build_thread_context_orders_and_excludes_current_message(mocker):
    mocker.patch("db.repository.list_raw_email_events_by_thread",
                 return_value=_thread_rows())

    ctx = trs.build_thread_context("t1", exclude_message_id="m3")

    assert "Can we come July 10?" in ctx
    assert "2pm or 4pm open" in ctx
    assert "Yes, 2pm works!" not in ctx           # the message being processed
    assert ctx.index("Can we come") < ctx.index("2pm or 4pm")   # oldest first


def test_build_thread_context_is_best_effort(mocker):
    assert trs.build_thread_context("") == ""     # no thread id → no context
    mocker.patch("db.repository.list_raw_email_events_by_thread",
                 side_effect=RuntimeError("db down"))
    assert trs.build_thread_context("t1") == ""   # DB failure never raises


def test_llm_extract_receives_thread_context_before_newest_email(mocker):
    fake_llm = _capture_llm(mocker, {"requested_time": "14:00:00"})

    trs.llm_extract_email("Re: Tasting request", "mira@example.com",
                          "Yes, 2pm works!", "client_acceptance",
                          thread_context="From: audrey@innovatuswine.com\nWe have 2pm or 4pm open.")

    human = fake_llm.invoke.call_args[0][0][1].content
    assert "Earlier messages in this thread" in human
    assert "2pm or 4pm open" in human
    assert human.index("2pm or 4pm open") < human.index("Newest email to extract from")


# ── 3. intake wires the case's thread into extraction ────────────────────────

def test_intake_passes_thread_context_for_the_cases_thread(mocker):
    from db.models import Reservation

    res = Reservation(reservation_id="TASTING-20260710-4G-MIRA-PARK",
                      client_name="Mira Park", client_email="mira@example.com")
    mocker.patch("db.repository.insert_raw_email_event", return_value=None)
    mocker.patch.object(trs, "classify_email", return_value="client_acceptance")
    mocker.patch.object(trs, "extract_email_facts", return_value={})
    mocker.patch.object(trs, "build_thread_context", return_value="EARLIER THREAD")
    llm = mocker.patch.object(trs, "llm_extract_email", return_value={})
    mocker.patch.object(trs, "merge_llm_facts", side_effect=lambda f, l, m=None: f)
    mocker.patch.object(trs, "find_or_create_reservation",
                        return_value=(res.reservation_id, {"reservation_id": res.reservation_id}))
    mocker.patch.object(trs, "merge_reservation", return_value=res)
    mocker.patch.object(trs, "build_claims", return_value=[])
    mocker.patch.object(trs, "persist_processed_email", return_value=None)

    intake.intake_email(subject="Re: Tasting request", sender="mira@example.com",
                        body="Yes, 2pm works!", gmail_message_id="m3", gmail_thread_id="t1")

    trs.build_thread_context.assert_called_once_with("t1", exclude_message_id="m3")
    assert llm.call_args.kwargs["thread_context"] == "EARLIER THREAD"


def test_repo_thread_query_sorts_and_limits(mocker):
    import db.repository as repo

    rows = list(reversed(_thread_rows()))         # arrive newest-first from the DB
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = rows
    mocker.patch.object(repo, "_get_client", return_value=fake)

    out = repo.list_raw_email_events_by_thread("t1", limit=2)

    assert [r["gmail_message_id"] for r in out] == ["m2", "m3"]   # newest 2, oldest first
    assert repo.list_raw_email_events_by_thread("") == []
