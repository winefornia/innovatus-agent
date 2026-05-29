"""Product catalog and deterministic pricing service.

No LLM in this path — prices are always computed from catalog + tier rules.
Primary source: Supabase products + pricing_tiers tables.
Fallback: app/data/product_catalog.json + pricing_tiers.json.
"""
import json
import unicodedata
from typing import Optional

from app.config import DATA_DIR, SUPABASE_URL, SUPABASE_SERVICE_KEY
from app.schemas import LineItem

_CATALOG_FILE = DATA_DIR / "product_catalog.json"
_TIERS_FILE = DATA_DIR / "pricing_tiers.json"

_sb_client = None


def _get_supabase():
    global _sb_client
    if _sb_client:
        return _sb_client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    try:
        from supabase import create_client
        _sb_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        return _sb_client
    except Exception:
        return None

SHIPPING_WAIVER_THRESHOLD_CENTS = 150_000  # $1,500

# Wine name aliases for natural language matching
_ALIASES: dict[str, str] = {
    "cab": "cabernet sauvignon",
    "cab sauv": "cabernet sauvignon",
    "cab franc": "cabernet franc",
    "sauv blanc": "sauvignon blanc",
    "sb": "sauvignon blanc",
    "chard": "chardonnay",
    "zin": "zinfandel",
    "brut": "brut sparkling",
    "brut rose": "sparkling rose",
    "pn": "pinot noir",
    "pinot": "pinot noir",
    "cuvee": "cuvee red",
}


