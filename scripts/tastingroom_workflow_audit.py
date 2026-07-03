"""Deterministic audit checks for the tasting room workflow."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.models import Reservation
from services.tastingroom_service import (
    classify_email,
    extract_email_facts,
    merge_llm_facts,
    plan_next_action_from_timeline,
)
from services.tastingroom_chat_service import _deterministic_command


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed: {detail}")
    print(f"PASS {name}")


def main() -> None:
    form_facts = {
        "client_name": "Mira Park",
        "requested_date": "2026-05-09",
        "requested_time": "14:30:00",
        "guest_count": 2,
        "candidate_slots": [],
    }
    merged = merge_llm_facts(
        form_facts,
        {
            "client_name": "Wrong Person",
            "requested_date": "2026-06-07",
            "guest_count": 6,
            "candidate_slots": [{"date": "2026-06-07", "start_time": "14:30:00"}],
        },
    )
    check("llm_does_not_override_core_facts", merged["client_name"] == "Mira Park" and merged["guest_count"] == 2)
    check("llm_can_add_slots", len(merged["candidate_slots"]) == 1)

    josh_body = "no problem.\n\n6/6 - 10 and 230 open\n6/7 - 10, 1230 and 230 open\n\nCheers!"
    msg_type = classify_email("Re: 5/19 Availability", "Josh Uran <josh@thecavesatsodacanyon.com>", josh_body)
    facts = extract_email_facts("Re: 5/19 Availability", "Josh Uran <josh@thecavesatsodacanyon.com>", josh_body, msg_type)
    check("josh_multi_slot_classified", msg_type == "josh_availability_reply", msg_type)
    check("josh_multi_slot_extracts_five_slots", len(facts["candidate_slots"]) == 5, str(facts["candidate_slots"]))

    staff_body = "Hi Josh,\n\nCan I also book 6/7 at 2:30 for a party of 2?\n\nThank you,\nAudrey"
    msg_type = classify_email("Re: 5/19 Availability", "INNOVATUS <contact@innovatuswine.com>", staff_body)
    facts = extract_email_facts("Re: 5/19 Availability", "INNOVATUS <contact@innovatuswine.com>", staff_body, msg_type)
    check("facility_booking_request_classified", msg_type == "facility_booking_request", msg_type)
    check("facility_booking_request_slot", facts["requested_date"] == "2026-06-07" and facts["requested_time"] == "14:30:00")

    final = Reservation(
        reservation_id="AUDIT-FINAL",
        current_state="FINAL_CONFIRMED",
        payment_status="paid",
        recommended_action="send_final_confirmation",
    )
    planned = plan_next_action_from_timeline(final, message_type="final_confirmation_sent", fallback_action="send_final_confirmation")
    check("terminal_case_no_action", planned["recommended_action"] == "", str(planned))

    unpaid = Reservation(
        reservation_id="AUDIT-UNPAID",
        current_state="CLIENT_ACCEPTED_SLOT",
        payment_status="not_sent",
        recommended_action="send_final_confirmation",
    )
    planned = plan_next_action_from_timeline(unpaid, message_type="client_acceptance", fallback_action="send_final_confirmation")
    check("unpaid_case_no_final_confirmation", planned["recommended_action"] != "send_final_confirmation", str(planned))

    square_created = classify_email(
        "A new invoice was created for Mira Park (#202440)",
        "Square <invoicing@messaging.squareup.com>",
        "Hello INNOVATUS,\n\nYou have sent an invoice of $237.05 to Mira Park.",
    )
    check("square_invoice_created_classified", square_created == "invoice_payment_message", square_created)
    facts = extract_email_facts(
        "A new invoice was created for Mira Park (#202440)",
        "Square <invoicing@messaging.squareup.com>",
        "Hello INNOVATUS,\n\nYou have sent an invoice of $237.05 to Mira Park.",
        square_created,
    )
    check("square_invoice_created_status", facts.get("payment_status") == "sent", str(facts))

    square_paid = classify_email(
        "An invoice was paid by Mira Park! (#202440)",
        "Square <invoicing@messaging.squareup.com>",
        "Hello INNOVATUS,\n\nMira Park has paid invoice #202440 for $237.05.",
    )
    facts = extract_email_facts(
        "An invoice was paid by Mira Park! (#202440)",
        "Square <invoicing@messaging.squareup.com>",
        "Hello INNOVATUS,\n\nMira Park has paid invoice #202440 for $237.05.",
        square_paid,
    )
    check("square_invoice_paid_status", facts.get("payment_status") == "paid", str(facts))

    command = _deterministic_command("mark Haein paid")
    check("chat_command_mark_paid", command and command.get("intent") == "mark_paid", str(command))

    command = _deterministic_command("revise Haein email to be warmer")
    check("chat_command_revise_email", command and command.get("intent") == "revise_pending_email", str(command))


if __name__ == "__main__":
    main()
