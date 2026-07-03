"""Verify tasting-room Square invoices against live Square.

This is a read-only audit. It proves whether reservation rows that claim a
Square invoice can be fetched back from Square and still match the expected
order/customer/title/public URL. It can also scan recent Square invoices for
the tasting-room title so older/manual invoices are visible.

Usage:
    python3 scripts/verify_tastingroom_square_invoices.py --limit 50
    python3 scripts/verify_tastingroom_square_invoices.py --reservation-id TASTING-...
    python3 scripts/verify_tastingroom_square_invoices.py --scan-square --limit 100
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.config  # noqa: F401 - load .env


def _reservation_rows(reservation_id: str | None, limit: int) -> list[dict]:
    from db.repository import get_reservation, list_recent_reservations

    if reservation_id:
        row = get_reservation(reservation_id)
        return [row] if row else []
    return [
        r for r in list_recent_reservations(limit=limit)
        if r.get("square_invoice_id")
    ]


def _verify_reservation(row: dict) -> dict:
    from services.square_service import verify_invoice

    invoice_id = row.get("square_invoice_id") or ""
    if not invoice_id:
        return {
            "reservation_id": row.get("reservation_id"),
            "ok": False,
            "error": "reservation has no square_invoice_id",
        }
    result = verify_invoice(
        invoice_id,
        expected_order_id=row.get("square_order_id") or "",
        expected_customer_id=row.get("square_customer_id") or "",
        title_contains="Innovatus tasting reservation",
        require_public_url=True,
    )
    return {
        "reservation_id": row.get("reservation_id"),
        "client_name": row.get("client_name"),
        "stored_invoice_id": invoice_id,
        "stored_url": row.get("square_invoice_url"),
        "ok": bool(result.get("ok")),
        "square": result,
    }


def _scan_square(limit: int) -> list[dict]:
    from services import square_service

    client = square_service._get_client()
    loc = square_service._active_location()
    if not client or not loc:
        return [{"ok": False, "error": "Square is not configured"}]
    out = []
    try:
        for inv in client.invoices.list(location_id=loc, limit=min(limit, 200)):
            title = getattr(inv, "title", "") or ""
            desc = getattr(inv, "description", "") or ""
            haystack = f"{title}\n{desc}".lower()
            if "innovatus tasting reservation" not in haystack and "tasting reservation" not in haystack:
                continue
            recipient = getattr(inv, "primary_recipient", None)
            out.append({
                "invoice_id": getattr(inv, "id", None),
                "invoice_number": getattr(inv, "invoice_number", None),
                "order_id": getattr(inv, "order_id", None),
                "customer_id": getattr(recipient, "customer_id", None) if recipient else None,
                "title": title,
                "status": getattr(inv, "status", None),
                "public_url": getattr(inv, "public_url", None),
                "created_at": getattr(inv, "created_at", None),
                "updated_at": getattr(inv, "updated_at", None),
            })
            if len(out) >= limit:
                break
    except Exception as exc:
        return [{"ok": False, "error": str(exc)}]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reservation-id", help="Verify one reservation by id.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--scan-square", action="store_true",
                        help="Also list recent live Square invoices that look like tasting-room invoices.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = _reservation_rows(args.reservation_id, args.limit)
    verified = [_verify_reservation(r) for r in rows]
    square_scan = _scan_square(args.limit) if args.scan_square else []
    payload = {
        "reservation_count": len(rows),
        "verified_ok": sum(1 for r in verified if r.get("ok")),
        "verified_failed": sum(1 for r in verified if not r.get("ok")),
        "reservations": verified,
        "square_tasting_invoice_scan": square_scan,
    }

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"Reservations checked: {payload['reservation_count']}")
        print(f"Verified ok: {payload['verified_ok']}")
        print(f"Failed: {payload['verified_failed']}")
        for item in verified:
            status = "OK" if item.get("ok") else "FAIL"
            sq = item.get("square") or {}
            print(f"- {status} {item.get('reservation_id')} invoice={item.get('stored_invoice_id')} "
                  f"status={sq.get('status')} url={sq.get('public_url') or item.get('stored_url')}")
            if not item.get("ok"):
                print(f"  error: {sq.get('error') or item.get('error')}")
        if args.scan_square:
            print("\nRecent Square invoices matching tasting-room titles:")
            for inv in square_scan:
                if inv.get("error"):
                    print(f"- FAIL {inv['error']}")
                else:
                    print(f"- {inv.get('invoice_id')} {inv.get('status')} {inv.get('title')} {inv.get('public_url')}")
    return 0 if payload["verified_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
