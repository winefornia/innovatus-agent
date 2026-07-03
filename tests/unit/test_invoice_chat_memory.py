"""Tests for the per-case conversation memory (vertex_agent.invoice_chat_memory)
and its injection into the invoicing chat prompt.

The failure this guards against: staff paste a full order, the agent asks
clarifying questions, and the terse follow-up ("2023, add on $30, other") used to
reach a fresh agent with zero context. Now the Chat thread keys a CASE whose
rolling transcript is replayed above every new message.

Covers:
  - case identity: thread name wins; threadless DMs fall back to space+sender;
    nothing to key on → "" (memory skipped)
  - record/render round-trip, ordering, and role labels
  - bounds: per-entry truncation, per-case turn cap, LRU eviction, TTL expiry
  - _build_prompt layering: transcript + pending-confirm note + new message,
    and pass-through when there's no history
"""

import pytest

import vertex_agent.invoice_chat_actions as ica
import vertex_agent.invoice_chat_memory as icm
from vertex_agent.invoice_chat_agent import _build_prompt


@pytest.fixture(autouse=True)
def _clear_state():
    icm._cases.clear()
    ica._PENDING.clear()
    yield
    icm._cases.clear()
    ica._PENDING.clear()


# ── case identity ────────────────────────────────────────────────────────────

class TestCaseKey:
    def test_space_plus_sender_is_the_case(self):
        key = icm.case_key(thread="spaces/A/threads/T1", space="spaces/A",
                           user="Cecil@Winefornia.com")
        assert key == "spaces/A|cecil@winefornia.com"

    def test_flat_space_thread_churn_does_not_change_the_case(self):
        # Google Chat flat spaces mint a NEW thread.name per message — the case
        # key must be identical across them (the production amnesia bug).
        a = icm.case_key(thread="spaces/A/threads/msg1", space="spaces/A", user="c@x.com")
        b = icm.case_key(thread="spaces/A/threads/msg2", space="spaces/A", user="c@x.com")
        assert a == b

    def test_senders_in_one_space_get_separate_cases(self):
        a = icm.case_key(space="spaces/A", user="a@x.com")
        b = icm.case_key(space="spaces/A", user="b@x.com")
        assert a != b

    def test_space_alone_and_thread_fallbacks(self):
        assert icm.case_key(space="spaces/A") == "spaces/A"
        assert icm.case_key(thread="spaces/A/threads/T1") == "spaces/A/threads/T1"

    def test_nothing_to_key_on_returns_empty(self):
        assert icm.case_key() == ""
        assert icm.case_key(user="a@x.com") == ""


# ── transcript store ─────────────────────────────────────────────────────────

class TestCaseMemory:
    def test_round_trip_orders_and_labels_turns(self):
        icm.record_turn("case1", "staff", "1 case Viognier, 15% off, ship to Tiburon $30")
        icm.record_turn("case1", "assistant", "Which vintage — 2025 750ml or 2023 375ml?")
        icm.record_turn("case1", "staff", "2023, add on $30, other")
        assert icm.render_case("case1") == (
            "Staff: 1 case Viognier, 15% off, ship to Tiburon $30\n"
            "You: Which vintage — 2025 750ml or 2023 375ml?\n"
            "Staff: 2023, add on $30, other"
        )

    def test_cases_are_isolated(self):
        icm.record_turn("case1", "staff", "invoice Christina")
        icm.record_turn("case2", "staff", "invoice Oak Barrel")
        assert "Christina" not in icm.render_case("case2")
        assert "Oak Barrel" not in icm.render_case("case1")

    def test_blank_key_role_or_text_is_ignored(self):
        icm.record_turn("", "staff", "lost")
        icm.record_turn("case1", "narrator", "lost")
        icm.record_turn("case1", "staff", "   ")
        assert icm.render_case("") == ""
        assert icm.render_case("case1") == ""

    def test_long_entries_are_truncated(self):
        icm.record_turn("case1", "staff", "order " + "x" * 10_000)
        rendered = icm.render_case("case1")
        assert rendered.endswith("… [truncated]")
        assert len(rendered) < icm._ENTRY_MAX_CHARS + 100

    def test_turn_cap_keeps_only_the_tail(self):
        for i in range(icm._MAX_TURNS + 5):
            icm.record_turn("case1", "staff", f"msg {i}")
        lines = icm.render_case("case1").splitlines()
        assert len(lines) == icm._MAX_TURNS
        assert lines[-1] == f"Staff: msg {icm._MAX_TURNS + 4}"
        assert "msg 0" not in icm.render_case("case1")

    def test_lru_eviction_caps_live_cases(self):
        for i in range(icm._MAX_CASES + 3):
            icm.record_turn(f"case{i}", "staff", "hi")
        assert len(icm._cases) == icm._MAX_CASES
        assert icm.render_case("case0") == ""          # oldest evicted
        assert icm.render_case(f"case{icm._MAX_CASES + 2}") != ""

    def test_stale_case_expires(self, monkeypatch):
        icm.record_turn("case1", "staff", "old order")
        icm._cases["case1"]["ts"] -= icm._CASE_TTL + 1
        assert icm.render_case("case1") == ""
        # a new message after expiry starts a fresh transcript
        icm.record_turn("case1", "staff", "new order")
        assert icm.render_case("case1") == "Staff: new order"

    def test_forget_case(self):
        icm.record_turn("case1", "staff", "hi")
        icm.forget_case("case1")
        assert icm.render_case("case1") == ""


# ── prompt assembly ──────────────────────────────────────────────────────────

class TestBuildPrompt:
    def test_no_history_no_pending_is_passthrough(self):
        assert _build_prompt("what's wholesale on the cab?", "u", "") == \
            "what's wholesale on the cab?"

    def test_history_is_replayed_above_the_new_message(self):
        icm.record_turn("case1", "staff",
                        "Invoice Christina Yoo — 1 case Viognier, 15% off, "
                        "christina@chothompson.com, shipping to Tiburon: $30")
        icm.record_turn("case1", "assistant", "Which vintage, and which tier?")
        prompt = _build_prompt("2023, add on $30, other", "u", "case1")
        assert "[conversation so far" in prompt
        assert "Christina Yoo" in prompt
        assert "Which vintage" in prompt
        assert prompt.endswith("2023, add on $30, other")
        assert prompt.index("Christina Yoo") < prompt.index("2023, add on $30, other")

    def test_pending_note_still_injected_after_history(self):
        ica.set_current_user("u")
        ica._stage("invoice", {}, "Send Christina's invoice for $765?")
        icm.record_turn("case1", "staff", "invoice Christina")
        prompt = _build_prompt("yes", "u", "case1")
        assert "[conversation so far" in prompt
        assert "[pending confirmation]" in prompt
        assert prompt.index("[conversation so far") < prompt.index("[pending confirmation]")
        assert prompt.endswith("yes")
