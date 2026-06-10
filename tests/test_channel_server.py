"""Tests for channel_server.py — MCP message construction and permission resolve."""

import threading

from claude_pty.channel_server import _resolve_permission, _pending_permissions, _permission_results, _permission_lock


class TestResolvePermission:
    def setup_method(self):
        with _permission_lock:
            _pending_permissions.clear()
            _permission_results.clear()

    def test_resolve_existing_request(self):
        event = threading.Event()
        with _permission_lock:
            _pending_permissions["req-1"] = event

        result = _resolve_permission("req-1", "allow")
        assert result is True
        assert event.is_set()

        with _permission_lock:
            assert _permission_results["req-1"] == "allow"

    def test_resolve_unknown_request(self):
        result = _resolve_permission("nonexistent", "deny")
        assert result is False

    def test_resolve_deny(self):
        event = threading.Event()
        with _permission_lock:
            _pending_permissions["req-2"] = event

        _resolve_permission("req-2", "deny")
        assert event.is_set()

        with _permission_lock:
            assert _permission_results["req-2"] == "deny"
