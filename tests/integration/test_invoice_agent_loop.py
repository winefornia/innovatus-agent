"""Live LLM-loop verification for the invoice chat agent.

This exercises the REAL ADK + Claude tool-selection loop (not mocked): it proves
the agent understands intent and routes to the right tool, and that confirm-first
gating holds. It is OFF by default — it needs google-adk + litellm + a real
ANTHROPIC_API_KEY and makes live API calls, so the normal `pytest` run skips it.

Run it explicitly:
    RUN_LLM_TESTS=1 PYTHONPATH=. .venv/bin/python -m pytest \
        tests/integration/test_invoice_agent_loop.py -q

Belt-and-suspenders: the write paths are mocked so a misbehaving model can never
mutate Supabase/Square during the test. It still reads live catalog pricing.
"""

import os

import pytest

_RUN = os.getenv("RUN_LLM_TESTS") == "1"
try:
    import google.adk  # noqa: F401
    _HAS_ADK = True
except Exception:
    _HAS_ADK = False

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_ADK),
    reason="live LLM loop — set RUN_LLM_TESTS=1 and install google-adk/litellm to run",
)

_USER = "gchat_llmtest@winefornia.com"


@pytest.fixture(autouse=True)
def _no_writes(mocker):
    """Use the real API key (tests/conftest sets a dummy) and guarantee no real
    mutation regardless of what the model decides to call."""
    # conftest.py sets ANTHROPIC_API_KEY="test-key"; load_dotenv won't override it,
    # so read the real key straight from .env for this live test.
    from dotenv import dotenv_values
    real_key = (dotenv_values() or {}).get("ANTHROPIC_API_KEY")
    if not real_key or real_key == "test-key":
        pytest.skip("no real ANTHROPIC_API_KEY in .env for the live LLM test")
    os.environ["ANTHROPIC_API_KEY"] = real_key

    import vertex_agent.invoice_chat_actions as ica
    ica._PENDING.clear()
    mocker.patch.object(ica, "_apply_product", return_value="(mocked write)")
    mocker.patch.object(ica, "_exec_invoice", return_value="(mocked invoice)")
    mocker.patch.object(ica, "_exec_set_tier", return_value="(mocked tier)")
    yield
    ica._PENDING.clear()


def test_read_query_answers_without_staging():
    import vertex_agent.invoice_chat_actions as ica
    from vertex_agent.invoice_chat_agent import discuss

    reply = discuss("what's the wholesale price on the 2021 cabernet franc?", user=_USER)

    assert ica.peek_pending(_USER) is None, "a read query must not stage anything"
    assert "$" in reply, f"expected a price in the answer, got: {reply!r}"


def test_price_edit_stages_and_confirm_gates():
    import vertex_agent.invoice_chat_actions as ica
    from vertex_agent.invoice_chat_agent import discuss

    reply = discuss("set the 2021 cabernet franc wholesale price to 90 dollars", user=_USER)

    pending = ica.peek_pending(_USER)
    assert pending is not None, "a price edit must stage a confirm-first action"
    assert pending["kind"] == "set_channel_price"
    assert pending["params"]["cents"] == 9000, "should parse '90 dollars' to 9000 cents"
    assert "yes" in reply.lower(), "must ask the user to confirm before writing"
