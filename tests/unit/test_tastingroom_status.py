"""Tests for the deterministic tasting-room status fast-path.

Covers the contract that keeps it safe:
  - only unambiguous status phrases are claimed; everything else returns None
    so the conversational assistant (LLM) still handles nuanced questions
  - board + case views render fixed, Google-Chat-safe text from the goal model
  - the three status levels (case traffic light, party ladder, next action)
    reflect GoalState exactly
  - zero matches / errors fall back to the assistant (return None)
  - the Chat adapter short-circuits before the LLM on a status question
"""

import asyncio

import pytest

import services.tastingroom_status as ts


# ── query matching ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "status", "Status", "STATUS?", "/status", "status update",
    "what's open", "What is the status", "open cases", "pending",
    "where are things", "show status",
])
def test_board_phrases_match(text):
    assert ts.match_status_query(text) == ("board", "")


@pytest.mark.parametrize("text,query", [
    ("status of Mira", "mira"),
    ("Status of Mira?", "mira"),
    ("status for the June tour", "june tour"),
    ("/status TASTING-20260607-4G-MIRA-PARK", "tasting-20260607-4g-mira-park"),
    ("what's the status of Mira's case", "mira"),
    ("What is the status of test", "test"),
])
def test_case_phrases_extract_query(text, query):
    assert ts.match_status_query(text) == ("case", query)


@pytest.mark.parametrize("text", [
    "why is Mira waiting?",             # nuanced question → assistant
    "show me the mail for Mira",        # email evidence → assistant tool
    "send the invoice for Mira",        # action → confirm-first assistant flow
    "mark Mira paid",
    "revise the draft to be warmer",
    "hello",
    "status meeting notes",             # not of/for/on → assistant
    "",
])
def test_non_status_phrases_fall_through(text):
    assert ts.match_status_query(text) is None


# ── board rendering ───────────────────────────────────────────────────────────

def _board_case(**over):
    base = {"case": "TASTING-1", "client_name": "Mira Park", "case_type": "standard",
            "date": "2026-07-25", "confirmed": [], "stage": "need_cecil_approval",
            "waiting_on": "Awaiting Winefornia approval"}
    base.update(over)
    return base


def test_board_empty():
    assert "all caught up" in ts.render_board([])


def test_board_sorts_blocked_first_and_marks_levels():
    out = ts.render_board([
        _board_case(client_name="Green Light", stage="send_invoice",
                    waiting_on="Ready to send the invoice"),
        _board_case(client_name="Red Blocked", stage="ask_client_alternatives",
                    waiting_on="Going back to the client for a new time"),
        _board_case(client_name="Yellow Wait", stage="await_or_check_payment",
                    waiting_on="Waiting on payment"),
    ])
    lines = out.split("\n")
    assert lines[0] == "*Open tasting cases (3)*"
    assert lines[1].startswith("🔴 *Red Blocked*")
    assert lines[2].startswith("🟡 *Yellow Wait*")
    assert lines[3].startswith("🟢 *Green Light*")
    # Google Chat markup only: single-asterisk bold, no markdown headers/tables
    assert "**" not in out and "#" not in out and "|" not in out


def test_board_flags_production_tours():
    out = ts.render_board([_board_case(case_type="production_tour")])
    assert "production tour" in out


# ── case rendering (the party ladder) ─────────────────────────────────────────

def _case(goal_state=None, gaps=None, goal_met=False, **res_over):
    reservation = {"reservation_id": "TASTING-20260725-4G-MIRA", "client_name": "Mira Park",
                   "requested_date": "2026-07-25", "requested_time": "14:00",
                   "guest_count": 4}
    reservation.update(res_over)
    gs = {"case_type": "standard", "cecil_status": "unknown",
          "customer_commitment": "none", "josh_availability": "unknown",
          "invoice": "not_sent", "confirmation": "not_sent"}
    gs.update(goal_state or {})
    return {"reservation": reservation, "goal_state": gs,
            "gaps": gaps if gaps is not None else [], "goal_met": goal_met}


def test_case_fresh_request():
    out = ts.render_case(_case(gaps=["need_cecil_approval", "need_josh_availability"]))
    assert "*Mira Park — TASTING-20260725-4G-MIRA*" in out
    assert "4 guests" in out and "Standard tasting" in out
    assert "◻️ Winefornia (Cecil) — approval not confirmed yet" in out
    assert "◻️ Josh (facility) — availability unknown" in out
    assert "◻️ Customer — no slot offered yet" in out
    assert "*Next:* Awaiting Winefornia approval" in out


def test_case_offered_awaiting_customer():
    out = ts.render_case(_case(
        goal_state={"cecil_status": "ok", "josh_availability": "confirmed",
                    "customer_commitment": "offered"},
        gaps=[]))
    assert "✅ Winefornia (Cecil) — approved" in out
    assert "✅ Josh (facility) — confirmed the slot" in out
    assert "⏳ Customer — slot offered, awaiting their reply" in out
    assert "*Waiting on:* a reply we already requested." in out


