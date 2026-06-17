"""Tests for the write-capable chat-action layer (vertex_agent.chat_actions).

Covers the migration-critical safety behaviors:
  - confirm-first: stage_* records an intent and does NOT mutate; the real
    mutation only fires on confirm_pending_action()
  - cancel_pending_action() discards a staged action without mutating
  - the per-user pending store is keyed by the acting approver
  - reversible ops (mark_paid, manual_handle) run immediately
  - end/remove is a soft-cancel (CANCELLED_OR_DEFERRED), never a hard delete
"""

import pytest

import vertex_agent.chat_actions as ca


@pytest.fixture(autouse=True)
def _clear_pending(monkeypatch):
    # Keep the pending-store unit tests purely in-memory — stub the durable
    # Supabase backing so they never reach the network.
    monkeypatch.setattr(ca, "_db_get", lambda user: None)
    monkeypatch.setattr(ca, "_db_put", lambda *a, **k: None)
    monkeypatch.setattr(ca, "_db_del", lambda *a, **k: None)
    ca._PENDING.clear()
    ca.set_current_user("gchat_cecil@winefornia.com")
    yield
    ca._PENDING.clear()


def _res(rid="TASTING-20260610-2G-MIRA", name="Mira Park", state="WAITING_FOR_JOSH"):
    return {"reservation_id": rid, "client_name": name, "current_state": state}


def _pending_email_action(action_id="act_email", rid="TASTING-20260610-2G-MIRA"):
    return {
        "action_id": action_id,
        "reservation_id": rid,
        "action_type": "offer_client_slot",
        "status": "pending",
        "recipient_email": "mira@example.com",
        "email_subject": "Innovatus tasting availability",
        "email_body": "Hi Mira, ...",
    }


# ── confirm-first: send email ────────────────────────────────────────────────

class TestStageSendEmail:
    def test_stage_does_not_send(self, mocker):
        mocker.patch.object(ca, "_resolve", return_value={"reservation": _res()})
        mocker.patch.object(ca, "_latest_pending", return_value=_pending_email_action())
        send = mocker.patch("services.tastingroom_service.process_action_decision")

        out = ca.stage_send_email("Mira")

        send.assert_not_called()
        assert "yes" in out.lower()
        assert ca.peek_pending(ca._user())["kind"] == "send_email"

    def test_confirm_sends(self, mocker):
        mocker.patch.object(ca, "_resolve", return_value={"reservation": _res()})
        mocker.patch.object(ca, "_latest_pending", return_value=_pending_email_action())
        send = mocker.patch(
            "services.tastingroom_service.process_action_decision",
            return_value={"ok": True, "status": "sent"},
        )

        ca.stage_send_email("Mira")
        out = ca.confirm_pending_action()

        send.assert_called_once_with("act_email", "approve", decided_by=ca._user())
        assert "sent" in out.lower()
        assert ca.peek_pending(ca._user()) is None  # cleared after firing

    def test_cancel_discards_without_sending(self, mocker):
        mocker.patch.object(ca, "_resolve", return_value={"reservation": _res()})
        mocker.patch.object(ca, "_latest_pending", return_value=_pending_email_action())
        send = mocker.patch("services.tastingroom_service.process_action_decision")

        ca.stage_send_email("Mira")
        out = ca.cancel_pending_action()

        send.assert_not_called()
        assert ca.peek_pending(ca._user()) is None
        assert "left it" in out.lower()

    def test_internal_step_has_no_email_to_send(self, mocker):
        mocker.patch.object(ca, "_resolve", return_value={"reservation": _res()})
        action = {**_pending_email_action(), "recipient_email": None,
                  "action_type": "review_payment_status"}
        mocker.patch.object(ca, "_latest_pending", return_value=action)

        out = ca.stage_send_email("Mira")

        assert "internal" in out.lower()
        assert ca.peek_pending(ca._user()) is None


# ── pending store is per-user ────────────────────────────────────────────────

def test_pending_is_keyed_by_user(mocker):
    mocker.patch.object(ca, "_resolve", return_value={"reservation": _res()})
    mocker.patch.object(ca, "_latest_pending", return_value=_pending_email_action())

    ca.set_current_user("gchat_cecil@winefornia.com")
    ca.stage_send_email("Mira")

    ca.set_current_user("gchat_lisa@winefornia.com")
    assert ca.peek_pending("gchat_lisa@winefornia.com") is None
    assert ca.peek_pending("gchat_cecil@winefornia.com") is not None


# ── soft-cancel (end/remove) ─────────────────────────────────────────────────

class TestCancelCase:
    def test_stage_then_confirm_soft_cancels(self, mocker):
        mocker.patch.object(ca, "_resolve", return_value={"reservation": _res()})
        update = mocker.patch("db.repository.update_reservation")
        mocker.patch("db.repository.insert_reservation_event")
        mocker.patch("db.repository.get_reservation", return_value=_res(state="CANCELLED_OR_DEFERRED"))
        mocker.patch.object(ca, "_reject_pending_actions")

        staged = ca.stage_cancel_case("Mira")
        assert "yes" in staged.lower()
        update.assert_not_called()  # nothing happens until confirm

        ca.confirm_pending_action()
        update.assert_called_once()
        args, kwargs = update.call_args
        assert kwargs["current_state"] == "CANCELLED_OR_DEFERRED"

    def test_already_closed_is_noop(self, mocker):
        mocker.patch.object(ca, "_resolve",
                            return_value={"reservation": _res(state="CANCELLED_OR_DEFERRED")})
        out = ca.stage_cancel_case("Mira")
        assert "already closed" in out.lower()
        assert ca.peek_pending(ca._user()) is None


# ── reversible immediate ops ─────────────────────────────────────────────────

def test_mark_paid_runs_immediately(mocker):
    mocker.patch.object(ca, "_resolve", return_value={"reservation": _res()})
    mocker.patch.object(ca, "_latest_pending",
                        return_value={"action_id": "act_pay", "action_type": "review_payment_status"})
    send = mocker.patch(
        "services.tastingroom_service.process_action_decision",
        return_value={"ok": True, "status": "paid", "next_action_id": "act_final"},
    )

    out = ca.mark_paid("Mira")

    send.assert_called_once_with("act_pay", "paid", decided_by=ca._user())
    assert "paid" in out.lower()
    assert ca.peek_pending(ca._user()) is None  # immediate ops never stage


def test_ambiguous_match_asks_which(mocker):
    mocker.patch.object(
        ca, "_resolve",
        return_value={"ambiguous": [
            {"reservation_id": "TASTING-A", "client_name": "Ina Lee"},
            {"reservation_id": "TASTING-B", "client_name": "Ina Park"},
        ]},
    )
    out = ca.stage_send_email("Ina")
    assert "which one" in out.lower()
    assert ca.peek_pending(ca._user()) is None
