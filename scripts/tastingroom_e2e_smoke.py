"""Run a local end-to-end tasting room smoke flow.

Default behavior patches Gmail sending in-process, so no external email is sent.
Pass --real-send to use the configured Gmail service; safe-mode recipient rules
still apply in services.tastingroom_service.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("POSTGRES_CONNECTION_STRING", "")
os.environ["TELEGRAM_APPROVAL_CHAT_ID"] = ""
os.environ["TELEGRAM_TASTINGROOM_BOT_TOKEN"] = ""

from agents.tastingroom_graph import tastingroom_graph
from db.repository import get_reservation
from services.tastingroom_service import process_action_decision


def _patch_send_email() -> None:
    import services.gmail_service as gmail_service
    import services.tastingroom_service as tastingroom_service

    tastingroom_service.TASTINGROOM_TEST_RECIPIENT = "safe-smoke@example.com"

    def fake_send_email(to: str, subject: str, html: str, plain: str = "") -> dict:
        return {
            "message_id": f"fake-{int(time.time())}",
            "thread_id": "fake-thread",
            "to": to,
            "subject": subject,
            "dry_run": True,
        }

    gmail_service.send_email = fake_send_email


def _invoke(subject: str, sender: str, body: str, msg_id: str, thread_id: str) -> dict:
    return tastingroom_graph.invoke(
        {
            "raw_email": f"Subject: {subject}\nFrom: {sender}\n\n{body}",
            "sender_id": sender,
            "subject": subject,
            "from_email": sender,
            "to_email": "contact@innovatuswine.com",
            "body": body,
            "gmail_message_id": msg_id,
            "gmail_thread_id": thread_id,
        },
        config={"configurable": {"thread_id": f"smoke-{thread_id}"}},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-send", action="store_true", help="Use Gmail service instead of in-process fake sender.")
    args = parser.parse_args()

    if not args.real_send:
        _patch_send_email()

    suffix = int(time.time())
    client_email = f"tasting-smoke-{suffix}@example.com"
    thread_id = f"smoke-thread-{suffix}"

    form_body = f"""Sent via form submission from INNOVATUS WINE NAPA VALLEY
Name: Smoke Test Guest {suffix}
Email: {client_email}
Phone: (202) 734-8246
Date Requested : May 09, 2026
Time: 2:30pm
Number of Guests : 2
Production Tour and Tasting : Production Tour and Tasting with Winemaker ($110 per person)
Questions / Comments: N/A"""

    step1 = _invoke(
        "Form Submission - Wine tasting Booking",
        "Squarespace <form-submission@squarespace.info>",
        form_body,
        f"smoke-form-{suffix}",
        thread_id,
    )
    print("1", step1["current_state"], step1["recommended_action"], step1["action_id"])

    step2 = process_action_decision(step1["action_id"], "internal_available", decided_by="smoke")
    print("2", step2["status"], step2["next_action"], step2["next_action_id"])

    step3 = _invoke(
        f"Re: Form Submission - Wine tasting Booking - {step1['reservation_id']}",
        "Josh Uran <josh@thecavesatsodacanyon.com>",
        "2:30pm open\n\nCheers!\nJosh",
        f"smoke-josh-{suffix}",
        thread_id,
    )
    print("3", step3["current_state"], step3["recommended_action"], step3["action_id"])

    step5 = process_action_decision(step3["action_id"], "approve", decided_by="smoke")
    print("5", step5["status"], step5["reservation_id"])

    step6 = _invoke(
        f"Re: Innovatus tasting availability - {step1['reservation_id']}",
        f"Smoke Test Guest <{client_email}>",
        "Yes, please reserve that time for two people. Thank you!",
        f"smoke-client-{suffix}",
        thread_id,
    )
    print("6", step6["current_state"], step6["recommended_action"], step6["action_id"])

    step7 = process_action_decision(step6["action_id"], "approve", decided_by="smoke")
    print("7", step7["status"], step7["next_action_id"])

    step8 = process_action_decision(step7["next_action_id"], "invoice_sent", decided_by="smoke")
    print("8", step8["status"], step8["reservation_id"], step8["next_action_id"])

    step9 = process_action_decision(step8["next_action_id"], "paid", decided_by="smoke")
    print("9", step9["status"], step9["next_action_id"])

    step10 = process_action_decision(step9["next_action_id"], "approve", decided_by="smoke")
    print("10", step10["status"], step10["reservation_id"])

    final = get_reservation(step1["reservation_id"])
    print("final", final["current_state"], final["payment_status"], final["booking_status"])


if __name__ == "__main__":
    main()
