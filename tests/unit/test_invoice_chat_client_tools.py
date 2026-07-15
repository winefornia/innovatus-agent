"""Tests for the invoice chat agent's client-knowledge tools
(vertex_agent.invoice_chat_actions: client_lookup / client_history /
usual_order / client_notes).

Covers:
  - profile resolution via customer_service (name vs email routing)
  - history merges synced Square invoices with their orders' line items
  - fallback to square_orders when square_invoices has no rows (the sync's
    invoice half was broken for a stretch; orders kept syncing)
  - usual_order re-prices the remembered items at the customer's tier, and
    degrades gracefully with no tier / no past order
  - client_notes searches Mem0 under both user-id prefixes and prepends the
    profile note
  - all tools are read-only: nothing is staged in the pending store

All Supabase/Mem0/customer access is stubbed — hermetic.
"""

import pytest

import vertex_agent.invoice_chat_actions as ica


@pytest.fixture(autouse=True)
def _clear_pending():
    ica._PENDING.clear()
    ica.set_current_user("gchat_cecil@winefornia.com")
    yield
    ica._PENDING.clear()


_CUSTOMER = {
    "id": "uuid-1",
    "square_customer_id": "sq_cust_1",
    "full_name": "Christina Lee",
    "company": "Oak Barrel Restaurant",
    "email": "christina@oakbarrel.com",
    "phone": "555-0100",
    "tier_name": "Wholesale",
    "customer_type": "restaurant",
    "notes": "Prefers morning deliveries",
}


def _mock_lookup(mocker, result=None):
    return mocker.patch(
        "services.customer_service.lookup_customer",
        return_value=result if result is not None else {"match": "fuzzy_name", "customer": dict(_CUSTOMER)},
    )


# ── client_lookup ─────────────────────────────────────────────────────────────

class TestClientLookup:
    def test_profile_returned(self, mocker):
        _mock_lookup(mocker)
        out = ica.client_lookup("christina")
        assert out["found"] is True
        assert out["profile"]["tier"] == "Wholesale"
        assert out["profile"]["email"] == "christina@oakbarrel.com"

    def test_email_query_routes_to_email_lookup(self, mocker):
        lookup = _mock_lookup(mocker)
        ica.client_lookup("christina@oakbarrel.com")
        assert lookup.call_args.kwargs.get("email") == "christina@oakbarrel.com"

    def test_name_query_routes_to_name_and_company(self, mocker):
        lookup = _mock_lookup(mocker)
        ica.client_lookup("Oak Barrel")
        assert lookup.call_args.kwargs.get("name") == "Oak Barrel"
        assert lookup.call_args.kwargs.get("company") == "Oak Barrel"

    def test_unknown_customer(self, mocker):
        _mock_lookup(mocker, {"match": "none"})
        out = ica.client_lookup("nobody")
        assert out["found"] is False
        assert "nobody" in out["hint"]

    def test_read_only(self, mocker):
        _mock_lookup(mocker)
        ica.client_lookup("christina")
        assert ica.peek_pending(ica._user()) is None


# ── client_history ────────────────────────────────────────────────────────────

