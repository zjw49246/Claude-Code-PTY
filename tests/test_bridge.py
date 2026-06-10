"""Tests for bridge.py — BridgeHub HTTP routing."""

import json
import threading
import time
import urllib.request

from claude_pty.bridge import BridgeHub


class TestBridgeHub:
    def test_start_and_stop(self):
        hub = BridgeHub(port=0)
        port = hub.start()
        assert port > 0
        assert hub.port == port
        hub.stop()

    def test_register_unregister_session(self):
        hub = BridgeHub()
        hub.register_session("s1", 19100)
        hub.register_session("s2", 19200)
        assert hub._session_ports == {"s1": 19100, "s2": 19200}

        hub.unregister_session("s1")
        assert "s1" not in hub._session_ports
        assert "s2" in hub._session_ports

    def test_unregister_nonexistent(self):
        hub = BridgeHub()
        hub.unregister_session("nope")  # should not raise

    def test_reply_callback(self):
        hub = BridgeHub(port=0)
        port = hub.start()

        received = []
        hub.on_reply(lambda sid, text: received.append((sid, text)))

        try:
            data = json.dumps({"session_id": "s1", "text": "hello"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/reply",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200

            time.sleep(0.1)
            assert len(received) == 1
            assert received[0] == ("s1", "hello")
        finally:
            hub.stop()

    def test_permission_request_callback(self):
        hub = BridgeHub(port=0)
        port = hub.start()

        received = []
        hub.on_permission_request(lambda sid, req: received.append((sid, req)))

        try:
            data = json.dumps({
                "session_id": "s1",
                "request_id": "req-1",
                "tool_name": "Edit",
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/permission_request",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200

            time.sleep(0.1)
            assert len(received) == 1
            assert received[0][0] == "s1"
            assert received[0][1]["tool_name"] == "Edit"
        finally:
            hub.stop()

    def test_inject_no_session(self):
        hub = BridgeHub()
        result = hub.inject("unknown-session", "hello")
        assert result is False

    def test_404_on_unknown_path(self):
        hub = BridgeHub(port=0)
        port = hub.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/unknown",
                data=b"{}",
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "Should have raised"
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            hub.stop()

    def test_resolve_permission_no_session(self):
        hub = BridgeHub()
        result = hub.resolve_permission("unknown", "req-1", "allow")
        assert result is False
