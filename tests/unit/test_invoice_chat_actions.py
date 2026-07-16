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
        from services.tool_registry import tool_registry
        dispatch = mocker.spy(tool_registry, "dispatch")
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

        assert [call.args[0] for call in dispatch.call_args_list] == [
            "square_create_customer",
            "square_create_order",
            "square_create_invoice_draft",
            "square_publish_invoice",
        ]
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
        from services.tool_registry import tool_registry
        dispatch = mocker.spy(tool_registry, "dispatch")
        mocker.patch("services.square_service.get_invoice", return_value=self._draft(version=7))
        publish = mocker.patch("services.square_service.publish_invoice",
                               return_value={"status": "published", "invoice_id": "inv_9",
                                             "public_url": "https://sq/pay/inv_9"})
        ica.stage_send_invoice(customer_name="Christina Yoo")
        out = ica.confirm_pending_action()
        assert "Sent" in out and "WF-0009" in out
        assert [call.args[0] for call in dispatch.call_args_list] == [
            "square_get_invoice",
            "square_publish_invoice",
        ]
        assert publish.call_args.kwargs.get("invoice_version") == 7 or publish.call_args.args[1:2] == (7,)
        assert not ica._PENDING  # consumed

    def test_confirm_when_draft_vanished_is_safe(self, mocker):
        mocker.patch("services.square_service.get_invoice",
                     side_effect=[self._draft(), self._draft(status="CANCELED")])
        ica.stage_send_invoice(invoice_number="inv_9")
        out = ica.confirm_pending_action()
        assert "nothing sent" in out.lower()


class TestGetInvoiceLink:
    """"show me the link of the draft" must return the Square Dashboard URL —
    drafts have no public payment link, and the agent used to refuse
    (production gap seen 2026-07-15). Read-only: never stages anything."""

    DASH = "https://app.squareup.com/dashboard/invoices/inv_9"

    def _inv(self, status="DRAFT", public_url=None):
        return {"invoice_id": "inv_9", "invoice_number": "WF-0009",
                "status": status, "public_url": public_url,
                "total_money_cents": 79500}

    def test_draft_returns_dashboard_edit_link(self, mocker):
        mocker.patch("db.repository.get_recent_invoice_for_customer",
                     return_value={"square_invoice_id": "inv_9"})
        mocker.patch("services.square_service.get_invoice", return_value=self._inv())
        out = ica.get_invoice_link(customer_name="Chang Lim")
        assert self.DASH in out and "DRAFT" in out and "WF-0009" in out
        assert not ica._PENDING  # read-only

    def test_sent_invoice_returns_payment_and_dashboard_links(self, mocker):
        mocker.patch("services.square_service.get_invoice",
                     return_value=self._inv(status="UNPAID",
                                            public_url="https://sq/pay/inv_9"))
        out = ica.get_invoice_link(invoice_number="inv_9")
        assert "https://sq/pay/inv_9" in out and self.DASH in out

    def test_square_error_still_gives_dashboard_link(self, mocker):
        mocker.patch("db.repository.get_recent_invoice_for_customer",
                     return_value={"square_invoice_id": "inv_9"})
        mocker.patch("services.square_service.get_invoice",
                     return_value={"error": "Square timeout"})
        out = ica.get_invoice_link(customer_name="Chang Lim")
        assert self.DASH in out and "couldn't verify" in out.lower()

    def test_no_name_or_number_asks(self):
        out = ica.get_invoice_link()
        assert "customer name" in out.lower()

    def test_unknown_customer_says_not_found(self, mocker):
        mocker.patch("db.repository.get_recent_invoice_for_customer",
                     return_value=None)
        out = ica.get_invoice_link(customer_name="Nobody")
        assert "couldn't find" in out.lower()

    def test_recent_invoices_include_dashboard_url(self, mocker):
        mocker.patch("db.repository.list_recent_invoices",
                     return_value=[{"customer_name": "Chang Lim",
                                    "square_invoice_id": "inv_9"}])
        rows = ica.recent_invoices()
        assert rows[0]["dashboard_url"] == self.DASH


