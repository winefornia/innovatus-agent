"""Tests for the invoice chat-action layer (vertex_agent.invoice_chat_actions).

Covers the safety behaviors that mirror the tasting-room assistant:
  - confirm-first: stage_* records an intent and does NOT mutate; the real
    mutation only fires on confirm_pending_action()
  - cancel_pending_action() discards a staged action without mutating
  - the per-user pending store is keyed by the acting approver
  - input parsing/validation (currency strings, ranges, payment schedule)
  - pricing-write lockstep: Supabase first; if it fails, JSON is left untouched
  - invoice staging guards (needs-price, email) + Square idempotency keys

All product/Supabase/Square access is stubbed — these tests are hermetic.
"""

import pytest

import vertex_agent.invoice_chat_actions as ica


@pytest.fixture(autouse=True)
def _clear_pending():
    ica._PENDING.clear()
    ica.set_current_user("gchat_cecil@winefornia.com")
    yield
    ica._PENDING.clear()


def _product(name="Cabernet Franc", vintage=2021, size="750ml"):
    return {
        "name": name, "vintage": vintage, "size": size,
        "msrp_bottle_cents": 12500,
        "tier_prices": {"wholesale": 8800, "fob": 6900},
        "tier_unavailable": [],
        "variable_pricing": False,
    }


# ── parsing / validation helpers ─────────────────────────────────────────────

class TestParsing:
    def test_amount_to_cents_currency_string(self):
        assert ica._amount_to_cents("$58") == 5800
        assert ica._amount_to_cents("1,200.50") == 120050
        assert ica._amount_to_cents(58) == 5800

    def test_amount_to_cents_rejects_garbage(self):
        assert ica._amount_to_cents("") is None
        assert ica._amount_to_cents("abc") is None
        assert ica._amount_to_cents(None) is None

    def test_to_float_handles_percent(self):
        assert ica._to_float("25%") == 25.0
        assert ica._to_float("0.7") == 0.7

    def test_norm_schedule(self):
        assert ica._norm_schedule("net 30") == "NET_30"
        assert ica._norm_schedule("NET_7") == "NET_7"
        assert ica._norm_schedule("garbage") == "NET_30"


# ── confirm-first: channel price edit ────────────────────────────────────────

class TestStageChannelPrice:
    def test_stage_does_not_write(self, mocker):
        mocker.patch.object(ica, "_resolve_product", return_value={"product": _product()})
        apply = mocker.patch.object(ica, "_apply_product")

        out = ica.stage_set_channel_price("cabernet franc", "wholesale", "$90", 2021)

        apply.assert_not_called()
        assert "yes" in out.lower()
        assert ica.peek_pending(ica._user())["kind"] == "set_channel_price"
        assert ica.peek_pending(ica._user())["params"]["cents"] == 9000

    def test_confirm_writes(self, mocker):
        mocker.patch.object(ica, "_resolve_product", return_value={"product": _product()})
        apply = mocker.patch.object(ica, "_apply_product", return_value="Done ✅")

        ica.stage_set_channel_price("cabernet franc", "wholesale", 90, 2021)
        out = ica.confirm_pending_action()

        apply.assert_called_once()
        assert "done" in out.lower()
        assert ica.peek_pending(ica._user()) is None

    def test_cancel_discards_without_writing(self, mocker):
        mocker.patch.object(ica, "_resolve_product", return_value={"product": _product()})
        apply = mocker.patch.object(ica, "_apply_product")

        ica.stage_set_channel_price("cabernet franc", "wholesale", 90, 2021)
        out = ica.cancel_pending_action()

        apply.assert_not_called()
        assert ica.peek_pending(ica._user()) is None
        assert "left it" in out.lower()

    def test_bad_price_rejected(self, mocker):
        rp = mocker.patch.object(ica, "_resolve_product")
        out = ica.stage_set_channel_price("cabernet franc", "wholesale", "abc", 2021)
        assert "doesn't look right" in out.lower()
        rp.assert_not_called()  # bails before resolving
        assert ica.peek_pending(ica._user()) is None

    def test_invalid_channel_rejected(self):
        out = ica.stage_set_channel_price("cabernet franc", "retail", 50, 2021)
        assert "isn't a channel" in out.lower()
        assert ica.peek_pending(ica._user()) is None

    def test_ambiguous_product_asks(self, mocker):
        mocker.patch.object(ica, "_resolve_product", return_value={"ambiguous": [
            {"name": "Cabernet Sauvignon", "vintage": 2021, "size": "750ml"},
            {"name": "Cabernet Franc", "vintage": 2021, "size": "750ml"},
        ]})
        out = ica.stage_set_channel_price("cabernet", "wholesale", 90, 2021)
        assert "which one" in out.lower()
        assert ica.peek_pending(ica._user()) is None


# ── tier edit validation ─────────────────────────────────────────────────────

