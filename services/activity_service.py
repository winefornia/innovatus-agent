"""
Activity Service — formats bot activity for operator review.

Two surfaces:
  - Telegram: render_telegram_invoice_history(), render_telegram_reservation_history()
  - Web:      render_html_activity_page() → self-contained HTML

Shows results only — no internal state, no trace events, no confidence scores.
The goal: Cecil or Lisa can see what the bot did, verify it, and catch exceptions.

Data from existing repository functions — no new queries needed.
"""

from __future__ import annotations

from datetime import datetime
from html import escape


# ---------------------------------------------------------------------------
# Tasting room state → (human label, status class)
# ---------------------------------------------------------------------------

_STATE_LABELS: dict[str, tuple[str, str]] = {
    # status class: ok | warn | error | neutral
    "REQUEST_RECEIVED":             ("Request received",         "neutral"),
    "NEEDS_FACILITY_CHECK":         ("Checking facility",        "neutral"),
    "WAITING_FOR_JOSH":             ("Waiting for Josh",         "warn"),
    "FACILITY_AVAILABLE":           ("Facility available",       "neutral"),
    "NEEDS_INTERNAL_CHECK":         ("Checking availability",    "neutral"),
    "INTERNAL_AVAILABLE":           ("Availability confirmed",   "neutral"),
    "READY_TO_OFFER_CLIENT":        ("Ready to offer slot",      "neutral"),
    "SLOT_OFFERED_TO_CLIENT":       ("Slot offered to client",   "neutral"),
    "CLIENT_ACCEPTED_SLOT":         ("Client accepted",          "ok"),
    "TENTATIVELY_BOOKED":           ("Tentatively booked",       "ok"),
    "INVOICE_SENT":                 ("Invoice sent",             "ok"),
    "PAYMENT_RECEIVED":             ("Payment received",         "ok"),
    "FINAL_CONFIRMED":              ("Confirmed",                "ok"),
    "CLIENT_REQUESTED_ALTERNATIVE": ("Client wants alternatives","warn"),
    "JOSH_UNAVAILABLE":             ("Josh unavailable",         "warn"),
    "INTERNAL_UNAVAILABLE":         ("No internal availability", "warn"),
    "NO_COMMON_SLOT":               ("No common slot found",     "error"),
    "WAITING_FOR_CLIENT_REPLY":     ("Waiting for client reply", "warn"),
    "WAITING_FOR_PAYMENT":          ("Waiting for payment",      "warn"),
    "PAYMENT_OVERDUE":              ("Payment overdue",          "error"),
    "AMBIGUOUS_REPLY":              ("Ambiguous reply",          "warn"),
    "HUMAN_REVIEW_REQUIRED":        ("Needs review",             "error"),
    "CANCELLED_OR_DEFERRED":        ("Cancelled",                "error"),
}

_STATUS_EMOJI = {
    "ok":      "✅",
    "warn":    "⏳",
    "error":   "❌",
    "neutral": "•",
}


# ---------------------------------------------------------------------------
# Shared formatters
# ---------------------------------------------------------------------------

def _fmt_ts(ts_str: str | None) -> str:
    """ISO timestamp → 'Jun 3, 2:14 pm'. Returns '' if None/unparseable."""
    if not ts_str:
        return ""
    try:
        s = ts_str.strip().replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt.strftime("%-d %b, %-I:%M %p").replace("AM", "am").replace("PM", "pm")
    except Exception:
        return ts_str[:16]


def _fmt_dollars(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"${cents / 100:,.2f}"


def _fmt_invoice(row: dict) -> dict:
    approval = (row.get("approval") or "").lower()
    if approval == "approved":
        label, css = "Created in Square", "ok"
    elif approval == "rejected":
        label, css = "Rejected", "error"
    elif approval == "edit_requested":
        label, css = "Edit requested", "warn"
    else:
        label, css = approval.replace("_", " ").title() or "Unknown", "neutral"

    ts = row.get("created_at") or ""
    return {
        "type":          "invoice",
        "name":          row.get("customer_name") or "Unknown customer",
        "tier":          row.get("tier_name") or "",
        "amount":        _fmt_dollars(row.get("total_before_tax_cents")),
        "outcome_label": label,
        "outcome_class": css,
        "square_id":     row.get("square_invoice_id") or "",
        "ts_raw":        ts,
        "ts":            _fmt_ts(ts),
    }


def _fmt_reservation(row: dict) -> dict:
    state = row.get("current_state") or ""
    label, css = _STATE_LABELS.get(state, (
        state.replace("_", " ").title(), "neutral"
    ))

    # Build "Jun 15 at 2:00 pm"
    date_str = row.get("requested_date") or ""
    time_str = row.get("requested_time") or ""
    when_parts = []
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str.split("T")[0])
            when_parts.append(dt.strftime("%-d %b"))
        except Exception:
            when_parts.append(date_str)
    if time_str:
        try:
            t = datetime.strptime(time_str[:5], "%H:%M")
            when_parts.append(f"at {t.strftime('%-I:%M %p').lower()}")
        except Exception:
            when_parts.append(f"at {time_str}")

    guests = row.get("guest_count")
    guest_str = f"{guests} guest{'s' if guests != 1 else ''}" if guests else ""
    exp = (row.get("experience_type") or "").replace("_", " ").title()

    ts = row.get("updated_at") or row.get("created_at") or ""
    return {
        "type":          "reservation",
        "name":          row.get("client_name") or "Unknown client",
        "when":          " ".join(when_parts),
        "guests":        guest_str,
        "experience":    exp,
        "outcome_label": label,
        "outcome_class": css,
        "ts_raw":        ts,
        "ts":            _fmt_ts(ts),
    }


