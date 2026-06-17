"""Tests for the two production-hardening additions:

  1. The chat pending-confirmation store is DURABLE — a staged action survives a
     web restart (in-memory mirror cleared) because it's read back from the DB.
  2. The watcher-liveness monitor alerts once when the heartbeat goes stale,
     posts a recovery note once when it returns, and stays quiet before the first
     heartbeat (no false alarm on a fresh deploy).
"""
from datetime import datetime, timedelta, timezone

import services.heartbeat_monitor as hb
import vertex_agent.chat_actions as ca


# ── 1. durable pending store ─────────────────────────────────────────────────

def test_pending_survives_restart(monkeypatch):
    """Stage an action, wipe the in-memory mirror (simulating a web restart), and
    confirm peek_pending recovers it from the durable backing."""
    fake_db: dict[str, dict] = {}

    def _put(user, kind, params, summary):
        fake_db[user] = {"chat_user": user, "kind": kind, "params": params,
                         "summary": summary,
                         "created_at": datetime.now(timezone.utc).isoformat()}

    def _get(user):
        return fake_db.get(user)

    monkeypatch.setattr("db.repository.upsert_chat_pending", _put)
    monkeypatch.setattr("db.repository.get_chat_pending", _get)
    monkeypatch.setattr("db.repository.delete_chat_pending", lambda u: fake_db.pop(u, None))

    ca.set_current_user("gchat_lisa@winefornia.com")
    ca._stage("send_email", {"action_id": "act_1"}, "Reply yes to send")

    ca._PENDING.clear()  # ← restart: process memory is gone, DB row remains

    entry = ca.peek_pending("gchat_lisa@winefornia.com")
    assert entry is not None
    assert entry["kind"] == "send_email"
    assert entry["params"]["action_id"] == "act_1"


# ── 2. watcher-liveness monitor ──────────────────────────────────────────────

def _row(age_seconds: float) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {"name": "tastingroom_watcher", "last_beat_at": ts.isoformat()}


def _run_check(monkeypatch, *, row, stale=600):
    import asyncio

    alerts: list[str] = []
    monkeypatch.setattr(hb, "_STALE_SECONDS", stale)
    monkeypatch.setattr("db.repository.get_heartbeat", lambda name: row)
    monkeypatch.setattr(hb, "_alert", lambda text: alerts.append(text))
    asyncio.run(hb._check_once())
    return alerts


def test_no_alert_before_first_heartbeat(monkeypatch):
    hb._alerted = False
    alerts = _run_check(monkeypatch, row=None)
    assert alerts == []          # no row yet → stay quiet
    assert hb._alerted is False


def test_alerts_once_when_stale(monkeypatch):
    hb._alerted = False
    alerts = _run_check(monkeypatch, row=_row(900))  # 15 min > 10 min
    assert len(alerts) == 1
    assert "down" in alerts[0].lower()
    assert hb._alerted is True
    # second check while still stale → no repeat
    again = _run_check(monkeypatch, row=_row(900))
    assert again == []


def test_recovery_note_when_back(monkeypatch):
    hb._alerted = True           # we had alerted during an outage
    alerts = _run_check(monkeypatch, row=_row(30))  # fresh again
    assert len(alerts) == 1
    assert "back" in alerts[0].lower()
    assert hb._alerted is False


def test_quiet_when_fresh(monkeypatch):
    hb._alerted = False
    alerts = _run_check(monkeypatch, row=_row(30))
    assert alerts == []
    assert hb._alerted is False