class TestStageTier:
    def test_discount_over_100_rejected(self, mocker):
        mocker.patch.object(ica, "_resolve_tier", return_value={"tier": {"name": "Wholesale",
                            "discount_percent": 30, "msrp_multiplier": 0.7}})
        assert "can't exceed 100" in ica.stage_set_tier("Wholesale", 150, -1)

    def test_multiplier_out_of_range_rejected(self, mocker):
        mocker.patch.object(ica, "_resolve_tier", return_value={"tier": {"name": "Wholesale",
                            "discount_percent": 30, "msrp_multiplier": 0.7}})
        assert "between 0 and 2" in ica.stage_set_tier("Wholesale", -1, 5)

    def test_unknown_tier_rejected(self, mocker):
        mocker.patch.object(ica, "_resolve_tier", return_value={"error": "I don't recognize the tier"})
        assert "don't recognize" in ica.stage_set_tier("Platinum", 10, -1)

    def test_sentinel_leaves_field_unchanged(self, mocker):
        mocker.patch.object(ica, "_resolve_tier", return_value={"tier": {"name": "Corporate",
                            "discount_percent": 20, "msrp_multiplier": 0.8}})
        ica.stage_set_tier("Corporate", 25, -1)
        params = ica.peek_pending(ica._user())["params"]
        assert params["fields"] == {"discount_percent": 25.0}


# ── per-user pending store ───────────────────────────────────────────────────

def test_pending_is_keyed_by_user(mocker):
    mocker.patch.object(ica, "_resolve_product", return_value={"product": _product()})
    ica.set_current_user("gchat_cecil@winefornia.com")
    ica.stage_set_channel_price("cabernet franc", "wholesale", 90, 2021)
    ica.set_current_user("gchat_lisa@winefornia.com")
    assert ica.peek_pending("gchat_lisa@winefornia.com") is None
    assert ica.peek_pending("gchat_cecil@winefornia.com") is not None


# ── invoice staging guards + idempotency ─────────────────────────────────────

class TestStageInvoice:
    _GOOD_QUOTE = {
        "summary": "• 1 case Cabernet Franc 2021 @ $88.00/btl = $1,056.00",
        "line_items": [{"product_name": "Cabernet Franc", "quantity": 1, "unit_type": "case",
                        "final_unit_price_cents": 8800, "bottles_per_case": 12}],
        "total": "$1,056.00", "total_cents": 105600, "blocks": [], "needs_price": [],
    }

    def test_needs_price_blocks_staging(self, mocker):
        mocker.patch.object(ica, "_quote", return_value={**self._GOOD_QUOTE,
                            "needs_price": [{"label": "Cab Sauv (shiners) 2021"}]})
        out = ica.stage_invoice("Acme", "a@acme.com", "Wholesale", "[]", "NET_30", 0, False)
        assert "need a price" in out.lower()
        assert ica.peek_pending(ica._user()) is None

    def test_missing_email_blocks_staging(self, mocker):
        mocker.patch.object(ica, "_quote", return_value=self._GOOD_QUOTE)
        out = ica.stage_invoice("Acme", "", "Wholesale", "[]", "NET_30", 0, False)
        assert "email" in out.lower()
        assert ica.peek_pending(ica._user()) is None

    def test_missing_shipping_blocks_staging(self, mocker):
        mocker.patch.object(ica, "_quote", return_value=self._GOOD_QUOTE)
        out = ica.stage_invoice("Acme", "a@acme.com", "Wholesale", "[]", "NET_30")
        assert "shipping" in out.lower()
        assert "free" in out.lower()
        assert ica.peek_pending(ica._user()) is None

    def test_stage_normalizes_schedule_and_adds_idem(self, mocker):
        mocker.patch.object(ica, "_quote", return_value=self._GOOD_QUOTE)
        ica.stage_invoice("Acme", "a@acme.com", "Wholesale", "[]", "net 30", 30, True)
        params = ica.peek_pending(ica._user())["params"]
        assert params["payment_schedule"] == "NET_30"
        assert params["shipping_cents"] == 3000
        assert params["send"] is True
        assert len(params["idem"]) >= 16

    def test_confirm_creates_invoice_with_idempotency_keys(self, mocker):
        mocker.patch.object(ica, "_quote", return_value=self._GOOD_QUOTE)
        cust = mocker.patch("services.square_service.get_or_create_square_customer",
                            return_value={"customer_id": "C1"})
        order = mocker.patch("services.square_service.create_order",
                             return_value={"order_id": "O1"})
        draft = mocker.patch("services.square_service.create_invoice_draft",
                             return_value={"invoice_id": "I1", "invoice_version": 0,
                                           "invoice_number": "0001"})
        pub = mocker.patch("services.square_service.publish_invoice",
                           return_value={"status": "published", "public_url": "https://sq/inv"})
        mocker.patch.object(ica, "_log_invoice_best_effort")

        ica.stage_invoice("Acme", "a@acme.com", "Wholesale", "[]", "NET_30", 30, True)
        out = ica.confirm_pending_action()

        # all four Square calls carry a deterministic idempotency key
        assert cust.call_args.kwargs["idempotency_key"]
        assert order.call_args.kwargs["idempotency_key"]
        assert order.call_args.kwargs["shipping_cents"] == 3000
        assert draft.call_args.kwargs["idempotency_key"]
        assert pub.call_args.kwargs["idempotency_key"]
        assert "sent" in out.lower()
        assert "https://sq/inv" in out