# ---------------------------------------------------------------------------
# Telegram renderers
# ---------------------------------------------------------------------------

def render_telegram_invoice_history(limit: int = 10) -> str:
    """Return a Telegram-ready string of recent invoice activity."""
    from db.repository import list_recent_invoices

    limit = min(max(1, limit), 20)
    try:
        rows = list_recent_invoices(limit=limit)
    except Exception as e:
        return f"Could not fetch invoices: {e}"

    if not rows:
        return "No invoices recorded yet."

    parts = [f"📋 Recent Invoices (last {len(rows)})\n"]
    for row in rows:
        inv = _fmt_invoice(row)
        emoji = _STATUS_EMOJI[inv["outcome_class"]]
        detail = "  " + " · ".join(filter(None, [inv["tier"], inv["amount"]]))
        sq = f"  Square {inv['square_id']}" if inv["square_id"] else ""
        ts = f"  {inv['ts']}" if inv["ts"] else ""
        block = f"• {inv['name']}\n{detail}\n  {emoji} {inv['outcome_label']}{sq}\n{ts}".strip()
        parts.append(block)

    return "\n\n".join(parts)


def render_telegram_reservation_history(limit: int = 10) -> str:
    """Return a Telegram-ready string of recent tasting room activity."""
    from db.repository import list_recent_reservations

    limit = min(max(1, limit), 20)
    try:
        rows = list_recent_reservations(limit=limit)
    except Exception as e:
        return f"Could not fetch reservations: {e}"

    if not rows:
        return "No reservations recorded yet."

    parts = [f"🍷 Recent Reservations (last {len(rows)})\n"]
    for row in rows:
        res = _fmt_reservation(row)
        emoji = _STATUS_EMOJI[res["outcome_class"]]
        detail_parts = list(filter(None, [res["when"], res["guests"]]))
        detail = "  " + " · ".join(detail_parts) if detail_parts else ""
        exp = f"  {res['experience']}" if res["experience"] else ""
        ts = f"  {res['ts']}" if res["ts"] else ""
        lines = [f"• {res['name']}"]
        if detail:
            lines.append(detail)
        if exp:
            lines.append(exp)
        lines.append(f"  {emoji} {res['outcome_label']}")
        if ts:
            lines.append(ts)
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>Winefornia · Activity</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
       background: #f5f4f2; color: #1a1a1a; min-height: 100vh; }}
.header {{ background: #1c1b2e; color: white; padding: 18px 28px;
           display: flex; align-items: center; gap: 10px; }}
