#!/usr/bin/env python3
"""
Winefornia Invoice Bot — Telegram long-polling (24/7, no public URL needed).

Run:
    source .venv/bin/activate
    python bot.py

Handles the full invoice pipeline via Telegram inline keyboards:
  - Text orders / forwarded emails → extract → tier → approval → Square draft
  - PDF attachments → extract → same pipeline
  - Inline keyboard buttons for every decision point
"""

import asyncio
import logging
import httpx
from langgraph.types import Command

from agents.invoice_graph import invoice_graph, DEFAULT_INVOICE_MESSAGE
from services.control_layer import control


def _close_case_if_done(result: dict, ix_after: str | None) -> None:
    """Close the control-layer case once no more interrupts remain.

    Called from on_callback and on_message resume paths — those paths don't
    open cases (that's _run()'s job) but they DO complete them.
    """
    if ix_after is not None:
        return   # pipeline still running — leave case open
    case_id = result.get("_case_id", "")
    if not case_id:
        return
    case = control.get_case(case_id)
    if not case:
        return   # already closed or not found
    final   = result.get("final_response", "")
    outcome = "success" if result.get("square_invoice_id") else "completed"
    control.close_case(case, outcome, final)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
from app.config import TELEGRAM_BOT_TOKEN
from services.pdf_service import extract_invoice_fields_from_pdf
from services.telegram_service import download_document

BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Per-chat tier wizard accumulator  {chat_id: {tier, schedule}}
_wizard: dict[int, dict] = {}


# ── Telegram API helpers ────────────────────────────────────────────────────

async def tg(client: httpx.AsyncClient, method: str, **kwargs) -> dict:
    payload = {k: v for k, v in kwargs.items() if v is not None}
    r = await client.post(f"{BASE}/{method}", json=payload)
    r.raise_for_status()
    return r.json()


