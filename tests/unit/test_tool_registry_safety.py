from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import square_service
from services.tool_registry import ToolDef, ToolError, ToolRegistry, tool_registry


def test_read_only_customer_lookup_cannot_create(monkeypatch):
    monkeypatch.setattr(tool_registry, "_get_hooks", lambda: None)
    client = Mock()
    client.customers.search.return_value = SimpleNamespace(customers=[], errors=None)
    monkeypatch.setattr(square_service, "_get_client", lambda: client)
    result = tool_registry.dispatch("square_lookup_customer", {"email": "test@example.com"})
    assert result["status"] == "not_found"
    client.customers.create.assert_not_called()


def test_lookup_failure_cannot_create_customer(monkeypatch):
    client = Mock()
    client.customers.search.return_value = SimpleNamespace(customers=None, errors=["denied"])
    monkeypatch.setattr(square_service, "_get_client", lambda: client)
    assert "error" in square_service.get_or_create_square_customer("test@example.com", "Test")
    client.customers.create.assert_not_called()


@pytest.mark.parametrize("returned_error", [True, False])
def test_business_tool_errors_are_marked_failed_in_trace(returned_error):
    registry = ToolRegistry()
    hooks = Mock()
    registry._hooks_module = hooks
    def fail():
        if returned_error:
            return {"error": "declined"}
        raise ToolError("test", "declined")
    registry.register(ToolDef("test", "test", "high", fail))
    with pytest.raises(ToolError):
        registry.dispatch("test", {}, case_id="case")
    call = hooks.fire.call_args
    assert call.args[0] == "post_tool_call"
    assert call.args[1]["has_error"] is True
    assert call.kwargs["error"] == "declined"
