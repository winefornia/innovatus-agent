"""Gmail service — intake reading + receipt email sending.

Auth: loads token from GMAIL_TOKEN_JSON_B64 env var (Fly.io)
      or from token.json in the project root (local dev).

Run scripts/google_auth.py once to get the token, then:
    flyctl secrets set GMAIL_TOKEN_JSON_B64=$(base64 -i token.json)
"""
import base64
import json
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import ANTHROPIC_API_KEY

_PROJECT_ROOT = Path(__file__).parent.parent
_TOKEN_FILE = _PROJECT_ROOT / "token.json"
_INTAKE_LABEL = os.getenv("GMAIL_INTAKE_LABEL", "To Invoice")
_PROCESSED_LABEL = os.getenv("GMAIL_PROCESSED_LABEL", "Invoice Processed")
_FORWARDING_QUERY = os.getenv("GMAIL_FORWARDING_QUERY", 'subject:"To Invoice"')
_GOOGLE_ACCOUNT_EMAIL = os.getenv("GOOGLE_ACCOUNT_EMAIL", "").strip().lower()

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
]

_service = None
_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _load_token_info() -> dict:
    """Load token JSON from env var (Fly.io) or local file (dev)."""
    if _GOOGLE_ACCOUNT_EMAIL:
        safe_account = _GOOGLE_ACCOUNT_EMAIL.upper().replace("@", "_").replace(".", "_").replace("-", "_")
        account_b64 = os.environ.get(f"GOOGLE_TOKEN_JSON_B64_{safe_account}")
        if account_b64:
            return json.loads(base64.b64decode(account_b64).decode())

    b64 = os.environ.get("GMAIL_TOKEN_JSON_B64")
    if b64:
        return json.loads(base64.b64decode(b64).decode())

    token_json = os.environ.get("GMAIL_TOKEN_JSON")
    if token_json:
        return json.loads(token_json)

    for path in (
        os.environ.get("GMAIL_TOKEN_FILE"),
        str(_PROJECT_ROOT / f"token-{_GOOGLE_ACCOUNT_EMAIL.replace('@', '-').replace('.', '-')}.json")
        if _GOOGLE_ACCOUNT_EMAIL else "",
        str(_TOKEN_FILE),
        str(Path.home() / ".hermes" / "google_token.json"),
    ):
        if path and Path(path).exists():
            return json.loads(Path(path).read_text())

    raise RuntimeError(
        "No Gmail token found. Run scripts/google_auth.py then set "
        "GMAIL_TOKEN_JSON_B64 env var (flyctl secrets set ...)."
    )


def _get_service():
    global _service
    if _service:
        return _service

    token_info = _load_token_info()
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Gmail credentials expired and can't be refreshed. "
                "Re-run scripts/google_auth.py."
            )

    _service = build("gmail", "v1", credentials=creds)
    return _service


def _get_label_id(service, label_name: str) -> Optional[str]:
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    return None


def _ensure_label(service, label_name: str) -> str:
    label_id = _get_label_id(service, label_name)
    if label_id:
        return label_id
    label = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return label["id"]


def list_labels() -> list[dict]:
    """Return Gmail labels visible to the authenticated account."""
    service = _get_service()
    return service.users().labels().list(userId="me").execute().get("labels", [])


def delete_label(label_name: str) -> dict:
    """Delete a custom Gmail label by name. System labels are ignored by Gmail."""
    service = _get_service()
    label_id = _get_label_id(service, label_name)
    if not label_id:
        return {"status": "missing", "label": label_name}
    service.users().labels().delete(userId="me", id=label_id).execute()
    return {"status": "deleted", "label": label_name}


# ---------------------------------------------------------------------------
# Intake — read emails by label / query
# ---------------------------------------------------------------------------

def _decode_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        if part.get("parts"):
            result = _decode_body(part)
            if result:
                return result
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html).strip()
    return ""


def list_intake_emails(max_results: int = 10) -> dict:
    """List emails tagged 'To Invoice' or matching the forwarding query."""
    return list_emails(label_name=_INTAKE_LABEL, query=_FORWARDING_QUERY, max_results=max_results)


