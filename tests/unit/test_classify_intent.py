"""Unit tests for the deterministic keyword path in classify_intent.

The keyword path never calls the LLM, so no mock_anthropic fixture needed.
The LLM path is only reached for longer messages (>80 chars) with no invoice keywords.
"""

import pytest

from agents.invoice_graph import classify_intent


def _run(text: str) -> str:
    """Helper: run classify_intent with a minimal state dict."""
    result = classify_intent({"raw_message": text})
    return result.get("intent", "")


class TestClassifyIntentKeywordPath:
    """These cases hit the fast-path keyword check — no LLM involved."""

    def test_invoice_keyword(self):
        assert _run("Invoice Oak Barrel for 3 cases Cab Sauv") == "invoice_request"

    def test_bottle_keyword(self):
        assert _run("6 bottles pinot noir for Sofia") == "invoice_request"

    def test_cases_of_keyword(self):
        assert _run("Please send 2 cases of Cab to the restaurant") == "invoice_request"

    def test_net_30_keyword(self):
        assert _run("bill them NET 30 for the order") == "invoice_request"

    def test_wine_keyword(self):
        assert _run("wine order for downtown bistro") == "invoice_request"

    def test_short_greeting_is_chat(self):
        assert _run("hi") == "chat"

    def test_short_question_is_chat(self):
        assert _run("what are your prices?") == "chat"

    def test_short_unknown_is_chat(self):
        assert _run("hello there") == "chat"

    def test_invoice_keyword_case_insensitive(self):
        """Keyword check uses .lower() so INVOICE should still match."""
        assert _run("INVOICE the restaurant for 12 BTL") == "invoice_request"

    def test_btl_keyword(self):
        assert _run("12 btl for the restaurant please") == "invoice_request"
