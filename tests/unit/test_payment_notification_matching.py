"""Square payment notifications must never invent or feed tasting cases.

The tasting mailbox receives Square notifications for EVERY Winefornia invoice —
wine orders included. Name-alone matching glued wine buyers' invoice emails to
tasting cases (phantom reservations: Christina Yoo #202468, John Nicastro
#202463-65, June 2026 — created pre-guard, then fed for weeks). These tests pin
the attach rules in find_or_create_reservation:

  - a payment notification attaches ONLY when a case is expecting that money
    (recorded deposit-invoice number, or a payment-state case with an invoice
    on file matched by client identity)
  - otherwise it matches nothing → the intake guard quarantines it
  - Square-sender emails count as payment notifications even when classified
    "unclassified"
  - terminal (cancelled/confirmed) cases never swallow new mail by name
"""

import pytest

import services.tastingroom_service as trs


def _case(rid, name, state, invoice_number=None, invoice_id=None, email=None):
    return {"reservation_id": rid, "client_name": name, "client_email": email,
            "current_state": state, "square_invoice_number": invoice_number,
            "square_invoice_id": invoice_id, "candidate_slots": [], "active_slot": {}}


@pytest.fixture
def repo(mocker):
    """Stub the reservation repository; tests fill `rows`."""
    rows = []
    mocker.patch("db.repository.find_recent_reservations",
                 side_effect=lambda *a, **k: rows if not a or a[0] is None else [])
    mocker.patch("db.repository.find_reservation_by_thread", return_value=None)
    mocker.patch("db.repository.get_reservation", return_value=None)
    return rows


def _resolve(subject, facts, thread=""):
    return trs.find_or_create_reservation(
        gmail_thread_id=thread, subject=subject, facts=facts)


class TestWineOrderNoiseIsQuarantined:
    def test_wine_invoice_email_never_name_matches_a_tasting_case(self, repo):
        # Christina has an open (zombie-like) tasting case with NO deposit invoice.
        repo.append(_case("TASTING-X-CHRISTINA-YOO", "Christina Yoo", "PAYMENT_RECEIVED"))
        rid, existing = _resolve(
            "A new invoice was created for Christina Yoo (#202468)",
            {"message_type": "invoice_payment_message", "client_name": "Christina Yoo",
             "sender_email": "invoicing@messaging.squareup.com"})
        assert existing is None            # unmatched → intake guard quarantines
        assert rid != "TASTING-X-CHRISTINA-YOO"

    def test_square_sender_unclassified_is_treated_as_payment_noise(self, repo):
        repo.append(_case("TASTING-X-CHRISTINA-YOO", "Christina Yoo", "PAYMENT_RECEIVED"))
        rid, existing = _resolve(
            "Your Square sales summary",
            {"message_type": "unclassified", "client_name": "Christina Yoo",
             "sender_email": "invoicing@messaging.squareup.com"})
        assert existing is None

    def test_non_payment_client_reply_still_name_matches_open_case(self, repo):
        repo.append(_case("TASTING-X-MIRA-PARK", "Mira Park", "SLOT_OFFERED_TO_CLIENT"))
        rid, existing = _resolve(
            "Re: your tasting visit",
            {"message_type": "client_acceptance", "client_name": "Mira Park",
             "sender_email": "mirasopa@gmail.com"})
        assert existing is not None
        assert rid == "TASTING-X-MIRA-PARK"


class TestExpectedDepositPaymentsAttach:
    def test_invoice_number_match_attaches(self, repo):
        repo.append(_case("TASTING-X-MIRA-PARK", "Mira Park", "INVOICE_SENT",
                          invoice_number="202447"))
        rid, existing = _resolve(
            "Payment processed: Invoice #202447 to Mira Park",
            {"message_type": "invoice_payment_message", "client_name": "Mira Park",
             "sender_email": "invoicing@messaging.squareup.com"})
        assert existing is not None
        assert rid == "TASTING-X-MIRA-PARK"

    def test_identity_match_requires_payment_state_and_invoice_on_file(self, repo):
        repo.append(_case("TASTING-X-MIRA-PARK", "Mira Park", "WAITING_FOR_PAYMENT",
                          invoice_id="inv_dep_1", email="mirasopa@gmail.com"))
        rid, existing = _resolve(
            "An invoice was paid by Mira Park!",
            {"message_type": "invoice_payment_message", "client_name": "Mira Park",
             "client_email": "mirasopa@gmail.com",
             "sender_email": "invoicing@messaging.squareup.com"})
        assert existing is not None
        assert rid == "TASTING-X-MIRA-PARK"

    def test_identity_match_refused_when_case_not_expecting_payment(self, repo):
        # Same client, but the case is still negotiating a slot — a Square payment
        # email naming her is wine-order noise, not the tasting deposit.
        repo.append(_case("TASTING-X-MIRA-PARK", "Mira Park", "SLOT_OFFERED_TO_CLIENT",
                          email="mirasopa@gmail.com"))
        rid, existing = _resolve(
            "An invoice was paid by Mira Park!",
            {"message_type": "invoice_payment_message", "client_name": "Mira Park",
             "client_email": "mirasopa@gmail.com",
             "sender_email": "invoicing@messaging.squareup.com"})
        assert existing is None


class TestTerminalCasesStayClosed:
    def test_named_match_skips_cancelled_cases(self, repo):
        repo.append(_case("TASTING-X-OLD", "Mira Park", "CANCELLED_OR_DEFERRED"))
        assert trs._find_named_reservation({"client_name": "Mira Park"}) is None

    def test_payment_match_skips_terminal_even_with_number(self, repo):
        repo.append(_case("TASTING-X-OLD", "Mira Park", "CANCELLED_OR_DEFERRED",
                          invoice_number="202447"))
        rid, existing = _resolve(
            "Payment processed: Invoice #202447 to Mira Park",
            {"message_type": "invoice_payment_message", "client_name": "Mira Park",
             "sender_email": "invoicing@messaging.squareup.com"})
        assert existing is None
