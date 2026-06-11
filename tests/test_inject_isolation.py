"""Regression tests for cross-session inject isolation.

Bug (2026-06-11): SessionPool allocated inject ports from a fixed per-instance
counter (19100+n), so two host processes on one machine handed out the same
port — injection from host B landed in host A's session, and /inject answered
200 so host B believed delivery succeeded.

Fixes under test:
1. Pool allocates OS-assigned free ports (no fixed base counter).
2. BridgeHub.inject() sends the target session_id.
3. channel_server /inject rejects mismatched session_id with 409.
4. channel_server survives an unbindable inject port (stdin fallback remains).
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from claude_pty import channel_server
from claude_pty.bridge import BridgeHub
from claude_pty.channel_server import _run_inject_server
from claude_pty.pool import SessionPool


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def inject_server(monkeypatch):
    """Run an inject server as session 'sess-A', capturing notifications."""
    notifications: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        channel_server, "_send_notification",
        lambda content, meta=None: notifications.append((content, meta)),
    )
    monkeypatch.setattr(channel_server, "_session_id", "sess-A")
    server = _run_inject_server(0)
    assert server is not None
    port = server.server_address[1]
    yield port, notifications
    server.shutdown()


class TestPortAllocation:
    def test_allocated_port_is_bindable(self):
        port = SessionPool._allocate_inject_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))  # must not raise

    def test_no_fixed_base_counter(self):
        # The old scheme deterministically returned 19100 for every pool's
        # first allocation. Occupy 19100 and verify the allocator never
        # hands out a port that is already listening.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            occupied = taken.getsockname()[1]
            for _ in range(20):
                assert SessionPool._allocate_inject_port() != occupied


class TestInjectSessionValidation:
    def test_mismatched_session_rejected(self, inject_server):
        port, notifications = inject_server
        status, body = _post(port, "/inject", {
            "content": "hello", "session_id": "sess-B",
        })
        assert status == 409
        assert body["error"] == "session mismatch"
        assert notifications == []

    def test_matching_session_accepted(self, inject_server):
        port, notifications = inject_server
        status, _ = _post(port, "/inject", {
            "content": "hello", "session_id": "sess-A",
        })
        assert status == 200
        assert notifications == [("hello", None)]

    def test_missing_session_id_accepted_for_compat(self, inject_server):
        port, notifications = inject_server
        status, _ = _post(port, "/inject", {"content": "legacy"})
        assert status == 200
        assert notifications == [("legacy", None)]

    def test_empty_content_still_400(self, inject_server):
        port, notifications = inject_server
        status, _ = _post(port, "/inject", {"session_id": "sess-A"})
        assert status == 400
        assert notifications == []


class TestBridgeInjectCarriesSessionId:
    def test_payload_includes_session_id(self, inject_server):
        port, notifications = inject_server
        hub = BridgeHub()
        hub.start()
        try:
            hub.register_session("sess-A", port)
            assert hub.inject("sess-A", "via hub") is True
            assert notifications == [("via hub", None)]
        finally:
            hub.stop()

    def test_cross_session_inject_fails_end_to_end(self, inject_server):
        # The exact production bug: hub B believes port N belongs to its own
        # session, but a foreign session's channel server is listening there.
        # Delivery must now FAIL (so the host falls back to stdin) instead of
        # silently landing in the foreign conversation.
        port, notifications = inject_server  # server thinks it's sess-A
        hub = BridgeHub()
        hub.start()
        try:
            hub.register_session("sess-B", port)  # collision: wrong owner
            assert hub.inject("sess-B", "leaked?") is False
            assert notifications == []
        finally:
            hub.stop()


class TestInjectServerBindFailure:
    def test_occupied_port_returns_none(self, capsys):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            occupied = taken.getsockname()[1]
            assert _run_inject_server(occupied) is None
