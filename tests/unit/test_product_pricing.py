"""Per-variety tier pricing from the Retail Accounts SKU sheet.

These exercise the JSON-fallback path of services/product_service (no Supabase),
verifying that explicit sheet prices win over the flat tier multiplier and that
the engine falls back to the multiplier when no sheet price exists.
"""
from services.product_service import calculate_invoice_prices


def _line(result, idx=0):
    assert not result["blocks"], result["blocks"]
    assert not result.get("needs_price"), result["needs_price"]
    return result["line_items"][idx]


def test_sheet_price_used_as_is_not_flat_multiplier():
    # Cabernet Sauvignon Wholesale on the sheet is $117/btl. The flat Wholesale
    # multiplier (0.70 x $195) would give $136.50 — the sheet price must win.
    r = calculate_invoice_prices("Wholesale", [
        {"product_name": "Cabernet Sauvignon", "vintage": 2022, "quantity": 1, "unit_type": "bottle"},
    ])
    li = _line(r)
    assert li["final_unit_price_cents"] == 11700
    assert li["base_unit_price_cents"] == 19500
    assert li["discount_percent"] == 40.0  # 117/195 -> 40% off


def test_fob_discount_varies_by_variety():
    # FOB is a different effective discount per wine: Viognier ~45% off,
    # Cabernet Sauvignon ~55% off. A flat multiplier could not express both.
    vio = _line(calculate_invoice_prices("FOB/Export", [
        {"product_name": "Viognier", "vintage": 2025, "quantity": 1},
    ]))
    cab = _line(calculate_invoice_prices("FOB/Export", [
        {"product_name": "Cabernet Sauvignon", "vintage": 2022, "quantity": 1},
    ]))
    assert vio["final_unit_price_cents"] == 4100   # $41
    assert cab["final_unit_price_cents"] == 8800   # $88
    assert vio["discount_percent"] != cab["discount_percent"]


def test_direct_tier_is_full_retail():
    li = _line(calculate_invoice_prices("Direct", [
        {"product_name": "Zinfandel", "vintage": 2021, "quantity": 1},
    ]))
    assert li["final_unit_price_cents"] == 4500
    assert li["discount_percent"] == 0.0


def test_case_quantity_multiplies_by_bottles_per_case():
    li = _line(calculate_invoice_prices("Club Member", [
        {"product_name": "Cabernet Franc", "vintage": 2022, "quantity": 2, "unit_type": "case"},
    ]))
    assert li["final_unit_price_cents"] == 10600          # club price $106/btl
    assert li["line_total_cents"] == 10600 * 24           # 2 cases x 12


def test_tier_without_sheet_column_falls_back_to_multiplier():
    # Corporate has no per-variety column on the sheet → flat 20% off MSRP.
    li = _line(calculate_invoice_prices("Corporate", [
        {"product_name": "Cabernet Franc", "vintage": 2022, "quantity": 1},
    ]))
    assert li["final_unit_price_cents"] == round(12500 * 0.8)  # 10000
    assert li["discount_percent"] == 20


def test_pinot_noir_fob_falls_back_but_wholesale_uses_sheet():
    # Pinot Noir has Club/Wholesale sheet prices but no FOB column.
    wholesale = _line(calculate_invoice_prices("Wholesale", [
        {"product_name": "Pinot Noir", "vintage": 2024, "quantity": 1},
    ]))
    assert wholesale["final_unit_price_cents"] == 6000   # sheet $60

    fob = _line(calculate_invoice_prices("FOB/Export", [
        {"product_name": "Pinot Noir", "vintage": 2024, "quantity": 1},
    ]))
    assert fob["final_unit_price_cents"] == round(7500 * 0.5)  # flat 50% multiplier


def test_lowercase_tier_name_still_resolves_sheet_price():
    li = _line(calculate_invoice_prices("wholesale", [
        {"product_name": "Cabernet Sauvignon", "vintage": 2022, "quantity": 1},
    ]))
    assert li["final_unit_price_cents"] == 11700


def test_stated_price_overrides_sheet_price():
    # An explicit price in the request is authoritative — no tier discount.
    li = _line(calculate_invoice_prices("Wholesale", [
        {"product_name": "Cabernet Sauvignon", "vintage": 2022, "quantity": 1,
         "unit_price": 150, "unit_type": "bottle"},
    ]))
    assert li["final_unit_price_cents"] == 15000
    assert li["discount_percent"] == 0


def test_operator_regular_price_gets_tier_discount():
    # A price the operator typed when asked (regular_unit_price_cents) is the
    # pre-discount base — the tier discount applies. "$75 regular, 15% off" → $63.75.
    li = _line(calculate_invoice_prices("Club Member", [
        {"product_name": "Special One-Off Wine", "quantity": 1,
         "regular_unit_price_cents": 7500},
    ]))
    assert li["base_unit_price_cents"] == 7500
    assert li["final_unit_price_cents"] == 6375   # 7500 * 0.85
    assert li["discount_percent"] == 15


def test_operator_regular_price_unknown_product_not_blocked():
    # Even when the product isn't in the catalog, an operator price prices it
    # (no needs_price, no block) so the flow never stalls or re-asks.
    r = calculate_invoice_prices("Direct", [
        {"product_name": "Mystery Magnum", "quantity": 2, "unit_type": "case",
         "regular_unit_price_cents": 5000},
    ])
    assert not r["blocks"] and not r.get("needs_price")
    li = r["line_items"][0]
    assert li["final_unit_price_cents"] == 5000          # Direct = 0% off
    assert li["line_total_cents"] == 5000 * 24           # 2 cases x 12 bottles