.header h1 {{ font-size: 17px; font-weight: 600; letter-spacing: -.2px; }}
.header .meta {{ font-size: 12px; color: #6b7280; margin-left: auto; }}
.feed {{ max-width: 680px; margin: 28px auto; padding: 0 14px 48px; }}
.empty {{ text-align: center; color: #9ca3af; padding: 72px 0; font-size: 15px; }}
.entry {{ background: white; border-radius: 8px; padding: 14px 18px;
          margin-bottom: 10px; border-left: 4px solid #e5e7eb;
          box-shadow: 0 1px 2px rgba(0,0,0,.05); }}
.entry.invoice     {{ border-left-color: #6366f1; }}
.entry.reservation {{ border-left-color: #059669; }}
.entry.recon       {{ border-left-color: #f59e0b; background: #fffbeb; }}
.row {{ display: flex; align-items: baseline; gap: 8px; }}
.badge {{ font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 99px;
          text-transform: uppercase; letter-spacing: .4px; flex-shrink: 0; }}
.badge.invoice     {{ background: #ede9fe; color: #4f46e5; }}
.badge.reservation {{ background: #d1fae5; color: #047857; }}
.name {{ font-weight: 600; font-size: 14px; flex: 1; }}
.ts {{ font-size: 11px; color: #9ca3af; white-space: nowrap; }}
.detail {{ margin-top: 5px; font-size: 13px; color: #6b7280; }}
.status {{ display: flex; align-items: center; gap: 4px; margin-top: 7px;
           font-size: 13px; font-weight: 500; }}
.status.ok      {{ color: #16a34a; }}
.status.warn    {{ color: #d97706; }}
.status.error   {{ color: #dc2626; }}
.status.neutral {{ color: #6b7280; }}
.sq {{ font-family: 'Courier New', monospace; font-size: 11px; color: #9ca3af;
       margin-top: 4px; }}
.recon-note {{ margin-top: 7px; font-size: 12px; color: #b45309;
               padding: 4px 8px; background: #fde68a; border-radius: 4px; }}
</style>
</head>
<body>
<div class="header">
  <span style="font-size:20px">🍷</span>
  <h1>Winefornia &mdash; Activity</h1>
  <span class="meta">Auto-refreshes every 60 s</span>
</div>
<div class="feed">
{body}
</div>
</body>
</html>
"""

_ENTRY_INVOICE = """\
<div class="entry invoice{recon_class}">
  <div class="row">
    <span class="badge invoice">Invoice</span>
    <span class="name">{name}</span>
    <span class="ts">{ts}</span>
  </div>
  {detail_html}
  <div class="status {css}">{emoji} {label}</div>
  {sq_html}
  {recon_html}
</div>"""

_ENTRY_RESERVATION = """\
<div class="entry reservation">
  <div class="row">
    <span class="badge reservation">Tasting</span>
    <span class="name">{name}</span>
    <span class="ts">{ts}</span>
  </div>
  {detail_html}
  <div class="status {css}">{emoji} {label}</div>
</div>"""


def _invoice_html(inv: dict) -> str:
    detail_parts = list(filter(None, [inv["tier"], inv["amount"]]))
    detail_html = (
        f'<div class="detail">{escape(" · ".join(detail_parts))}</div>'
        if detail_parts else ""
    )
    sq_html = (
        f'<div class="sq">Square&nbsp;{escape(inv["square_id"])}</div>'
        if inv["square_id"] else ""
    )
    recon_class = " recon" if inv.get("recon") else ""
    recon_html = (
        '<div class="recon-note">⚠️ Reconciliation needed — verify in Square and Supabase</div>'
        if inv.get("recon") else ""
    )
    emoji = _STATUS_EMOJI[inv["outcome_class"]]
    return _ENTRY_INVOICE.format(
        recon_class=recon_class,
        name=escape(inv["name"]),
        ts=escape(inv["ts"]),
        detail_html=detail_html,
        css=inv["outcome_class"],
        emoji=emoji,
        label=escape(inv["outcome_label"]),
        sq_html=sq_html,
        recon_html=recon_html,
    )


def _reservation_html(res: dict) -> str:
    detail_parts = list(filter(None, [res["when"], res["guests"], res["experience"]]))
    detail_html = (
        f'<div class="detail">{escape(" · ".join(detail_parts))}</div>'
        if detail_parts else ""
    )
    emoji = _STATUS_EMOJI[res["outcome_class"]]
    return _ENTRY_RESERVATION.format(
        name=escape(res["name"]),
        ts=escape(res["ts"]),
        detail_html=detail_html,
        css=res["outcome_class"],
        emoji=emoji,
        label=escape(res["outcome_label"]),
    )


def render_html_activity_page(limit: int = 20) -> str:
    """Return a self-contained HTML page with a unified invoice + reservation timeline."""
    from db.repository import list_recent_invoices, list_recent_reservations

    limit = min(max(1, limit), 50)

    try:
        inv_rows = list_recent_invoices(limit=limit)
    except Exception:
        inv_rows = []
    try:
        res_rows = list_recent_reservations(limit=limit)
    except Exception:
        res_rows = []

    entries: list[dict] = []
    for row in inv_rows:
        e = _fmt_invoice(row)
        entries.append(e)
    for row in res_rows:
        e = _fmt_reservation(row)
        entries.append(e)

    # Sort newest first
    entries.sort(key=lambda e: e.get("ts_raw") or "", reverse=True)
    entries = entries[:limit]

    if not entries:
        body = '<div class="empty">No activity recorded yet.</div>'
    else:
        blocks = []
        for e in entries:
            if e["type"] == "invoice":
                blocks.append(_invoice_html(e))
            else:
                blocks.append(_reservation_html(e))
        body = "\n".join(blocks)

    return _HTML_TEMPLATE.format(body=body)
