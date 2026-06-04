"""Unit tests for the deterministic idempotency key helper."""

import re
import uuid

import pytest

from services.square_service import _ikey


class TestIkey:
    def test_deterministic_same_input(self):
        """Same case_id + action always produces the same key."""
        k1 = _ikey("case_abc123", "create_customer")
        k2 = _ikey("case_abc123", "create_customer")
        assert k1 == k2

    def test_different_actions_differ(self):
        """Different action names produce different keys."""
        k1 = _ikey("case_abc123", "create_customer")
        k2 = _ikey("case_abc123", "create_order")
        assert k1 != k2

    def test_different_case_ids_differ(self):
        """Different case_ids produce different keys."""
        k1 = _ikey("case_001", "create_invoice")
        k2 = _ikey("case_002", "create_invoice")
        assert k1 != k2

    def test_max_length_45(self):
        """Key must be at most 45 characters (Square API limit)."""
        k = _ikey("case_abc123", "create_customer")
        assert len(k) <= 45

    def test_hex_chars_only(self):
        """Key contains only hexadecimal characters."""
        k = _ikey("case_abc123", "create_customer")
        assert re.fullmatch(r"[0-9a-f]+", k), f"Non-hex chars in key: {k!r}"

    def test_empty_case_id_falls_back_to_uuid(self):
        """Empty case_id falls back to a UUID — non-deterministic but valid."""
        k1 = _ikey("", "create_customer")
        k2 = _ikey("", "create_customer")
        # Length is still valid
        assert len(k1) <= 45
        # Two calls with empty case_id should differ (UUID fallback)
        # Note: this test can theoretically fail if UUIDs collide, but p(collision) ≈ 0
        assert k1 != k2

    def test_not_uuid_format_when_case_id_set(self):
        """When case_id is set the key is a truncated SHA-256 hex, not a UUID string."""
        k = _ikey("case_abc123", "create_customer")
        # UUID format has dashes; SHA-256 hex does not
        assert "-" not in k