class TestInvoiceNumberResolution:
    """Staff paste back the number the bot itself displays ("Invoice #202471"),
    but Square's API only accepts the opaque invoice id ("inv:0-…") and 404s on
    the number. Production 2026-07-15: the bot printed #202471, staff pasted it
    back, and the bot claimed the invoice might not exist. Numeric refs must
    resolve through the durable copies (invoice_logs, then synced
    square_invoices) so the number staff see in chat is a working handle."""

    SQ_ID = "inv:0-ChCHabc123"
    DASH = f"https://app.squareup.com/dashboard/invoices/{SQ_ID}"

    def _inv(self, status="DRAFT"):
        return {"invoice_id": self.SQ_ID, "invoice_number": "202471",
                "status": status, "version": 2, "public_url": None,
                "total_money_cents": 167548}

    def test_display_number_resolves_via_invoice_log(self, mocker):
        mocker.patch("db.repository.find_invoice_log_by_number",
                     return_value={"square_invoice_id": self.SQ_ID})
        mocker.patch("services.square_service.get_invoice", return_value=self._inv())
        out = ica.get_invoice_link(customer_name="Chang Lim", invoice_number="#202471")
        assert self.DASH in out and "202471" in out and "DRAFT" in out

    def test_send_stages_from_display_number(self, mocker):
        mocker.patch("db.repository.find_invoice_log_by_number",
                     return_value={"square_invoice_id": self.SQ_ID})
        mocker.patch("services.square_service.get_invoice", return_value=self._inv())
        out = ica.stage_send_invoice(customer_name="Chang Lim", invoice_number="202471")
        assert "SEND invoice" in out and "202471" in out and "$1,675.48" in out
        assert ica._PENDING[ica._user()]["params"]["invoice_id"] == self.SQ_ID

    def test_number_falls_back_to_synced_square_invoices(self, mocker):
        mocker.patch("db.repository.find_invoice_log_by_number", return_value=None)
        mocker.patch("db.repository.find_square_invoice_by_number",
                     return_value={"square_invoice_id": self.SQ_ID})
        mocker.patch("services.square_service.get_invoice", return_value=self._inv())
        out = ica.get_invoice_link(invoice_number="202471")
        assert self.DASH in out

    def test_send_never_substitutes_for_an_explicit_number(self, mocker):
        # Staff named #202471; if it matches nothing we must NOT stage the
        # customer's most recent draft instead.
        mocker.patch("db.repository.find_invoice_log_by_number", return_value=None)
        mocker.patch("db.repository.find_square_invoice_by_number", return_value=None)
        recent = mocker.patch("db.repository.get_recent_invoice_for_customer")
        out = ica.stage_send_invoice(customer_name="Chang Lim", invoice_number="202471")
        assert "couldn't match" in out.lower()
        recent.assert_not_called()
        assert not ica._PENDING

    def test_link_falls_back_to_customer_with_a_note(self, mocker):
        # Read-only path: an unmatched number degrades to the customer's most
        # recent invoice, flagged so staff can tell.
        mocker.patch("db.repository.find_invoice_log_by_number", return_value=None)
        mocker.patch("db.repository.find_square_invoice_by_number", return_value=None)
        mocker.patch("db.repository.get_recent_invoice_for_customer",
                     return_value={"square_invoice_id": self.SQ_ID,
                                   "square_invoice_number": "202471"})
        mocker.patch("services.square_service.get_invoice", return_value=self._inv())
        out = ica.get_invoice_link(customer_name="Chang Lim", invoice_number="999999")
        assert self.DASH in out and "most recent" in out

    def test_square_id_passes_through_without_lookup(self, mocker):
        log_lookup = mocker.patch("db.repository.find_invoice_log_by_number")
        mocker.patch("services.square_service.get_invoice", return_value=self._inv())
        out = ica.get_invoice_link(invoice_number=self.SQ_ID)
        assert self.DASH in out
        log_lookup.assert_not_called()
