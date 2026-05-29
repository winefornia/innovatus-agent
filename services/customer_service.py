"""Customer resolution service.

Resolves customer name/phone/email → structured customer record.
Primary source: Supabase customers table.
Fallback: app/data/customers.json (used until migration runs).
"""
import json
import uuid
from typing import Optional

from app.config import DATA_DIR, SUPABASE_URL, SUPABASE_SERVICE_KEY

_CUSTOMERS_FILE = DATA_DIR / "customers.json"
_PRICING_TIERS_FILE = DATA_DIR / "pricing_tiers.json"

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


def _load_customers() -> list[dict]:
    try:
        if not _CUSTOMERS_FILE.exists():
            return []
        with open(_CUSTOMERS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_customers(customers: list[dict]):
    try:
        _CUSTOMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_CUSTOMERS_FILE, "w") as f:
            json.dump(customers, f, indent=2)
    except Exception:
        pass  # read-only fs on cloud — Supabase is the source of truth


# ---------------------------------------------------------------------------
# Supabase-backed lookup
# ---------------------------------------------------------------------------

def _sb_row_to_customer(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "square_customer_id": row.get("square_customer_id"),
        "full_name": row.get("full_name"),
        "company": row.get("company"),
        "email": row.get("email"),
        "phone": row.get("phone"),
        "tier_name": row.get("tier_name"),
        "customer_type": row.get("customer_type"),
        "notes": row.get("notes"),
    }


def _lookup_supabase(
    name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    company: Optional[str],
) -> Optional[dict]:
    """Try to resolve customer from Supabase. Returns match dict or None."""
    sb = _get_supabase()
    if not sb:
        return None

    try:
        # Exact email
        if email:
            r = sb.table("customers").select("*").ilike("email", email.strip()).limit(1).execute()
            if r.data:
                return {"match": "exact_email", "customer": _sb_row_to_customer(r.data[0])}

        # Exact phone (digits only)
        if phone:
            digits = "".join(d for d in phone if d.isdigit())
            r = sb.table("customers").select("*").execute()
            for row in (r.data or []):
                c_digits = "".join(d for d in (row.get("phone") or "") if d.isdigit())
                if c_digits and c_digits == digits:
                    return {"match": "exact_phone", "customer": _sb_row_to_customer(row)}

        # Name + company
        if name and company:
            r = sb.table("customers").select("*") \
                .ilike("full_name", f"%{name.strip()}%") \
                .ilike("company", f"%{company.strip()}%") \
                .limit(1).execute()
            if r.data:
                return {"match": "name_company", "customer": _sb_row_to_customer(r.data[0])}

        # Fuzzy name
        if name:
            r = sb.table("customers").select("*") \
                .ilike("full_name", f"%{name.strip()}%") \
                .limit(1).execute()
            if r.data:
                return {"match": "fuzzy_name", "customer": _sb_row_to_customer(r.data[0]), "confidence": 0.7}

        # Company only
        if company:
            r = sb.table("customers").select("*") \
                .ilike("company", f"%{company.strip()}%") \
                .limit(1).execute()
            if r.data:
                return {"match": "fuzzy_company", "customer": _sb_row_to_customer(r.data[0]), "confidence": 0.6}

    except Exception:
        pass

    return None


def _normalize(s: str | None) -> str:
    return (s or "").lower().strip()


def lookup_customer(
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Search for a customer by name, email, phone, or company.

    Tries Supabase first (post-migration), falls back to customers.json.
    Returns best match with a 'match' key, or {'match': 'none'} if not found.
    """
    # Try Supabase first
    sb_result = _lookup_supabase(name, email, phone, company)
    if sb_result:
        return sb_result

    # Fallback to JSON
    customers = _load_customers()

    # Exact email match
    if email:
        for c in customers:
            if _normalize(c.get("email")) == _normalize(email):
                return {"match": "exact_email", "customer": c}

    # Exact phone match
    if phone:
        phone_digits = "".join(d for d in phone if d.isdigit())
        for c in customers:
            c_digits = "".join(d for d in (c.get("phone") or "") if d.isdigit())
            if c_digits and c_digits == phone_digits:
                return {"match": "exact_phone", "customer": c}

    # Name + company match
    if name and company:
        for c in customers:
            if (
                _normalize(name) in _normalize(c.get("full_name"))
                and _normalize(company) in _normalize(c.get("company"))
            ):
                return {"match": "name_company", "customer": c}

    # Fuzzy name match
    if name:
        name_lower = _normalize(name)
        for c in customers:
            c_name = _normalize(c.get("full_name"))
            if c_name and (name_lower in c_name or c_name in name_lower):
                return {"match": "fuzzy_name", "customer": c, "confidence": 0.7}

    # Company-only match
    if company:
        company_lower = _normalize(company)
        for c in customers:
            c_company = _normalize(c.get("company"))
            if c_company and (company_lower in c_company or c_company in company_lower):
                return {"match": "fuzzy_company", "customer": c, "confidence": 0.6}

    return {
        "match": "none",
        "message": "Customer not found. Needs manual confirmation or new profile creation.",
        "searched": {"name": name, "email": email, "phone": phone, "company": company},
    }


def resolve_customer_tier(
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
) -> dict:
    """Resolve the pricing tier for a customer.

    Returns tier info if known, or a needs_confirmation status.
    """
    result = lookup_customer(name=customer_name, email=customer_email)
    if result["match"] == "none":
        return {
            "status": "needs_confirmation",
            "reason": "Customer not found in database.",
            "action": "Ask Cecil/Audrey to classify this customer into a pricing tier.",
        }

    customer = result["customer"]
    tier_name = customer.get("tier_name")
    if not tier_name:
        return {
            "status": "needs_tier_confirmation",
            "customer": customer,
            "reason": "Customer exists but has no assigned pricing tier.",
            "action": "Ask Cecil/Audrey to assign a tier.",
        }

    if _PRICING_TIERS_FILE.exists():
        with open(_PRICING_TIERS_FILE) as f:
            tiers = json.load(f)
        tier = next((t for t in tiers if t["name"].lower() == tier_name.lower()), None)
        if tier and tier.get("requires_human_confirmation"):
            return {
                "status": "needs_discount_confirmation",
                "customer": customer,
                "tier": tier,
                "reason": f"Tier '{tier_name}' requires human confirmation for discount level.",
            }
        return {"status": "resolved", "customer": customer, "tier": tier}

    return {"status": "resolved", "customer": customer, "tier": {"name": tier_name}}


def create_customer(
    full_name: str,
    company: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    customer_type: Optional[str] = None,
    tier_name: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Create a new customer record and append to customers.json."""
    customers = _load_customers()
    new_customer = {
        "id": str(uuid.uuid4()),
        "full_name": full_name,
        "company": company,
        "email": email,
        "phone": phone,
        "customer_type": customer_type or "unknown",
        "tier_name": tier_name,
        "square_customer_id": None,
        "notes": notes,
        "confidence": 0.0 if not tier_name else 0.8,
    }
    customers.append(new_customer)
    _save_customers(customers)
    return {"status": "created", "customer": new_customer}


