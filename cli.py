"""Local CLI for testing the Invoice Agent without WhatsApp or FastAPI.

Usage:
  python cli.py
  python cli.py --message "Invoice for Anna Lee, 12 Pinot Noir 2021"
"""

import argparse
from langgraph.types import Command
from agents.invoice_graph import invoice_graph

DEFAULT_MESSAGE = (
    "hey just talked to tom from pacific wine merchants, he wants 3 cases cab sauv 2022 "
    "and 2 cases of the pinot, also 6 bottles of chard 2023. net 30 terms. "
    "his email is tom@pacificwine.com"
)
THREAD_ID = "cli_test_001"
CONFIG = {"configurable": {"thread_id": THREAD_ID}}


def run(message: str):
    print(f"\n[→] {message}\n")

    result = invoice_graph.invoke(
        {"raw_message": message, "sender_id": "cecil"},
        config=CONFIG,
    )

    # Drive through interrupts interactively
    while True:
        if result.get("final_response") and not _is_interrupted(result):
            print(f"\n[✓] {result['final_response']}\n")
            break

        # Determine which interrupt we're at by checking what's missing
        if result.get("missing_fields"):
            print(f"\n[?] Missing: {', '.join(result['missing_fields'])}")
            reply = input("Provide missing info: ").strip()
            result = invoice_graph.invoke(Command(resume=reply), config=CONFIG)

        elif result.get("customer") and not result.get("tier_name"):
            customer = result.get("customer", {})
            print(f"\n[?] Confirm for {customer.get('full_name', 'customer')}:")
            print("    Tier: Wholesale | Corporate | Employee | Club Member | Direct | FOB/Export")
            print("    Format: 'Wholesale, NET_30, CARD+BANK_ACCOUNT'")
            reply = input("Confirm: ").strip()
            result = invoice_graph.invoke(Command(resume=reply), config=CONFIG)

        elif result.get("invoice_preview") and not result.get("square_invoice_id"):
            preview = result["invoice_preview"]
            print(f"\n{preview.get('preview_text', str(preview))}")
            decision = input("\nApprove? [approve/reject]: ").strip().lower()
            decision = "approved" if decision in ("approve", "approved") else "rejected"
            result = invoice_graph.invoke(Command(resume=decision), config=CONFIG)

        elif result.get("square_invoice_id") and not result.get("send_decision"):
            invoice_id = result.get("square_invoice_id")
            customer_name = result.get("customer", {}).get("full_name", "customer")
            preview = result.get("invoice_preview", {})
            total = preview.get("total_before_tax_cents", 0) / 100
            from services.square_service import invoice_dashboard_url
            print(f"\n[DRAFT SAVED] {customer_name} — ${total:.2f}")
            print(f"  Square Invoice ID: {invoice_id}")
            print(f"  Open in Square Dashboard: {invoice_dashboard_url(invoice_id)}")
            decision = input("\nSend to client now? [send/draft]: ").strip().lower()
            decision = "send" if decision == "send" else "draft"
            result = invoice_graph.invoke(Command(resume=decision), config=CONFIG)

        elif result.get("square_invoice_id") and result.get("send_decision") == "send" and not result.get("email_receipt_decision"):
            customer = result.get("customer", {})
            customer_email = customer.get("email") or result.get("extracted", {}).get("email", "")
            if customer_email:
                customer_name = customer.get("full_name") or customer.get("company", "customer")
                print(f"\n[EMAIL RECEIPT] Send receipt email to {customer_name} at {customer_email}?")
                decision = input("Send email? [send/skip]: ").strip().lower()
                decision = "send" if decision == "send" else "skip"
                result = invoice_graph.invoke(Command(resume=decision), config=CONFIG)
            else:
                print("\n[EMAIL] No email on file, skipping receipt.")
                result = invoice_graph.invoke(Command(resume="skip"), config=CONFIG)

        else:
            # No interrupt to handle — something unexpected
            print(f"\n[✓] {result.get('final_response', 'Done.')}\n")
            break


def _is_interrupted(result: dict) -> bool:
    """Check if the graph is paused at an interrupt (not yet complete)."""
    return (
        bool(result.get("missing_fields"))
        or (result.get("customer") and not result.get("tier_name"))
        or (result.get("invoice_preview") and not result.get("square_invoice_id"))
        or (result.get("square_invoice_id") and not result.get("send_decision"))
        or (result.get("send_decision") == "send" and not result.get("email_receipt_decision"))
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    args = parser.parse_args()
    run(args.message)