def test_case_blocked_shows_blocked_level():
    out = ts.render_case(_case(
        goal_state={"josh_availability": "unavailable"},
        gaps=["ask_client_alternatives"]))
    assert "🔴 Josh (facility) — not available for the requested time" in out
    assert "*Blocked:* Going back to the client for a new time" in out


def test_case_paid_awaiting_confirmation():
    out = ts.render_case(_case(
        goal_state={"cecil_status": "ok", "josh_availability": "confirmed",
                    "customer_commitment": "accepted", "invoice": "paid"},
        gaps=["send_final_confirmation"]))
    assert "✅ Invoice — paid" in out
    assert "◻️ Final confirmation — not sent yet" in out
    assert "*Next:* Ready to confirm + send calendar invites" in out


def test_case_production_tour_wording():
    out = ts.render_case(_case(
        goal_state={"case_type": "production_tour", "cecil_status": "ok"},
        gaps=["need_josh_availability"]))
    assert "Production tour + tasting" in out
    assert "✅ Winefornia (Cecil) — available for the slot" in out


def test_case_goal_met():
    out = ts.render_case(_case(
        goal_state={"cecil_status": "ok", "josh_availability": "confirmed",
                    "customer_commitment": "accepted", "invoice": "paid",
                    "confirmation": "sent"},
        goal_met=True))
    assert "*Done:*" in out


# ── try_status_reply (fetch + fallback contract) ──────────────────────────────

def test_reply_board(monkeypatch):
    import vertex_agent.tools as tools
    monkeypatch.setattr(tools, "open_cases_status", lambda: [_board_case()])
    out = ts.try_status_reply("status")
    assert out and "*Open tasting cases (1)*" in out and "Mira Park" in out


def test_reply_single_case(monkeypatch):
    import vertex_agent.chat_agent as chat_agent
    import vertex_agent.tools as tools
    monkeypatch.setattr(chat_agent, "find_cases",
                        lambda q: [{"reservation_id": "TASTING-1", "client_name": "Mira Park"}])
    monkeypatch.setattr(tools, "get_case", lambda rid: _case())
    out = ts.try_status_reply("status of mira")
    assert out and "*Mira Park — TASTING-20260725-4G-MIRA*" in out


def test_reply_multiple_matches_lists_candidates(monkeypatch):
    import vertex_agent.chat_agent as chat_agent
    rows = [{"reservation_id": f"TASTING-{i}", "client_name": f"Mira {i}",
             "requested_date": "2026-07-25"} for i in range(2)]
    monkeypatch.setattr(chat_agent, "find_cases", lambda q: rows)
    out = ts.try_status_reply("status of mira")
    assert out and "which one?" in out and "TASTING-0" in out and "TASTING-1" in out


def test_reply_unknown_name_returns_none_for_llm(monkeypatch):
    import vertex_agent.chat_agent as chat_agent
    monkeypatch.setattr(chat_agent, "find_cases", lambda q: [])
    assert ts.try_status_reply("status of nobody") is None


def test_reply_never_raises(monkeypatch):
    import vertex_agent.tools as tools
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(tools, "open_cases_status", boom)
    assert ts.try_status_reply("status") is None  # falls back to the assistant


def test_reply_non_status_is_none_without_db():
    # No mocks: a non-status phrase must return None before touching anything.
    assert ts.try_status_reply("why is mira waiting?") is None


# ── adapter short-circuit ─────────────────────────────────────────────────────

def _message_event(text):
    return {"type": "MESSAGE", "space": {"name": "spaces/TEST"},
            "user": {"email": "cecil.park@winefornia.com"},
            "message": {"name": f"spaces/TEST/messages/{text!r}", "text": text}}


def test_status_message_skips_llm(monkeypatch):
    import app.adapters.google_chat_tastingroom as tr
    import vertex_agent.chat_agent as chat_agent
    tr._seen_messages.clear()
    monkeypatch.setenv("GCHAT_ASYNC", "off")
    monkeypatch.setattr(chat_agent, "discuss",
                        lambda *a, **k: pytest.fail("status must not reach the LLM"))
    monkeypatch.setattr(ts, "try_status_reply", lambda text: "*Open tasting cases (0)*")

    resp = asyncio.run(tr.handle_tastingroom_event(_message_event("status")))
    assert "Open tasting cases" in resp["text"]


def test_non_status_message_still_reaches_llm(monkeypatch):
    import app.adapters.google_chat_tastingroom as tr
    import vertex_agent.chat_agent as chat_agent
    tr._seen_messages.clear()
    monkeypatch.setenv("GCHAT_ASYNC", "off")
    calls = []
    monkeypatch.setattr(chat_agent, "discuss",
                        lambda text, *, user="": calls.append(text) or "llm answer")

    resp = asyncio.run(tr.handle_tastingroom_event(_message_event("why is mira waiting?")))
    assert calls == ["why is mira waiting?"]
    assert resp["text"] == "llm answer"