class TestClientHistory:
    def test_merges_invoices_with_order_items(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("db.repository.list_square_invoices_for_customer", return_value=[{
            "square_invoice_id": "inv_1", "square_order_id": "ord_1",
            "invoice_number": "202468", "status": "PAID",
            "total_money_cents": 105600, "paid_at": "2026-05-02T10:00:00",
            "invoice_created_at": "2026-05-01T10:00:00",
        }])
        mocker.patch("db.repository.get_square_orders_by_ids", return_value=[{
            "square_order_id": "ord_1",
            "line_items": [{"name": "Cabernet Franc 2021", "quantity": "36"}],
        }])
        mocker.patch("db.repository.list_invoice_logs_for_customer", return_value=[])

        out = ica.client_history("christina")

        assert out["found"] is True
        assert out["square_history"][0]["invoice_number"] == "202468"
        assert out["square_history"][0]["total"] == "$1,056.00"
        assert "Cabernet Franc 2021" in out["square_history"][0]["items"]
        assert out["summary"]["paid"] == 1

    def test_falls_back_to_orders_when_no_invoices(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("db.repository.list_square_invoices_for_customer", return_value=[])
        mocker.patch("db.repository.get_square_orders_by_ids", return_value=[])
        mocker.patch("db.repository.list_square_orders_for_customer", return_value=[{
            "square_order_id": "ord_2", "state": "COMPLETED",
            "total_money_cents": 43200, "order_created_at": "2026-04-01T10:00:00",
            "line_items": [{"name": "Viognier 2023", "quantity": "12"}],
        }])
        mocker.patch("db.repository.list_invoice_logs_for_customer", return_value=[])

        out = ica.client_history("christina")

        assert out["square_history"][0]["order_state"] == "COMPLETED"
        assert "Viognier 2023" in out["square_history"][0]["items"]

    def test_includes_recent_agent_invoices(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("db.repository.list_square_invoices_for_customer", return_value=[])
        mocker.patch("db.repository.get_square_orders_by_ids", return_value=[])
        mocker.patch("db.repository.list_square_orders_for_customer", return_value=[])
        mocker.patch("db.repository.list_invoice_logs_for_customer", return_value=[{
            "customer_name": "Christina Lee", "tier_name": "Wholesale",
            "total_before_tax_cents": 88000, "approval": "approved",
            "verification_status": "paid_confirmed", "square_invoice_number": "202470",
            "line_items": [{"product_name": "Cabernet Franc", "vintage": 2021,
                            "quantity": 3, "unit_type": "case"}],
            "created_at": "2026-06-01T10:00:00",
        }])

        out = ica.client_history("christina")

        agent_inv = out["recent_agent_invoices"][0]
        assert agent_inv["verification"] == "paid_confirmed"
        assert "3× 2021 Cabernet Franc" in agent_inv["items"]

    def test_unknown_customer(self, mocker):
        _mock_lookup(mocker, {"match": "none"})
        out = ica.client_history("nobody")
        assert out["found"] is False

    def test_db_errors_reported_not_raised(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("db.repository.list_square_invoices_for_customer",
                     side_effect=RuntimeError("db down"))
        mocker.patch("db.repository.list_invoice_logs_for_customer",
                     side_effect=RuntimeError("db down"))
        out = ica.client_history("christina")
        assert out["found"] is True
        assert "db down" in out["square_history_error"]


# ── usual_order ───────────────────────────────────────────────────────────────

class TestUsualOrder:
    ITEMS = [{"product_name": "Cabernet Franc", "vintage": 2021,
              "quantity": 3, "unit_type": "case"}]

    def test_reprices_at_customer_tier(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.skill_service.skill_service.resolve_reference",
                     return_value=list(self.ITEMS))
        quote = mocker.patch.object(ica, "_quote", return_value={
            "summary": "…", "total": "$3,168.00", "total_cents": 316800})

        out = ica.usual_order("christina")

        assert out["found"] is True
        assert out["items"] == self.ITEMS
        assert out["quote_at_current_prices"]["total"] == "$3,168.00"
        assert quote.call_args.args[0] == "Wholesale"

    def test_no_tier_on_file(self, mocker):
        cust = dict(_CUSTOMER, tier_name=None)
        _mock_lookup(mocker, {"match": "fuzzy_name", "customer": cust})
        mocker.patch("services.skill_service.skill_service.resolve_reference",
                     return_value=list(self.ITEMS))

        out = ica.usual_order("christina")

        assert out["found"] is True
        assert "quote_at_current_prices" not in out
        assert "tier" in out["pricing_note"].lower()

    def test_no_past_order(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.skill_service.skill_service.resolve_reference",
                     return_value=None)
        out = ica.usual_order("christina")
        assert out["found"] is False
        assert "no past order" in out["hint"].lower()

    def test_read_only(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.skill_service.skill_service.resolve_reference",
                     return_value=list(self.ITEMS))
        mocker.patch.object(ica, "_quote", return_value={"total": "$1"})
        ica.usual_order("christina")
        assert ica.peek_pending(ica._user()) is None


# ── client_notes ──────────────────────────────────────────────────────────────

class TestClientNotes:
    def test_searches_both_user_id_prefixes(self, mocker):
        _mock_lookup(mocker, {"match": "none"})
        load = mocker.patch("services.skill_service.skill_service.load_skills",
                            return_value=["Oak Barrel always orders NET_30"])
        out = ica.client_notes("Oak Barrel")
        searched = {c.kwargs["user_id"] for c in load.call_args_list}
        assert searched == {"gchat_cecil@winefornia.com", "gc_cecil@winefornia.com"}
        assert "Oak Barrel always orders NET_30" in out["notes"]

    def test_profile_note_prepended(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.skill_service.skill_service.load_skills",
                     return_value=["mem0 fact"])
        out = ica.client_notes("christina")
        assert out["notes"][0] == "(profile note) Prefers morning deliveries"
        assert "mem0 fact" in out["notes"]

    def test_dedupes_across_user_ids(self, mocker):
        _mock_lookup(mocker, {"match": "none"})
        mocker.patch("services.skill_service.skill_service.load_skills",
                     return_value=["same fact"])
        out = ica.client_notes("Oak Barrel")
        assert out["notes"].count("same fact") == 1

    def test_blank_query(self):
        assert ica.client_notes("") == {"notes": []}
