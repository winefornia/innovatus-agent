"""Tests for the invoice chat agent's durable recall + situational edits:

  - invoice_chat_memory: turns persist to Supabase best-effort, rehydrate on a
    cache miss (restart), respect the TTL window, and trip a circuit breaker
    when the DB is down instead of failing every turn
  - past_conversations: months-later search over the durable transcript
  - client_reservations: read-only view of tasting-room bookings
  - stage_update_client: confirm-first profile edits (tier validated, note
    appended, nothing written until confirm)

Hermetic — the durable layer is stubbed (conftest autouse) and re-patched here.
"""

import pytest

import vertex_agent.invoice_chat_actions as ica
import vertex_agent.invoice_chat_memory as icm


@pytest.fixture(autouse=True)
def _clean():
    ica._PENDING.clear()
    ica.set_current_user("gchat_cecil@winefornia.com")
    icm.forget_case("space|user")
    yield
    ica._PENDING.clear()
    icm.forget_case("space|user")


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


# ── durable chat memory ───────────────────────────────────────────────────────

class TestDurableMemory:
    def test_record_turn_persists(self, mocker):
        insert = mocker.patch("db.repository.insert_chat_turn")
        icm.record_turn("space|user", "staff", "invoice Oak Barrel for 3 cases")
        insert.assert_called_once_with("space|user", "staff", "invoice Oak Barrel for 3 cases")

    def test_render_rehydrates_after_cache_miss(self, mocker):
        mocker.patch("db.repository.list_chat_turns_for_case", return_value=[
            {"role": "staff", "text": "invoice Oak Barrel for 3 cases",
             "created_at": "2099-01-01T00:00:00+00:00"},
            {"role": "assistant", "text": "That's $3,168 at Wholesale — stage it?",
             "created_at": "2099-01-01T00:00:05+00:00"},
        ])
        # nothing in the in-process cache for this key (simulated restart)
        out = icm.render_case("space|user")
        assert "invoice Oak Barrel for 3 cases" in out
        assert "stage it?" in out

    def test_rehydrate_skips_stale_turns(self, mocker):
        mocker.patch("db.repository.list_chat_turns_for_case", return_value=[
            {"role": "staff", "text": "an order from months ago",
             "created_at": "2020-01-01T00:00:00+00:00"},
        ])
        assert icm.render_case("space|user") == ""

    def test_circuit_breaker_disables_persistence(self, mocker):
        insert = mocker.patch("db.repository.insert_chat_turn",
                              side_effect=RuntimeError("db down"))
        for i in range(icm._PERSIST_MAX_FAILURES + 2):
            icm.record_turn("space|user", "staff", f"msg {i}")
        assert insert.call_count == icm._PERSIST_MAX_FAILURES
        assert icm._persist_broken is True
        # in-memory behavior unaffected
        assert "msg 0" in icm.render_case("space|user")


# ── past_conversations ────────────────────────────────────────────────────────

class TestPastConversations:
    def test_returns_matches(self, mocker):
        mocker.patch("db.repository.search_chat_turns", return_value=[
            {"case_key": "space|user", "role": "staff",
             "text": "what did we quote Christina on the Viognier?",
             "created_at": "2026-05-02T10:00:00+00:00"},
        ])
        out = ica.past_conversations("Christina")
        assert out["matches"][0]["who"] == "staff"
        assert "Viognier" in out["matches"][0]["said"]

    def test_blank_query(self):
        assert ica.past_conversations("") == {"matches": []}

    def test_db_error_reported(self, mocker):
        mocker.patch("db.repository.search_chat_turns",
                     side_effect=RuntimeError("db down"))
        assert "db down" in ica.past_conversations("Christina")["error"]


# ── client_reservations ───────────────────────────────────────────────────────

