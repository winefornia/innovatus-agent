#!/usr/bin/env python3
"""
Winefornia Tasting Room Bot — separate Telegram bot for reservation coordination.

Run:
    source .venv/bin/activate
    python tastingroom_bot.py

Handles:
  - Tasting room approval callbacks (tr: prefix) from inline keyboards
  - Direct tasting room commands via message text
  - Gmail watcher sends approval requests to this bot's chat
"""

import asyncio
import logging

import httpx

from app.config import TELEGRAM_TASTINGROOM_BOT_TOKEN
from services.telegram_auth import is_authorized_tastingroom_update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

BOT_TOKEN = TELEGRAM_TASTINGROOM_BOT_TOKEN
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TASTINGROOM_BOT_TOKEN is not set")

BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


# -- Telegram API helpers ----------------------------------------------------

async def tg(client: httpx.AsyncClient, method: str, **kwargs) -> dict:
    payload = {k: v for k, v in kwargs.items() if v is not None}
    r = await client.post(f"{BASE}/{method}", json=payload)
    r.raise_for_status()
    return r.json()


async def send(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    await tg(client, "sendMessage", chat_id=chat_id, text=text[:4096])


async def ack(client: httpx.AsyncClient, cq_id: str) -> None:
    await tg(client, "answerCallbackQuery", callback_query_id=cq_id)


# -- Handlers ----------------------------------------------------------------

async def on_message(client: httpx.AsyncClient, message: dict) -> None:
    chat_id = message["chat"]["id"]
    text = message.get("text") or ""
    if not is_authorized_tastingroom_update(message):
        logging.warning(
            "[tastingroom auth] blocked message chat=%s user=%s",
            chat_id,
            (message.get("from") or {}).get("id"),
        )
        await send(client, chat_id, "This tasting room bot is restricted.")
        return

    if text.strip() == "/start":
        await send(client, chat_id,
            "Winefornia Tasting Room Bot\n\n"
            "This bot handles tasting room reservation approvals.\n"
            "Approval requests arrive automatically from the Gmail watcher.\n"
            "Use the inline buttons to approve, reject, or escalate actions.\n\n"
            "Commands:\n"
            "  /status — pending reservations\n"
            "  /history [n] — recent activity"
        )
        return

    # /history [n] — recent tasting room activity
    if text.strip().startswith("/history"):
        parts = text.strip().split()
        limit = 10
        if len(parts) > 1:
            try:
                limit = max(1, min(int(parts[1]), 20))
            except ValueError:
                pass
        from services.activity_service import render_telegram_reservation_history
        await send(client, chat_id, render_telegram_reservation_history(limit=limit))
        return

    if text.strip() == "/status":
        try:
            from services.tastingroom_chat_service import handle_tastingroom_chat

            await send(client, chat_id, handle_tastingroom_chat("pending", chat_id=chat_id))
        except Exception as e:
            await send(client, chat_id, f"Could not fetch reservations: {e}")
        return

    try:
        from services.tastingroom_chat_service import handle_tastingroom_chat

        await send(client, chat_id, handle_tastingroom_chat(text, chat_id=chat_id))
    except Exception as e:
        logging.error("[tastingroom message] error: %s", e, exc_info=True)
        await send(client, chat_id, f"Tasting room command failed: {e}")


async def on_callback(client: httpx.AsyncClient, callback_query: dict) -> None:
    cq_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    data = callback_query.get("data", "")
    await ack(client, cq_id)
    auth_obj = {
        "chat": callback_query.get("message", {}).get("chat") or {},
        "from": callback_query.get("from") or {},
    }
    if not is_authorized_tastingroom_update(auth_obj):
        logging.warning(
            "[tastingroom auth] blocked callback chat=%s user=%s",
            chat_id,
            (callback_query.get("from") or {}).get("id"),
        )
        await send(client, chat_id, "This tasting room bot is restricted.")
        return

    if not data.startswith("tr:"):
        await send(client, chat_id, "Unknown action.")
        return

    parts = data.split(":", 2)
    if len(parts) != 3:
        await send(client, chat_id, "Invalid tasting room action.")
        return

    _, action_id, decision = parts
    try:
        from services.tastingroom_service import process_action_decision

        result = process_action_decision(action_id, decision, decided_by=f"tg_{chat_id}")
        if result.get("ok"):
            await send(client, chat_id,
                f"Tasting room action {result.get('status')}.\n"
                f"Reservation: {result.get('reservation_id')}"
            )
        else:
            await send(client, chat_id, f"Tasting room action failed: {result.get('error')}")
    except Exception as e:
        logging.error("[tastingroom callback] error: %s", e, exc_info=True)
        await send(client, chat_id, f"Tasting room action failed: {e}")


# -- Polling loop ------------------------------------------------------------

async def _delete_webhook(client: httpx.AsyncClient) -> None:
    r = await client.post(f"{BASE}/deleteWebhook", json={"drop_pending_updates": False})
    data = r.json()
    if data.get("ok"):
        print("   Webhook cleared")


async def run() -> None:
    print("Winefornia Tasting Room Bot")
    print("   Mode: long polling")
    print("   Press Ctrl+C to stop\n")

    offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        await _delete_webhook(client)
        print("   Listening for messages...\n")

        while True:
            try:
                r = await client.get(
                    f"{BASE}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 30,
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
                pass
            except httpx.HTTPStatusError as e:
                print(f"[HTTP {e.response.status_code}] {e}")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[poll error] {e}")
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
