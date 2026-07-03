"""Approval formatting and audit logging."""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import DATA_DIR

APPROVAL_LOG = DATA_DIR / "approval_log.json"


def _load_approvals() -> list[dict]:
    try:
        if APPROVAL_LOG.exists():
            with open(APPROVAL_LOG) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_approvals(approvals: list[dict]):
    try:
        APPROVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(APPROVAL_LOG, "w") as f:
            json.dump(approvals, f, indent=2)
    except Exception:
        pass  # ephemeral FS on Fly — Supabase is the source of truth


def format_approval_request(
    customer_name: str,
    customer_company: str | None,
    tier_name: str,
    line_items: list[dict],
    subtotal_cents: int,
    discount_cents: int,
    total_before_tax_cents: int,
    shipping_cents: int | None,
    warnings: list[str],
    missing_fields: list[str],
) -> str:
    """Format a priced invoice draft into a human-readable approval message."""
    lines = [
        "INVOICE DRAFT READY FOR APPROVAL",
        "=" * 40,
        f"Customer: {customer_name}" + (f" / {customer_company}" if customer_company and customer_company != customer_name else ""),
        f"Tier: {tier_name}",
        "",
        "Items:",
    ]

    for li in line_items:
        qty = int(li['quantity'])
        qty_str = f"{qty} {li['unit_type']}{'s' if qty > 1 else ''}"
        lines.append(
            f"  - {qty_str} {li['product_name']} {li.get('vintage') or ''} "
            f"@ ${li['final_unit_price_cents'] / 100:.2f}/bottle "
            f"= ${li['line_total_cents'] / 100:.2f}"
        )

    shipping_cents = shipping_cents or 0
    final_total_cents = total_before_tax_cents + shipping_cents

    lines.extend([
        "",
        f"Subtotal (retail): ${subtotal_cents / 100:.2f}",
        f"Discount: -${discount_cents / 100:.2f}",
        f"Wine total: ${total_before_tax_cents / 100:.2f}",
    ])

    if shipping_cents == 0:
        lines.append("Shipping: Waived")
    else:
        lines.append(f"Shipping: ${shipping_cents / 100:.2f}")
    lines.append(f"Total before tax: ${final_total_cents / 100:.2f}")

    if warnings:
        lines.append("\nWarnings:")
        for w in warnings:
            lines.append(f"  - {w}")

    if missing_fields:
        lines.append("\nNeeds confirmation:")
        for field in missing_fields:
            lines.append(f"  - {field}")

    lines.extend([
        "",
        "Reply: APPROVE | EDIT | REJECT",
    ])

    return "\n".join(lines)


def log_approval_event(
    draft_summary: str,
    action: str,
    approver: str = "pending",
    notes: str | None = None,
) -> dict:
    """Log an approval event to app/data/approval_log.json."""
    approvals = _load_approvals()
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "draft_summary": draft_summary[:200],
        "action": action,
        "approver": approver,
        "notes": notes,
    }
    approvals.append(event)
    _save_approvals(approvals)
    return {"status": "logged", "event": event}