class TestClientReservations:
    ROW = {
        "reservation_id": "res_1", "client_name": "Christina Lee",
        "client_email": "christina@oakbarrel.com", "requested_date": "2026-08-01",
        "requested_time": "14:00", "guest_count": 4,
        "experience_type": "production_tour", "current_state": "FINAL_CONFIRMED",
        "payment_status": "paid", "booking_status": "confirmed",
        "square_invoice_number": "202471",
    }

    def test_lookup_by_name(self, mocker):
        lister = mocker.patch("db.repository.list_reservations_for_client",
                              return_value=[dict(self.ROW)])
        out = ica.client_reservations("Christina")
        assert lister.call_args.kwargs.get("client_name") == "Christina"
        assert out["found"] is True
        assert out["bookings"][0]["state"] == "FINAL_CONFIRMED"
        assert "read-only" in out["note"].lower()

    def test_lookup_by_email(self, mocker):
        lister = mocker.patch("db.repository.list_reservations_for_client",
                              return_value=[dict(self.ROW)])
        ica.client_reservations("christina@oakbarrel.com")
        assert lister.call_args.kwargs.get("client_email") == "christina@oakbarrel.com"

    def test_none_found(self, mocker):
        mocker.patch("db.repository.list_reservations_for_client", return_value=[])
        out = ica.client_reservations("nobody")
        assert out["found"] is False

    def test_read_only(self, mocker):
        mocker.patch("db.repository.list_reservations_for_client",
                     return_value=[dict(self.ROW)])
        ica.client_reservations("Christina")
        assert ica.peek_pending(ica._user()) is None


# ── stage_update_client ───────────────────────────────────────────────────────

class TestStageUpdateClient:
    def test_stage_does_not_write(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.product_service.get_tier_by_name",
                     return_value={"name": "Club Member"})
        write = mocker.patch("db.repository.update_customer_fields")

        out = ica.stage_update_client("christina", tier="club member")

        write.assert_not_called()
        assert "yes" in out.lower()
        pending = ica.peek_pending(ica._user())
        assert pending["kind"] == "update_client"
        assert pending["params"]["fields"] == {"tier_name": "Club Member"}

    def test_confirm_writes(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.product_service.get_tier_by_name",
                     return_value={"name": "Club Member"})
        write = mocker.patch("db.repository.update_customer_fields", return_value=True)

        ica.stage_update_client("christina", tier="club member")
        out = ica.confirm_pending_action()

        write.assert_called_once_with("uuid-1", {"tier_name": "Club Member"})
        assert "✅" in out

    def test_cancel_discards(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.product_service.get_tier_by_name",
                     return_value={"name": "Club Member"})
        write = mocker.patch("db.repository.update_customer_fields")

        ica.stage_update_client("christina", tier="club member")
        ica.cancel_pending_action()
        assert ica.confirm_pending_action() == "There's nothing waiting for confirmation right now."
        write.assert_not_called()

    def test_invalid_tier_rejected_at_stage(self, mocker):
        _mock_lookup(mocker)
        mocker.patch("services.product_service.get_tier_by_name", return_value=None)
        out = ica.stage_update_client("christina", tier="platinum")
        assert "isn't a pricing tier" in out
        assert ica.peek_pending(ica._user()) is None

    def test_note_appends_to_existing(self, mocker):
        _mock_lookup(mocker)
        ica.stage_update_client("christina", add_note="Allergic to sulfites question — follow up")
        fields = ica.peek_pending(ica._user())["params"]["fields"]
        assert fields["notes"].startswith("Prefers morning deliveries")
        assert "follow up" in fields["notes"]

    def test_bad_email_rejected(self, mocker):
        _mock_lookup(mocker)
        out = ica.stage_update_client("christina", email="not-an-email")
        assert "email" in out.lower()
        assert ica.peek_pending(ica._user()) is None

    def test_unknown_customer(self, mocker):
        _mock_lookup(mocker, {"match": "none"})
        out = ica.stage_update_client("nobody", tier="Wholesale")
        assert "can't find" in out.lower()

    def test_no_fields_asks(self, mocker):
        _mock_lookup(mocker)
        out = ica.stage_update_client("christina")
        assert "what should i change" in out.lower()
        assert ica.peek_pending(ica._user()) is None