async def send(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    await tg(client, "sendMessage", chat_id=chat_id, text=text[:4096])


async def send_kb(
    client: httpx.AsyncClient,
    chat_id: int,
    text: str,
    rows: list[list[tuple[str, str]]],
) -> None:
    """Send a message with an inline keyboard.
    rows = list of rows, each row = list of (label, callback_data) tuples.
    """
    markup = {
        "inline_keyboard": [
            [{"text": lbl, "callback_data": data} for lbl, data in row]
            for row in rows
        ]
    }
    await tg(client, "sendMessage", chat_id=chat_id, text=text[:4096], reply_markup=markup)


async def ack(client: httpx.AsyncClient, cq_id: str) -> None:
    await tg(client, "answerCallbackQuery", callback_query_id=cq_id)


# ── State → interrupt type ──────────────────────────────────────────────────

def which(state: dict | None) -> str | None:
    if not state:
        return None
    if state.get("missing_fields"):
        return "missing"
    if state.get("customer") and state.get("customer_confirmed") is False:
        return "confirm_customer"
    if state.get("customer") and not state.get("tier_name"):
        return "tier"
    # Paused at interpret_edit — approval set to "edit_requested", waiting for edit text
    if state.get("approval") == "edit_requested" and state.get("invoice_preview"):
        return "edit_instruction"
    # Paused at apply_patch — waiting to confirm a low-confidence field change
    if (state.get("edit_instruction") and not state.get("approval")
            and state.get("invoice_preview") and not state.get("square_invoice_id")):
        changes = (state.get("edit_patch") or {}).get("field_changes", [])
        if any(c.get("confidence", 1.0) < 0.80 for c in changes):
            return "edit_clarification"
    if state.get("invoice_preview") and not state.get("approval") and not state.get("square_invoice_id"):
        return "approval"
    if state.get("square_invoice_id") and not state.get("send_decision"):
        return "send"
    if state.get("send_decision") == "send" and not state.get("email_receipt_decision"):
        return "email"
    return None


# ── State renderer ──────────────────────────────────────────────────────────

async def render(client: httpx.AsyncClient, chat_id: int, state: dict) -> None:
    """Send the right message or keyboard based on current graph interrupt."""
    ix = which(state)

    if ix == "missing":
        fields = state.get("missing_fields", [])
        await send(client, chat_id,
            "I need a bit more info. Please provide:\n• " + "\n• ".join(fields))

    elif ix == "confirm_customer":
        c = state.get("customer", {})
        name = c.get("full_name") or c.get("company") or "Unknown"
        parts = [c.get("company"), c.get("email"), c.get("phone"), c.get("tier_name")]
        detail = "\n".join(f"  {p}" for p in parts if p)
        await send_kb(client, chat_id,
            f"Found a potential match:\n\n{name}\n{detail}\n\nIs this the right customer?",
            [[("✅ Yes, this is them", "yes"), ("❌ No, create new", "no")]]
        )

    elif ix == "tier":
        c = state.get("customer", {})
        name = c.get("full_name") or c.get("company") or "customer"
        known = c.get("tier_name") or ""
        msg = f"Invoice for {name}."
        if known:
            msg += f"\nTier on file: {known}"
        msg += "\n\nSelect pricing tier:"
        _wizard[chat_id] = {}
        await send_kb(client, chat_id, msg, [
            [("Wholesale (30%)",    "tier:Wholesale"),    ("Corporate (20%)",  "tier:Corporate")],
            [("Club Member (15%)", "tier:Club Member"),  ("Employee (50%)",   "tier:Employee")],
            [("Direct (0%)",       "tier:Direct"),        ("FOB/Export (50%)", "tier:FOB/Export")],
        ])

    elif ix == "approval":
        pr   = state.get("invoice_preview", {})
        c    = state.get("customer", {})
        name = c.get("full_name") or c.get("company") or "Customer"
        tier = state.get("tier_name") or pr.get("tier_name") or ""
        sched = (state.get("payment_schedule") or "").replace("_", " ")
        items = state.get("line_items", [])
        lines = []
        for i in items:
            vintage = i.get("vintage")
            prod = " ".join(filter(None, [i.get("product_name"), str(vintage) if vintage is not None else None]))
            qty  = i.get("quantity", 0)
            tot  = (i.get("line_total_cents") or 0) / 100
            lines.append(f"  {prod} × {qty} = ${tot:.2f}")
        disc_cents = pr.get("discount_cents") or 0
        ship_cents = pr.get("shipping_cents")
        ship_str   = ("Waived" if ship_cents == 0
                      else ("TBD" if ship_cents is None else f"${ship_cents/100:.2f}"))
        total = (pr.get("total_before_tax_cents") or 0) / 100
        body  = "\n".join(lines) or "  (no items)"
        disc_line = f"  Discount: -${disc_cents/100:.2f}\n" if disc_cents > 0 else ""
        await send_kb(client, chat_id,
            f"📋 Invoice Ready — {name}\n"
            f"Tier: {tier}  |  Due: {sched}\n\n"
            f"{body}\n\n"
            f"{disc_line}"
            f"  Shipping: {ship_str}\n"
            f"  Total: ${total:.2f}\n\n"
            "Create this draft in Square?",
            [[("✅ Approve", "approved"), ("✏️ Edit", "__edit__"), ("❌ Reject", "rejected")]]
        )

    elif ix == "send":
        sq_id = state.get("square_invoice_id", "")
        pr    = state.get("invoice_preview", {})
        total = (pr.get("total_before_tax_cents") or 0) / 100
        c     = state.get("customer", {})
        name  = c.get("full_name") or c.get("company") or "customer"
        sq_link = f"https://squareup.com/dashboard/invoices/{sq_id}"
        await send_kb(client, chat_id,
            f"✅ Draft saved in Square\n{name} · ${total:.2f}\nID: {sq_id}\n{sq_link}\n\nSend to client?",
            [[("📤 Send to Client", "send"), ("💾 Keep as Draft", "draft")]]
        )

    elif ix == "email":
        c    = state.get("customer", {})
        name = c.get("full_name") or c.get("company") or "client"
        em   = c.get("email") or ""
        await send_kb(client, chat_id,
            f"Invoice sent! Send email receipt to {name}" + (f" ({em})" if em else "") + "?",
            [[("📧 Send Receipt", "send"), ("⏭ Skip", "skip")]]
        )

    elif ix == "edit_instruction":
        await send(client, chat_id,
            "What would you like to change?\n"
            "Describe the edit (e.g. 'make it 6 bottles', 'change to NET_14', "
            "'use Oak Barrel instead').")

    elif ix == "edit_clarification":
        changes = (state.get("edit_patch") or {}).get("field_changes", [])
        low_conf = [c for c in changes if c.get("confidence", 1.0) < 0.80]
        if low_conf:
            ch    = low_conf[0]
            field = ch.get("field", "field")
            old_v = ch.get("old_value", "?")
            new_v = ch.get("new_value", "?")
            await send_kb(client, chat_id,
                f"Just to confirm: change {field} from {old_v!r} to {new_v!r}?",
                [[("✅ Yes, confirm", "yes"), ("❌ Cancel edit", "cancel")]]
            )
        else:
            await send(client, chat_id, "Please confirm the edit.")

    else:
        final = (state or {}).get("final_response")
        if final:
            await send(client, chat_id, final)


# ── Update handlers ─────────────────────────────────────────────────────────

async def on_message(client: httpx.AsyncClient, message: dict) -> None:
    chat_id = message["chat"]["id"]
    text    = message.get("text") or ""

    # /start command
    if text.strip() == "/start":
        await send(client, chat_id,
            "🍷 Winefornia Invoice Agent\n\n"
            "Send me a customer order and I'll create a Square invoice draft.\n\n"
            "Examples:\n"
            '• "John Smith, Oak Barrel, 12 Cabernet 2022, 6 Rosé 2021"\n'
            "• Paste a forwarded email order\n"
            "• Send a PDF attachment\n\n"
            "I'll walk you through the rest step by step."
        )
        return

    # PDF attachment
    doc = message.get("document")
    if doc and not text:
        mime  = doc.get("mime_type", "")
        fname = doc.get("file_name", "")
        if "pdf" in mime or fname.lower().endswith(".pdf"):
            await _run_pdf(client, chat_id, doc["file_id"])
        else:
            await send(client, chat_id, "Please send a PDF file for order extraction.")
        return

    if not text:
        return

    # Resume any active text-input interrupt instead of starting fresh
    thread_id = f"tg_{chat_id}"
    config    = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = invoice_graph.get_state(config)
        if snapshot and snapshot.next:
            ix = which(snapshot.values)
            if ix in ("missing", "edit_instruction", "edit_clarification"):
                result   = invoice_graph.invoke(Command(resume=text), config=config)
                ix_after = which(result)
                _close_case_if_done(result, ix_after)
                await render(client, chat_id, result)
                return
    except Exception:
        pass

    await _run(client, chat_id, text)


async def on_callback(client: httpx.AsyncClient, callback_query: dict) -> None:
    cq_id   = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    data    = callback_query.get("data", "")
    await ack(client, cq_id)

    thread_id = f"tg_{chat_id}"
    config    = {"configurable": {"thread_id": thread_id}}

    # ── Tier wizard: step 1 — tier selected ────────────────────────────────
    if data.startswith("tier:"):
        tier = data[5:]
        _wizard.setdefault(chat_id, {})["tier"] = tier
        await send_kb(client, chat_id,
            f"Tier: {tier} ✓\n\nPayment schedule:",
            [
                [("Upon Receipt", "sched:UPON_RECEIPT"), ("NET 7",  "sched:NET_7")],
                [("NET 14",       "sched:NET_14"),       ("NET 30", "sched:NET_30")],
            ]
        )
        return

    # ── Tier wizard: step 2 — schedule selected ────────────────────────────
    if data.startswith("sched:"):
        sched = data[6:]
        _wizard.setdefault(chat_id, {})["schedule"] = sched
        label = sched.replace("_", " ")
        await send_kb(client, chat_id,
            f"Schedule: {label} ✓\n\nPayment methods:",
            [
                [("💳 Card + 🏦 Bank ACH", "methods:CARD+BANK_ACCOUNT")],
                [("💳 Card only",           "methods:CARD")],
                [("🏦 Bank ACH only",       "methods:BANK_ACCOUNT")],
            ]
        )
        return

    # ── Tier wizard: step 3 — methods selected → resume graph ─────────────
    if data.startswith("methods:"):
        methods_str = data[8:]
        ws    = _wizard.pop(chat_id, {})
        tier  = ws.get("tier", "Wholesale")
        sched = ws.get("schedule", "NET_30")
        resume_val = f"{tier}, {sched}, {methods_str}"
        logging.info("[wizard] resuming with: %r", resume_val)
        try:
            result = invoice_graph.invoke(Command(resume=resume_val), config=config)
            ix = which(result)
            logging.info("[wizard] result: which=%r tier_name=%r line_items=%d",
                         ix, result.get("tier_name"), len(result.get("line_items", [])))
            _close_case_if_done(result, ix)
            await render(client, chat_id, result)
        except Exception as e:
            logging.error("[wizard] error: %s", e, exc_info=True)
            # Label failure on the active case if we can find it
            snapshot = None
            try:
                snapshot = invoice_graph.get_state(config)
            except Exception:
                pass
            if snapshot:
                case_id = (snapshot.values or {}).get("_case_id", "")
                case = control.get_case(case_id)
                if case:
                    control.label_failure(case, "square_api_error", "high", "on_callback_wizard", str(e), "tool")
                    control.close_case(case, "failed", "", str(e))
            await send(client, chat_id, f"Error applying tier: {e}")
        return

    # ── Edit — resume graph with "edit" to route into interpret_edit node ──
    if data == "__edit__":
        try:
            result   = invoice_graph.invoke(Command(resume="edit"), config=config)
            ix_after = which(result)
            _close_case_if_done(result, ix_after)
            await render(client, chat_id, result)
        except Exception as e:
            logging.error("[edit] error: %s", e, exc_info=True)
            await send(client, chat_id, f"Error starting edit: {e}")
        return

    # ── All other callbacks → guard then resume graph ──────────────────────
    # Each callback is only valid at a specific interrupt stage.
    # Reject stale callbacks (e.g. double-tap YES sending "yes" to the wrong interrupt).
    _VALID_AT: dict[str, set[str]] = {
        "yes":      {"confirm_customer", "edit_clarification"},
        "no":       {"confirm_customer"},
        "approved": {"approval"},
        "rejected": {"approval"},
        "draft":    {"send"},
        "skip":     {"email"},
        "send":     {"send", "email"},
        "cancel":   {"edit_clarification"},
    }
    if data in _VALID_AT:
        try:
            snapshot = invoice_graph.get_state(config)
            ix = which(snapshot.values) if snapshot else None
        except Exception:
            ix = None
        logging.info("[callback] data=%r current_interrupt=%r valid_at=%s", data, ix, _VALID_AT[data])
        if ix not in _VALID_AT[data]:
            logging.warning("[callback] DROPPING stale callback data=%r (interrupt=%r)", data, ix)
            return  # stale or misrouted callback — silently ignore

    try:
        logging.info("[callback] resuming graph with data=%r thread=%s", data, thread_id)
        result   = invoice_graph.invoke(Command(resume=data), config=config)
        ix_after = which(result)
        logging.info("[callback] after resume: which=%r keys=%s", ix_after, [k for k in result if result[k] is not None])
        _close_case_if_done(result, ix_after)
        await render(client, chat_id, result)
    except Exception as e:
        logging.error("[callback] error: %s", e, exc_info=True)
        # Label failure on the active case
        try:
            snapshot = invoice_graph.get_state(config)
            case_id  = (snapshot.values or {}).get("_case_id", "") if snapshot else ""
            case     = control.get_case(case_id)
            if case:
                control.label_failure(case, "square_api_error", "high", "on_callback", str(e), "tool")
                control.close_case(case, "failed", "", str(e))
        except Exception:
            pass
        await send(client, chat_id, f"Error: {e}")


async def _run(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    """Route a text message through the gateway and render the result."""
    from services.gateway import gateway, from_telegram

    logging.info("[run] chat=%s text=%r", chat_id, text[:80])
    msg    = from_telegram(chat_id, text)
    result = await asyncio.get_event_loop().run_in_executor(None, gateway.dispatch, msg)

    if result.get("blocked"):
        await send(client, chat_id, result.get("final_response", "Request blocked."))
        return

    ix = which(result)
    logging.info("[run] after gateway: interrupt=%r intent=%r tier=%r",
                 ix, result.get("intent"), result.get("tier_name"))

    if result.get("error") and not ix:
        await send(client, chat_id,
            f"Something went wrong: {result['error']}\n\nPlease try again.")
        return

    await render(client, chat_id, result)


async def _run_pdf(client: httpx.AsyncClient, chat_id: int, file_id: str) -> None:
    """Download a Telegram PDF, extract fields, run graph."""
    await send(client, chat_id, "📄 Reading PDF...")
    try:
        pdf_bytes = download_document(file_id)
        text      = extract_invoice_fields_from_pdf(pdf_bytes)
        await _run(client, chat_id, text)
    except Exception as e:
        await send(client, chat_id, f"Couldn't read that PDF: {e}")


# ── Polling loop ────────────────────────────────────────────────────────────

async def _delete_webhook(client: httpx.AsyncClient) -> None:
    """Clear any registered webhook so long polling works."""
    r = await client.post(f"{BASE}/deleteWebhook", json={"drop_pending_updates": False})
    data = r.json()
    if data.get("ok"):
        print("   Webhook cleared ✓")


async def run() -> None:
    print("🍷 Winefornia Invoice Bot")
    print("   Bot:  @FireHorse00_bot")
    print("   Mode: long polling  (no public URL needed)")
    print("   Press Ctrl+C to stop\n")

    offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        await _delete_webhook(client)   # ensure no conflicting webhook
        print("   Listening for messages...\n")

        while True:
            try:
                r = await client.get(
                    f"{BASE}/getUpdates",
                    params={
                        "offset":          offset,
                        "timeout":         30,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                r.raise_for_status()
                for update in r.json().get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        if "message" in update:
                            await on_message(client, update["message"])
                        elif "callback_query" in update:
                            await on_callback(client, update["callback_query"])
                    except Exception as e:
                        print(f"[update error] {e}")

            except httpx.ReadTimeout:
                pass                          # normal — no updates in 30s window
            except httpx.HTTPStatusError as e:
                print(f"[HTTP {e.response.status_code}] {e}")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[poll error] {e}")
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
