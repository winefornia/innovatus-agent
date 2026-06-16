"""A price/vintage stated when answering a clarifying question must be captured
on the extracted item, so the flow never re-asks for it (regression: operator
typed "$75" at ask_missing_fields and was asked the same price again later)."""
from agents.invoice_graph import _apply_clarification_facts, _clarification_price_cents


def _items(*items):
    return {"items": [dict(i) for i in items]}


def test_dollar_price_single_item_captured_as_manual_price():
    ext = _items({"product_name": "Viognier"})
    _apply_clarification_facts("$75", ext)
    assert ext["items"][0]["manual_price_cents"] == 7500


def test_bare_number_treated_as_price():
    ext = _items({"product_name": "Viognier"})
    _apply_clarification_facts("75", ext)
    assert ext["items"][0]["manual_price_cents"] == 7500


def test_decimal_and_thousands_separator():
    ext = _items({"product_name": "Cabernet Sauvignon"})
    _apply_clarification_facts("$1,250.00", ext)
    assert ext["items"][0]["manual_price_cents"] == 125000


def test_four_digit_year_is_vintage_not_price():
    ext = _items({"product_name": "Viognier"})
    _apply_clarification_facts("2023", ext)
    assert ext["items"][0]["vintage"] == 2023
    assert "manual_price_cents" not in ext["items"][0]


def test_price_and_vintage_together():
    ext = _items({"product_name": "Viognier"})
    _apply_clarification_facts("Viognier 2023, $75", ext)
    assert ext["items"][0]["vintage"] == 2023
    assert ext["items"][0]["manual_price_cents"] == 7500


def test_multi_item_price_not_auto_assigned():
    # Two unpriced items → ambiguous which the price belongs to; leave for the
    # per-item confirm step. Vintage still fills both (safe to share).
    ext = _items({"product_name": "Viognier"}, {"product_name": "Zinfandel"})
    _apply_clarification_facts("$75", ext)
    assert "manual_price_cents" not in ext["items"][0]
    assert "manual_price_cents" not in ext["items"][1]


def test_existing_price_not_overwritten():
    ext = _items({"product_name": "Viognier", "manual_price_cents": 6000})
    _apply_clarification_facts("$75", ext)
    assert ext["items"][0]["manual_price_cents"] == 6000


def test_non_numeric_clarification_no_change():
    ext = _items({"product_name": "Viognier"})
    _apply_clarification_facts("find the retail price", ext)
    assert "manual_price_cents" not in ext["items"][0]
    assert "vintage" not in ext["items"][0]


def test_price_helper_excludes_year():
    assert _clarification_price_cents("2023", exclude=None) is None
    assert _clarification_price_cents("Viognier 2023 $75", exclude="2023") == 7500
