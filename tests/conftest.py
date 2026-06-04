"""
Shared pytest fixtures for unit and integration tests.

Fixtures:
  mock_square          — stubs services.square_service with fake Square IDs
  mock_supabase        — stubs db.repository writes; read functions return test data
  mock_anthropic       — stubs ChatAnthropic.invoke with deterministic responses
  invoice_graph_mem    — builds the invoice graph with MemorySaver (no Postgres needed)
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Minimal env vars so config.py does not raise on import
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("SQUARE_ACCESS_TOKEN", "test-square-token")
os.environ.setdefault("SQUARE_LOCATION_ID", "test-location-id")
os.environ.setdefault("SQUARE_ENVIRONMENT", "sandbox")
os.environ.setdefault("PRODUCTION_MODE", "false")


# ---------------------------------------------------------------------------
# mock_square — patch square_service at the function level
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_square(mocker):
    """Patch Square service functions to return deterministic fake IDs."""
    mocker.patch(
        "services.square_service.get_or_create_square_customer",
        return_value={"customer_id": "cust_test_001"},
    )
    mocker.patch(
        "services.square_service.create_order",
        return_value={"order_id": "ord_test_001"},
    )
    mocker.patch(
        "services.square_service.create_invoice_draft",
        return_value={
            "invoice_id": "inv_test_001",
            "invoice_version": 0,
            "invoice_url": "https://squareup.com/pay-invoice/inv_test_001",
        },
    )
    mocker.patch(
        "services.square_service.publish_invoice",
        return_value={"ok": True},
    )
    yield


# ---------------------------------------------------------------------------
# mock_supabase — no-op writes, controlled reads
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_supabase(mocker):
    """Patch Supabase repository functions. Writes are no-ops; reads return stubs."""
    mocker.patch("db.repository.upsert_invoice", return_value=None)
    mocker.patch("db.repository.log_invoice", return_value=None)
    mocker.patch("db.repository.write_workflow_record", return_value=None)
    mocker.patch("db.repository.insert_case", return_value=None)
    mocker.patch("db.repository.update_case", return_value=None)
    mocker.patch("db.repository.insert_trace_event", return_value=None)
    mocker.patch("db.repository.insert_failure_label", return_value=None)
    mocker.patch(
        "db.repository.list_recent_invoices",
        return_value=[
            {
                "thread_id": "tg_000",
                "customer_name": "Oak Barrel Restaurant",
                "tier_name": "wholesale",
                "total_before_tax_cents": 43200,
                "approval": "approved",
                "square_invoice_id": "inv_test_001",
                "created_at": "2026-06-03T14:14:00",
            }
        ],
    )
    mocker.patch(
        "db.repository.list_recent_reservations",
        return_value=[
            {
                "reservation_id": "res_001",
                "client_name": "Smith Family",
                "requested_date": "2026-06-15",
                "requested_time": "14:00",
                "guest_count": 4,
                "experience_type": "cave_experience",
                "current_state": "FINAL_CONFIRMED",
                "updated_at": "2026-06-03T09:22:00",
            }
        ],
    )
    yield


# ---------------------------------------------------------------------------
# mock_anthropic — deterministic LLM stubs (avoids real API calls)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_anthropic(mocker):
    """Patch ChatAnthropic.invoke to return deterministic responses."""
    mock_response = MagicMock()
    mock_response.content = "invoice_request"

    mocker.patch(
        "langchain_anthropic.ChatAnthropic.invoke",
        return_value=mock_response,
    )
    yield mock_response


# ---------------------------------------------------------------------------
# invoice_graph_mem — MemorySaver graph (no Postgres needed for tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def invoice_graph_mem(mocker):
    """Return an invoice graph compiled with MemorySaver checkpointer."""
    from langgraph.checkpoint.memory import MemorySaver
    mocker.patch(
        "agents.invoice_graph._make_checkpointer",
        return_value=MemorySaver(),
    )
    from agents.invoice_graph import build_invoice_graph
    return build_invoice_graph(checkpointer=MemorySaver())
