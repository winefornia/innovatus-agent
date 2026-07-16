"""Regression tests for the website-form intake bug.

A real Squarespace form arrived with an HTML-only body; _decode_body returned the
empty text/plain part, so the body was '', the classifier said 'unclassified', and
the request dead-ended (no reservation, no Google Chat card). These lock in the fix.
"""
import base64

from services.gmail_service import _decode_body
from services.tastingroom_service import classify_email


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def test_squarespace_form_classified_by_sender_even_with_empty_body():
    # The exact failing signature: Squarespace notifier + form subject + no body.
    assert classify_email(
        "Form Submission - Wine tasting Booking",
        "Squarespace <form-submission@squarespace.info>",
        "",
    ) == "squarespace_form"


def test_decode_body_falls_back_to_html_when_plain_is_empty():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("   ")}},   # empty plain
            {"mimeType": "text/html",
             "body": {"data": _b64("<b>Date Requested:</b> 2026-07-04<br>Guests: 4")}},
        ],
    }
    out = _decode_body(payload)
    assert "Date Requested: 2026-07-04" in out
    assert "Guests: 4" in out


def test_decode_body_prefers_nonempty_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain wins")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
        ],
    }
    assert _decode_body(payload) == "plain wins"


# ── form parsing captures the client name across every layout ─────────────────
# Regression for the "(no name yet)" cases: HTML forms collapsed to one line
# (every <br> → space) or used table cells, so the parser read only the first
# field and lost the name/email.

from services.tastingroom_service import parse_squarespace_form


def test_parse_form_collapsed_one_line():
    f = parse_squarespace_form(
        "Name: Jane Doe Email: jane@example.com Phone: 555-1212 "
        "Date Requested: 2026-07-04 Number of Guests: 4"
    )
    assert f["client_name"] == "Jane Doe"
    assert f["client_email"] == "jane@example.com"
    assert f["guest_count"] == 4


def test_parse_form_per_line_label_variants():
    f = parse_squarespace_form(
        "Full Name: Mira Park\nEmail Address: mira@example.com\n"
        "# of guests: 2\nDate Requested: 06/07/2026"
    )
    assert f["client_name"] == "Mira Park"
    assert f["client_email"] == "mira@example.com"
    assert f["guest_count"] == 2


def test_parse_form_html_table_cells():
    html_body = (
        "<table><tr><td>Name</td><td>Audrey Park</td></tr>"
        "<tr><td>Email</td><td>audrey@x.com</td></tr>"
        "<tr><td>Number of Guests</td><td>6</td></tr></table>"
    )
    payload = {"mimeType": "text/html", "body": {"data": _b64(html_body)}}
    f = parse_squarespace_form(_decode_body(payload))
    assert f["client_name"] == "Audrey Park"
    assert f["client_email"] == "audrey@x.com"
    assert f["guest_count"] == 6


def test_parse_form_html_br_with_bold_labels():
    html_body = (
        "<b>Name:</b> Lily Chen<br><b>Email:</b> lily@x.com<br>"
        "<b>Number of Guests:</b> 3<br><b>Date Requested:</b> 2026-09-10"
    )
    payload = {"mimeType": "text/html", "body": {"data": _b64(html_body)}}
    f = parse_squarespace_form(_decode_body(payload))
    assert f["client_name"] == "Lily Chen"
    assert f["client_email"] == "lily@x.com"
    assert f["guest_count"] == 3


# ── intake gate: only a website form with a client identity opens a new case ──

def _patch_intake(mocker, message_type, facts):
    import services.tastingroom_service as trs
    mocker.patch.object(trs, "classify_email", return_value=message_type)
    mocker.patch.object(trs, "extract_email_facts", return_value=facts)
    mocker.patch.object(trs, "llm_extract_email", return_value={})
    mocker.patch.object(trs, "build_thread_context", return_value="")
    mocker.patch.object(trs, "merge_llm_facts", side_effect=lambda f, l, m=None: f)
    mocker.patch.object(trs, "find_or_create_reservation", return_value=("TASTING-X", None))  # new
    mocker.patch("db.repository.insert_raw_email_event", return_value=None)
    return mocker.patch("db.repository.insert_unresolved_event", return_value=None)


def test_square_report_with_a_date_is_quarantined_not_a_case(mocker):
    # The exact past failure: a Square report parsed a date and became a reservation.
    import vertex_agent.intake as intake
    unresolved = _patch_intake(mocker, "invoice_payment_message", {"requested_date": "2026-06-12"})
    res = intake.intake_email(
        subject="INNOVATUS—Daily Sales Summary for June 12, 2026",
        sender="Square Reports <noreply@messaging.squareup.com>",
        body="Your sales summary", gmail_message_id="m1", gmail_thread_id="t1",
    )
    assert res["unresolved"] is True
    assert res["reservation_id"] is None
    unresolved.assert_called_once()


def test_form_without_identity_is_quarantined(mocker):
    import vertex_agent.intake as intake
    unresolved = _patch_intake(mocker, "squarespace_form", {"requested_date": "2026-07-04"})
    res = intake.intake_email(
        subject="Form Submission - Wine tasting Booking",
        sender="Squarespace <form-submission@squarespace.info>",
        body="Date Requested: 2026-07-04", gmail_message_id="m2", gmail_thread_id="t2",
    )
    assert res["unresolved"] is True
    unresolved.assert_called_once()
