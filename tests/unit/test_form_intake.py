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