def _load_catalog() -> list[dict]:
    try:
        with open(_CATALOG_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _load_tiers() -> list[dict]:
    try:
        with open(_TIERS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _load_catalog_from_supabase() -> list[dict]:
    sb = _get_supabase()
    if not sb:
        return []
    try:
        result = sb.table("products").select("*").execute()
        return result.data or []
    except Exception:
        return []


def _load_tiers_from_supabase() -> list[dict]:
    sb = _get_supabase()
    if not sb:
        return []
    try:
        result = sb.table("pricing_tiers").select("*").execute()
        return result.data or []
    except Exception:
        return []


def get_tier_by_name(tier_name: str) -> Optional[dict]:
    # Try Supabase first
    for t in _load_tiers_from_supabase():
        if t["name"].lower() == tier_name.lower():
            return t
    # Fallback to JSON
    for t in _load_tiers():
        if t["name"].lower() == tier_name.lower():
            return t
    return None


def _ascii(s: str) -> str:
    """Strip accents and lowercase for accent-insensitive matching (rosé → rose)."""
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode("ascii")


def _search_catalog(
    catalog: list[dict],
    resolved: str,
    vintage: Optional[int],
    size: Optional[str],
) -> Optional[dict]:
    """Search a catalog list for a product. Shared by Supabase and JSON paths."""
    r_ascii = _ascii(resolved)
    matches = []
    for p in catalog:
        p_name = _ascii(p["name"])
        if r_ascii in p_name or p_name in r_ascii:
            if vintage and p.get("vintage") != vintage:
                continue
            if size and p.get("size", "").lower() != size.lower():
                continue
            matches.append(p)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        for m in matches:
            if m["name"].lower().strip() == resolved:
                return m
        return matches[0]
    return None


def find_product(
    name: str,
    vintage: Optional[int] = None,
    size: Optional[str] = None,
) -> Optional[dict]:
    """Find a product by name (with alias resolution), vintage, and size.

    Tries Supabase first, falls back to product_catalog.json.
    """
    query = _ascii(name.strip())
    resolved = _ALIASES.get(query, query)

    # Try Supabase
    sb_catalog = _load_catalog_from_supabase()
    if sb_catalog:
        result = _search_catalog(sb_catalog, resolved, vintage, size)
        if result:
            return result

    # Fallback to JSON
    return _search_catalog(_load_catalog(), resolved, vintage, size)


def search_product_catalog(query: str, vintage: Optional[int] = None) -> dict:
    """Search the product catalog by name or alias, optionally filtered by vintage."""
    exact = find_product(query, vintage)
    if exact:
        return {"matches": [exact], "count": 1}

    catalog = _load_catalog()
    q = _ascii(query.strip())
    matches = [
        p for p in catalog
        if q in _ascii(p["name"]) and (vintage is None or p.get("vintage") == vintage)
    ]

    if matches:
        return {"matches": matches, "count": len(matches)}

    return {
        "matches": [],
        "count": 0,
        "message": f"No products found for '{query}' {vintage or ''}.",
    }


def calculate_invoice_prices(
    tier_name: str,
    items: list[dict],
) -> dict:
    """Compute prices for a list of items given a pricing tier.

    items: list of dicts with product_name, vintage (optional), size (optional),
           quantity, unit_type ('bottle' or 'case')

    Returns:
        line_items: list of LineItem dicts
        subtotal_cents: retail subtotal before discount
        discount_cents: total discount
        total_before_tax_cents: total after discount
        shipping_cents: 0 if waived, None if applicable (add $15 separately)
        warnings: non-blocking notes
        blocks: items that could not be priced (abort if non-empty)
    """
    tier = get_tier_by_name(tier_name)
    if tier is None:
        return {
            "line_items": [],
            "subtotal_cents": 0,
            "discount_cents": 0,
            "total_before_tax_cents": 0,
            "shipping_cents": None,
            "warnings": [],
            "blocks": [f"Unknown pricing tier: {tier_name}"],
        }

    multiplier = tier["msrp_multiplier"]
    discount_pct = tier["discount_percent"]
    line_items: list[dict] = []
    warnings: list[str] = []
    blocks: list[str] = []

    if tier.get("requires_human_confirmation"):
        warnings.append(f"Tier '{tier_name}' requires human confirmation for discount level.")

    for item in items:
        product = find_product(
            item.get("product_name", ""),
            item.get("vintage"),
            item.get("size"),
        )

        if product is None:
            blocks.append(f"Product not found: {item.get('product_name', '?')} {item.get('vintage', '')}")
            continue

        if product.get("variable_pricing") and product.get("msrp_bottle_cents") is None:
            blocks.append(
                f"Variable pricing, no MSRP: {product['name']} {product.get('vintage')}. "
                "Needs price confirmation."
            )
            continue

        if tier_name in product.get("tier_unavailable", []):
            blocks.append(
                f"{product['name']} {product.get('vintage')} is not available at {tier_name} tier."
            )
            continue

        msrp_cents = product["msrp_bottle_cents"]
        quantity = item.get("quantity", 1)
        unit_type = item.get("unit_type", "bottle")
        bottles_per_case = product.get("bottles_per_case", 12)

        if unit_type == "case":
            bottle_count = quantity * bottles_per_case
        else:
            bottle_count = quantity

        # round() avoids int(19500 * 0.7) = 13649 floating-point truncation bug
        final_unit_price = round(msrp_cents * multiplier)
        line_total = round(final_unit_price * bottle_count)

        li = LineItem(
            product_name=product["name"],
            vintage=product.get("vintage"),
            size=product.get("size", "750ml"),
            quantity=quantity,
            unit_type=unit_type,
            base_unit_price_cents=msrp_cents,
            discount_percent=discount_pct,
            final_unit_price_cents=final_unit_price,
            line_total_cents=line_total,
            bottles_per_case=bottles_per_case,
        )
        line_items.append(li.model_dump())

    subtotal_cents = sum(
        li["base_unit_price_cents"] * (li["quantity"] * (li["bottles_per_case"] if li["unit_type"] == "case" else 1))
        for li in line_items
    )
    total_after_discount = sum(li["line_total_cents"] for li in line_items)
    discount_cents = subtotal_cents - total_after_discount

    shipping_cents = None
    if total_after_discount >= SHIPPING_WAIVER_THRESHOLD_CENTS:
        shipping_cents = 0
        warnings.append("Shipping waived (order >= $1,500).")

    return {
        "line_items": line_items,
        "subtotal_cents": int(subtotal_cents),
        "discount_cents": int(discount_cents),
        "total_before_tax_cents": int(total_after_discount),
        "shipping_cents": shipping_cents,
        "warnings": warnings,
        "blocks": blocks,
    }
