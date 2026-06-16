"""Apply the Retail Accounts SKU pricing sheet + 9/30/25 inventory to the catalog.

Idempotent, re-runnable. Reads app/data/product_catalog.json, applies:
  1. Per-variety retail MSRP + tier prices (Club / Wholesale / FOB / Ex-cellar)
     from the "WINE INVENTORY & PRICING" sheet (Retail Accounts SKU tab).
  2. Inventory (bottle counts) from the 9/30/25 Warehouse Inv pivot — INNOVATUS
     for the house SKUs, JD/N-A rows for the branded/shiner SKUs.
Then writes the catalog back and prints a reconciliation report.

The pricing engine (services/product_service.py) uses `tier_prices` directly;
inventory_cases is reference-only (carries bottle counts, matching the source).

Usage:
    python scripts/update_pricing_from_sheet.py            # write changes
    python scripts/update_pricing_from_sheet.py --dry-run  # report only
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import DATA_DIR

CATALOG_FILE = DATA_DIR / "product_catalog.json"

# ---------------------------------------------------------------------------
# 1. Sheet pricing — bottle price in cents per variety/size.
#    `tier_prices` holds the discount-channel columns; retail == msrp.
#    Pinot Noir intentionally has no fob/ex_cellar (the sheet leaves them blank,
#    noting variable case-discount logic) → those tiers fall back to multiplier.
# ---------------------------------------------------------------------------
SHEET: dict[tuple[str, str], dict] = {
    ("viognier", "750ml"):              {"retail": 7500,  "club_member": 6400,  "wholesale": 5300,  "fob": 4100, "ex_cellar": 3800},
    ("brut sparkling", "750ml"):        {"retail": 8500,  "club_member": 7200,  "wholesale": 6000,  "fob": 4700, "ex_cellar": 4500},
    ("sparkling rose", "375ml"):        {"retail": 2800,  "club_member": 2400,  "wholesale": 2000,  "fob": 1500, "ex_cellar": 1500},
    ("rose", "750ml"):                  {"retail": 4500,  "club_member": 3800,  "wholesale": 3200,  "fob": 2500, "ex_cellar": 2300},
    ("zinfandel", "750ml"):             {"retail": 4500,  "club_member": 3800,  "wholesale": 3200,  "fob": 2500, "ex_cellar": 2300},
    ("cuvee red", "750ml"):             {"retail": 6500,  "club_member": 5500,  "wholesale": 5200,  "fob": 4600, "ex_cellar": 4500},
    ("cuvee red library", "750ml"):     {"retail": 8500,  "club_member": 7200,  "wholesale": 6800,  "fob": 6000, "ex_cellar": 5500},
    ("cabernet sauvignon", "750ml"):    {"retail": 19500, "club_member": 16600, "wholesale": 11700, "fob": 8800, "ex_cellar": 7800},
    ("cabernet franc", "750ml"):        {"retail": 12500, "club_member": 10600, "wholesale": 8800,  "fob": 6900, "ex_cellar": 6200},
    ("chardonnay", "750ml"):            {"retail": 3800,  "club_member": 3200,  "wholesale": 3000,  "fob": 2100, "ex_cellar": 2000},
    ("pinot noir", "750ml"):            {"retail": 7500,  "club_member": 6400,  "wholesale": 6000},
}

# ---------------------------------------------------------------------------
# 2. Inventory pivot (9/30/25), bottle counts.
#    INNOVATUS = house SKUs (page 2). JD / NA = branded & unlabeled-shiner rows.
# ---------------------------------------------------------------------------
INNOVATUS: dict[tuple[str, int], int] = {
    ("CF", 2018): 15, ("CF", 2021): 26, ("CF", 2022): 42,
    ("CH", 2020): 8,
    ("CS", 2015): 55, ("CS", 2016): 107, ("CS", 2017): 26,
    ("CS", 2018): 44, ("CS", 2019): 19, ("CS", 2022): 102,
    ("CU", 2014): 107, ("CU", 2020): 285,
    ("RS", 2022): 41,
    ("SPK", 2021): 36,
    ("SPK375", 2021): 209,
    ("VI", 2023): 38,
    ("ZN", 2021): 130, ("ZN", 2022): 88,
}
JD: dict[tuple[str, int], int] = {
    ("CF", 2020): 21, ("CF", 2022): 23,
    ("CS", 2018): 37, ("CS", 2019): 291, ("CS", 2020): 143, ("CS", 2021): 428, ("CS", 2022): 285,
    ("SPK", 2022): 35, ("SPK", 2023): 126,
}
NA: dict[tuple[str, int], int] = {  # unlabeled / non-brand rows
    ("VI", 2023): 48,
    ("RS", 2023): 42,
    ("CS", 2021): 125,
    ("SPK", 2015): 79, ("SPK", 2021): 36,
    ("CH", 2018): 29,  # listed under SB brand on the sheet; no house SKU
}
# Variety codes present in the pivot at all. A house SKU of one of these whose
# vintage is absent is genuinely out of stock → set to 0. Varieties NOT here
# (Pinot Noir, Sauvignon Blanc, Red Blend) have no pivot data → left untouched.
PIVOT_VARIETIES = {"CF", "CH", "CS", "CU", "RS", "SPK", "SPK375", "VI", "ZN"}


def variety_key(name: str, vintage):
    """Sheet variety key for a catalog product name, or None if not on the sheet."""
    n = name.lower()
    if "cuvee" in n:
        return "cuvee red library" if (vintage and vintage <= 2016) else "cuvee red"
    if "cabernet sauvignon" in n:
        return None if "horse" in n else "cabernet sauvignon"  # horse label = special edition
    if "cabernet franc" in n:
        return "cabernet franc"
    if "sauvignon blanc" in n:
        return None
    if "chardonnay" in n:
        return "chardonnay"
    if "brut sparkling" in n:
        return "brut sparkling"
    if "sparkling rose" in n:
        return "sparkling rose"
    if "rose" in n:
        return "rose"
    if "zinfandel" in n:
        return "zinfandel"
    if "pinot noir" in n:
        return "pinot noir"
    if "viognier" in n:
        return "viognier"
    return None  # red blend, etc.


def inv_code(name: str):
    n = name.lower()
    if "cabernet franc" in n:
        return "CF"
    if "cabernet sauvignon" in n:
        return "CS"
    if "cuvee" in n:
        return "CU"
    if "chardonnay" in n:
        return "CH"
    if "sparkling rose" in n:
        return "SPK375"
    if "brut sparkling" in n:
        return "SPK"
    if "rose" in n:
        return "RS"
    if "zinfandel" in n:
        return "ZN"
    if "viognier" in n:
        return "VI"
    if "pinot noir" in n:
        return "PN"
    if "sauvignon blanc" in n:
        return "SB"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, do not write")
    args = ap.parse_args()

    catalog = json.loads(CATALOG_FILE.read_text())
    price_changes, inv_changes, warnings, unmatched = [], [], [], []
    claimed: set = set()  # (brand, code, vintage) already assigned an inventory row

    for p in catalog:
        name, vintage, size = p["name"], p.get("vintage"), p.get("size", "750ml")
        label = f"{name} {vintage or ''} {size}".strip()

        # ---- pricing (skip variable-priced shiners; they stay operator-priced) ----
        vk = variety_key(name, vintage)
        if vk and (vk, size) in SHEET and not p.get("variable_pricing"):
            sheet = SHEET[(vk, size)]
            new_msrp = sheet["retail"]
            new_tiers = {k: v for k, v in sheet.items() if k != "retail"}
            if p.get("msrp_bottle_cents") != new_msrp or p.get("tier_prices") != new_tiers:
                price_changes.append(
                    f"{label}: MSRP {p.get('msrp_bottle_cents')}->{new_msrp}, "
                    f"tier_prices {sorted(new_tiers)}"
                )
            p["msrp_bottle_cents"] = new_msrp
            p["msrp_case_cents"] = new_msrp * p.get("bottles_per_case", 12)
            p["tier_prices"] = new_tiers
        elif vk and (vk, size) in SHEET and p.get("variable_pricing"):
            warnings.append(f"{label}: variable-priced — left operator-priced, no sheet price applied")

        # ---- inventory ----
        # Conservative: only overwrite when a pivot row confidently matches this
        # SKU's brand+variety+vintage. Non-matches are LEFT UNCHANGED (not zeroed) —
        # an absent vintage usually means it's tracked under another brand, not that
        # stock is zero — and flagged for manual review.
        code = inv_code(name)
        if code in PIVOT_VARIETIES:
            brand_text = f"{name} {p.get('appellation', '')}".lower()
            if "jd" in brand_text:
                brand, table = "JD", JD
            elif p.get("variable_pricing"):
                brand, table = "NA", NA
            else:
                brand, table = "IN", INNOVATUS
            key = (brand, code, vintage)
            bottles = table.get((code, vintage))
            if bottles is None:
                warnings.append(f"{label}: no {brand} {code} {vintage} row in pivot — inventory left unchanged")
            elif key in claimed:
                warnings.append(
                    f"{label}: {brand} {code} {vintage} ({bottles} btl) already claimed by "
                    f"another SKU — left unchanged; pivot can't split duplicate vintages"
                )
            else:
                claimed.add(key)
                if p.get("inventory_cases") != bottles:
                    inv_changes.append(f"{label}: inventory {p.get('inventory_cases')}->{bottles} ({brand})")
                p["inventory_cases"] = bottles
        else:
            warnings.append(f"{label}: variety not in 9/30/25 pivot — inventory left unchanged")

    # ---- unmatched pivot rows (have stock, no catalog SKU consumed them) ----
    for brand, table in (("IN", INNOVATUS), ("JD", JD), ("NA", NA)):
        for (code, vintage), btl in table.items():
            if (brand, code, vintage) not in claimed:
                unmatched.append(f"{brand} {code} {vintage}: {btl} btl — no catalog SKU")

    def section(title, rows):
        print(f"\n{title} ({len(rows)})")
        for r in rows:
            print(f"  - {r}")

    section("PRICE CHANGES", price_changes)
    section("INVENTORY CHANGES", inv_changes)
    section("WARNINGS / ASSUMPTIONS", warnings)
    section("UNMATCHED PIVOT ROWS (stock with no catalog SKU)", unmatched)

    if args.dry_run:
        print("\n[dry-run] catalog not written.")
        return
    CATALOG_FILE.write_text(json.dumps(catalog, indent=2) + "\n")
    print(f"\nWrote {CATALOG_FILE} ({len(catalog)} products).")


if __name__ == "__main__":
    main()