# ── pricing-write lockstep orchestration ─────────────────────────────────────

class TestApplyProduct:
    def test_supabase_first_then_json(self, mocker):
        mocker.patch.object(ica, "_update_product_supabase", return_value=(True, "ok"))
        mocker.patch.object(ica, "_update_product_json", return_value=(True, "ok"))
        out = ica._apply_product("Cab", 2021, "750ml", {"msrp_bottle_cents": 100}, lambda e: None, "MSRP")
        assert "supabase + catalog json" in out.lower()

    def test_supabase_failure_leaves_json_untouched(self, mocker):
        mocker.patch.object(ica, "_update_product_supabase", return_value=(False, "column missing"))
        json_write = mocker.patch.object(ica, "_update_product_json")
        out = ica._apply_product("Cab", 2021, "750ml", {"x": 1}, lambda e: None, "MSRP")
        json_write.assert_not_called()  # no drift introduced
        assert "left the json alone" in out.lower()
        assert "nothing changed" in out.lower()

    def test_json_failure_warns_of_drift(self, mocker):
        mocker.patch.object(ica, "_update_product_supabase", return_value=(True, "ok"))
        mocker.patch.object(ica, "_update_product_json", return_value=(False, "no matching JSON entry"))
        out = ica._apply_product("Cab", 2021, "750ml", {"x": 1}, lambda e: None, "MSRP")
        assert "may drift" in out.lower()


# ── confirm with nothing staged ──────────────────────────────────────────────

def test_confirm_with_nothing_pending():
    assert "nothing waiting" in ica.confirm_pending_action().lower()


# ── send an existing draft (reopen a case later) ──────────────────────────────

class TestStageSendInvoice:
    """"send Christina's invoice" days after drafting: the draft is found in the
    durable invoice log + Square, staged confirm-first, and published on yes —
    so a finished case can be reopened toward its final SENT state even after a
    redeploy wiped the in-process conversation memory."""

    def _draft(self, status="DRAFT", version=3):
        return {"invoice_id": "inv_9", "invoice_number": "WF-0009", "version": version,
                "status": status, "public_url": "https://sq/pay/inv_9",
                "total_money_cents": 79500}

    def test_stages_from_customer_name(self, mocker):
        mocker.patch("db.repository.get_recent_invoice_for_customer",
                     return_value={"square_invoice_id": "inv_9"})
        mocker.patch("services.square_service.get_invoice", return_value=self._draft())
        out = ica.stage_send_invoice(customer_name="Christina Yoo")
        assert "SEND invoice" in out and "WF-0009" in out and "$795.00" in out
        assert ica._PENDING  # staged, not executed
        assert ica._PENDING[ica._user()]["kind"] == "send_existing"

    def test_no_draft_found_asks_instead_of_staging(self, mocker):
        mocker.patch("db.repository.get_recent_invoice_for_customer", return_value=None)
        out = ica.stage_send_invoice(customer_name="Nobody")
        assert "couldn't find" in out.lower()
        assert not ica._PENDING

    def test_no_name_or_number_asks(self):
        out = ica.stage_send_invoice()
        assert "customer name" in out.lower()
        assert not ica._PENDING

    def test_already_sent_reports_instead_of_staging(self, mocker):
        mocker.patch("services.square_service.get_invoice",
                     return_value=self._draft(status="UNPAID"))
        out = ica.stage_send_invoice(invoice_number="inv_9")
        assert "already sent" in out.lower()
        assert not ica._PENDING

    def test_confirm_publishes_with_current_version(self, mocker):
        mocker.patch("db.repository.get_recent_invoice_for_customer",
                     return_value={"square_invoice_id": "inv_9"})
        mocker.patch("services.square_service.get_invoice", return_value=self._draft(version=7))
        publish = mocker.patch("services.square_service.publish_invoice",
                               return_value={"status": "published", "invoice_id": "inv_9",
                                             "public_url": "https://sq/pay/inv_9"})
        ica.stage_send_invoice(customer_name="Christina Yoo")
        out = ica.confirm_pending_action()
        assert "Sent" in out and "WF-0009" in out
        assert publish.call_args.kwargs.get("invoice_version") == 7 or publish.call_args.args[1:2] == (7,)
        assert not ica._PENDING  # consumed

    def test_confirm_when_draft_vanished_is_safe(self, mocker):
        mocker.patch("services.square_service.get_invoice",
                     side_effect=[self._draft(), self._draft(status="CANCELED")])
        ica.stage_send_invoice(invoice_number="inv_9")
        out = ica.confirm_pending_action()
        assert "nothing sent" in out.lower()