def list_emails(label_name: str = "", query: str = "", max_results: int = 10) -> dict:
    """List emails by Gmail label and/or search query."""
    service = _get_service()
    label_id = _get_label_id(service, label_name) if label_name else None
    seen: dict[str, str] = {}

    if label_id:
        res = service.users().messages().list(userId="me", labelIds=[label_id], maxResults=max_results).execute()
        for m in res.get("messages", []):
            seen[m["id"]] = "label"

    if query:
        res = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        for m in res.get("messages", []):
            seen.setdefault(m["id"], "query")

    messages = []
    for mid in list(seen)[:max_results]:
        msg = service.users().messages().get(userId="me", id=mid, format="metadata",
                                              metadataHeaders=["Subject", "From", "Date"]).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append({
            "message_id": msg["id"],
            "thread_id": msg["threadId"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "source": seen[mid],
        })

    return {"messages": messages, "label": label_name, "query": query}


def list_emails_multi(
    *,
    label_names: list[str] | None = None,
    query: str = "",
    max_results: int = 10,
) -> dict:
    """List messages from multiple labels plus an optional Gmail search query."""
    service = _get_service()
    seen: dict[str, str] = {}

    for label_name in label_names or []:
        label_id = _get_label_id(service, label_name)
        if not label_id:
            continue
        res = service.users().messages().list(userId="me", labelIds=[label_id], maxResults=max_results).execute()
        for m in res.get("messages", []):
            seen.setdefault(m["id"], f"label:{label_name}")

    if query:
        res = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        for m in res.get("messages", []):
            seen.setdefault(m["id"], "query")

    messages = []
    for mid in list(seen)[:max_results]:
        msg = service.users().messages().get(
            userId="me",
            id=mid,
            format="metadata",
            metadataHeaders=["Subject", "From", "To", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        label_names_for_message = _message_label_names(service, msg.get("labelIds", []))
        messages.append({
            "message_id": msg["id"],
            "thread_id": msg["threadId"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "source": seen[mid],
            "labels": label_names_for_message,
        })

    return {"messages": messages, "labels": label_names or [], "query": query}


def list_thread_messages(thread_id: str, *, max_results: int = 20) -> list[dict]:
    """List messages in a Gmail thread with enough metadata for mailbox routing."""
    service = _get_service()
    thread = service.users().threads().get(
        userId="me",
        id=thread_id,
        format="metadata",
        metadataHeaders=["Subject", "From", "To", "Date"],
    ).execute()
    messages = []
    for msg in (thread.get("messages") or [])[-max_results:]:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append({
            "message_id": msg["id"],
            "thread_id": msg.get("threadId") or thread_id,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "source": f"thread:{thread_id}",
            "labels": _message_label_names(service, msg.get("labelIds", [])),
        })
    return messages


def _message_label_names(service, label_ids: list[str]) -> list[str]:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    names = {label["id"]: label["name"] for label in labels}
    return [names.get(label_id, label_id) for label_id in label_ids]


def get_message_label_names(message_id: str) -> list[str]:
    """Return label names currently attached to a message."""
    service = _get_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="metadata").execute()
    return _message_label_names(service, msg.get("labelIds", []))


def read_email(message_id: str) -> dict:
    """Read full content of an email by message ID."""
    service = _get_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body = _decode_body(msg.get("payload", {}))
    return {
        "message_id": msg["id"],
        "thread_id": msg["threadId"],
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "date": headers.get("Date", ""),
        "body": body,
    }


def mark_processed(message_id: str) -> dict:
    """Move a message from 'To Invoice' → 'Invoice Processed'."""
    return move_message_label(message_id, remove_label=_INTAKE_LABEL, add_label=_PROCESSED_LABEL)


def apply_message_labels(
    message_id: str,
    *,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict:
    """Apply one or more Gmail labels, creating custom labels as needed."""
    service = _get_service()
    add_ids = [_ensure_label(service, label) for label in (add_labels or []) if label]
    remove_ids = [
        label_id
        for label in (remove_labels or [])
        if label and (label_id := _get_label_id(service, label))
    ]

    modify_body: dict[str, list[str]] = {}
    if add_ids:
        modify_body["addLabelIds"] = add_ids
    if remove_ids:
        modify_body["removeLabelIds"] = remove_ids

    if modify_body:
        service.users().messages().modify(userId="me", id=message_id, body=modify_body).execute()
    return {"status": "labeled", "message_id": message_id, "added": add_labels or [], "removed": remove_labels or []}


def move_message_label(message_id: str, remove_label: str = "", add_label: str = "") -> dict:
    """Move a message between labels, creating the destination label if needed."""
    apply_message_labels(
        message_id,
        add_labels=[add_label] if add_label else [],
        remove_labels=[remove_label] if remove_label else [],
    )
    return {"status": "processed", "message_id": message_id}


# ---------------------------------------------------------------------------
# Send email
# ---------------------------------------------------------------------------

def send_email(to: str, subject: str, html: str, plain: str = "") -> dict:
    """Send an email from the authenticated account."""
    service = _get_service()

    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = "me"
    msg["Subject"] = subject

    if plain:
        msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": result["id"], "thread_id": result.get("threadId"), "to": to}


# ---------------------------------------------------------------------------
# Receipt email composition
# ---------------------------------------------------------------------------

def compose_receipt_email(state: dict) -> dict:
    """Use Claude Haiku to write a professional receipt / invoice email.

    Returns {"subject": str, "html": str, "plain": str}.
    """
    customer = state.get("customer", {})
    preview = state.get("invoice_preview", {})
    line_items = state.get("line_items", [])
    tier = state.get("tier_name", "")
    schedule = state.get("payment_schedule", "UPON_RECEIPT")
    methods = state.get("payment_methods", [])
    sq_invoice_id = state.get("square_invoice_id", "")

    customer_name = customer.get("full_name") or customer.get("company") or "Valued Customer"
    customer_email = customer.get("email", "")
    total_cents = preview.get("total_before_tax_cents", 0)
    total = total_cents / 100
    subtotal = preview.get("subtotal_cents", 0) / 100
    discount = preview.get("discount_cents", 0) / 100
    shipping = preview.get("shipping_cents", 0) / 100

    items_text = "\n".join(
        f"  - {li.get('name','')} {li.get('vintage','')} × {int(li.get('quantity',1))} {li.get('unit','bottle')} @ ${(li.get('unit_price_cents',0)/100):.2f}"
        for li in line_items
    )

    payment_methods_str = " or ".join(m.replace("_", " ").title() for m in methods) if methods else "per agreement"

    prompt = f"""Write a professional, warm receipt/invoice email for a wine distributor (Winefornia).

Customer: {customer_name}
Email: {customer_email}
Square Invoice ID: {sq_invoice_id}
Pricing tier: {tier}
Payment terms: {schedule.replace('_', ' ')}
Payment methods: {payment_methods_str}

Order summary:
{items_text}

Subtotal: ${subtotal:.2f}
{"Discount: -$" + f"{discount:.2f}" if discount > 0 else ""}
{"Shipping: $" + f"{shipping:.2f}" if shipping > 0 else "Shipping: waived"}
Total: ${total:.2f}

Write a short, professional email (3–4 paragraphs) thanking them for their order,
summarising what they ordered, stating payment terms, and letting them know
a Square invoice link will arrive separately (or is attached).
Sign off as "Winefornia Team".

Return JSON with keys "subject", "html" (full HTML email body), and "plain" (plain text version).
No markdown fences."""

    resp = _claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # Strip any accidental markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: plain text email
        subject = f"Your Winefornia Order — {customer_name}"
        plain = f"Hi {customer_name},\n\nThank you for your order.\n\nTotal: ${total:.2f}\nPayment: {schedule.replace('_',' ')}\n\nWe'll send your Square invoice separately.\n\nWinefornia Team"
        html = plain.replace("\n", "<br>")
        return {"subject": subject, "html": html, "plain": plain}


def send_receipt(state: dict) -> dict:
    """Compose and send a receipt email. Returns send result + email copy."""
    customer = state.get("customer", {})
    to_email = customer.get("email") or state.get("extracted", {}).get("email")
    if not to_email:
        return {"error": "No customer email address on file."}

    email_content = compose_receipt_email(state)
    result = send_email(
        to=to_email,
        subject=email_content["subject"],
        html=email_content["html"],
        plain=email_content.get("plain", ""),
    )
    return {**result, "subject": email_content["subject"], "to": to_email}
