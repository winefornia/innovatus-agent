"""Replay the real Mira booking email sequence into the tastingroom agent.

This is a shadow-mode historical import: it creates reservation rows,
availability claims, and events, but does not approve or send outbound email.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_APPROVAL_CHAT_ID", "")
os.environ.setdefault("POSTGRES_CONNECTION_STRING", "")

from agents.tastingroom_graph import tastingroom_graph
from services.gmail_service import read_email
import services.tastingroom_service as tastingroom_service


MIRA_MESSAGE_IDS = [
    "19df0672d1adc9e9",  # Squarespace form
    "19df94a68b074bea",  # May 9 unavailable
    "19df9873ff793418",  # Client asks 6/6
    "19df9f4685ae7dae",  # Staff asks Josh 6/6 and 6/7
    "19df9f9e60702141",  # Josh gives open slots
    "19dfa40ff307cdf9",  # Staff offers 6/6 2:30
    "19dfadd1a31a0c6c",  # Client asks 6/7 AM or 2:30
    "19dff3368c7accbc",  # Staff offers 6/7 2:30
    "19dff89610d3bf27",  # Client accepts
    "19dfe66e1a701059",  # Staff asks Josh to book 6/7 2:30
    "19e040dcafc2705a",  # Staff confirms grouped bookings
    "19e040fe53faf6c9",  # Josh confirms grouped bookings
    "19e04c413cb4d7b3",  # Tentative booking + invoice link
    "19e04c04e220f935",  # Square invoice created
    "19e04cba91d71bb4",  # Square invoice paid
    "19e1e44269f96304",  # Final confirmation
]

MIRA_FORM_BODY = """Sent via form submission from INNOVATUS WINE NAPA VALLEY
Name: Mira Park
Email: mirasopa@gmail.com
Phone: (202) 734-8246
Date Requested : May 09, 2026
Time: 2:30pm
Number of Guests : 2
Production Tour and Tasting : Production Tour and Tasting with Winemaker ($110 per person)
Questions / Comments: N/A"""


def main() -> None:
    tastingroom_service.llm_extract_email = lambda *args, **kwargs: {}

    for message_id in MIRA_MESSAGE_IDS:
        msg = read_email(message_id)
        body = msg["body"] or (MIRA_FORM_BODY if message_id == "19df0672d1adc9e9" else "")
        raw_email = (
            f"Subject: {msg['subject']}\n"
            f"From: {msg['from']}\n"
            f"To: {msg['to']}\n\n"
            f"{body}"
        )
        out = tastingroom_graph.invoke(
            {
                "raw_email": raw_email,
                "sender_id": msg["from"],
                "subject": msg["subject"],
                "from_email": msg["from"],
                "to_email": msg["to"],
                "body": body,
                "gmail_message_id": message_id,
                "gmail_thread_id": msg["thread_id"],
            },
            config={"configurable": {"thread_id": f"mira-replay-{message_id}"}},
        )
        print(
            message_id,
            out.get("message_type"),
            out.get("reservation_id"),
            out.get("current_state"),
            out.get("recommended_action"),
            out.get("claims_count"),
        )


if __name__ == "__main__":
    main()
