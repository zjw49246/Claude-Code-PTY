"""BridgeHub: localhost HTTP server for channel message injection.

The BridgeHub runs in the parent process (your backend) and provides:
- inject(session_id, content, meta) → forwards to that session's MCP server
- on_reply callback → receives replies from CC via MCP server

Architecture:
    Your backend → BridgeHub.inject(sid, "需求变了")
        → HTTP POST to channel_server /inject
        → channel_server writes MCP notification to CC's stdin
        → CC sees <channel source="pty-bridge">需求变了</channel>

    CC calls pty_bridge_reply tool
        → channel_server HTTP POST to BridgeHub /reply
        → BridgeHub fires on_reply callback
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

logger = logging.getLogger(__name__)


class BridgeHub:
    """Central hub for channel message routing."""

    def __init__(self, port: int = 0):
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._session_ports: dict[str, int] = {}  # session_id → channel server inject port
        self._reply_handler: Callable[[str, str], None] | None = None  # (session_id, text)
        self._permission_handler: Callable[[str, dict], None] | None = None  # (session_id, request)
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._server:
            return self._server.server_address[1]
        return self._port

    def start(self) -> int:
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/reply":
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                    sid = body.get("session_id", "")
                    text = body.get("text", "")
                    if hub._reply_handler and sid and text:
                        try:
                            hub._reply_handler(sid, text)
                        except Exception:
                            logger.exception("Reply handler error")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                elif self.path == "/permission_request":
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                    sid = body.get("session_id", "")
                    if hub._permission_handler and sid:
                        try:
                            hub._permission_handler(sid, body)
                        except Exception:
                            logger.exception("Permission handler error")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", self._port), Handler)
        actual_port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        logger.info("BridgeHub started on port %d", actual_port)
        return actual_port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        self._session_ports.clear()

    def register_session(self, session_id: str, inject_port: int) -> None:
        with self._lock:
            self._session_ports[session_id] = inject_port

    def unregister_session(self, session_id: str) -> None:
        with self._lock:
            self._session_ports.pop(session_id, None)

    def on_reply(self, handler: Callable[[str, str], None]) -> None:
        self._reply_handler = handler

    def on_permission_request(self, handler: Callable[[str, dict], None]) -> None:
        """Register a callback for permission requests from CC.

        handler(session_id, request) where request contains:
            request_id, tool_name, description, input_preview
        """
        self._permission_handler = handler

    def resolve_permission(
        self, session_id: str, request_id: str, behavior: str = "allow"
    ) -> bool:
        """Send a permission decision back to CC.

        behavior: "allow" or "deny"
        Returns True if sent successfully.
        """
        with self._lock:
            port = self._session_ports.get(session_id)

        if port is None:
            logger.warning("No channel server for session %s", session_id)
            return False

        try:
            data = json.dumps(
                {"request_id": request_id, "behavior": behavior}
            ).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/permission_resolve",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status == 200
        except Exception:
            logger.exception(
                "Failed to resolve permission for session %s", session_id
            )
            return False

    def inject(self, session_id: str, content: str, meta: dict | None = None) -> bool:
        """Inject a message into a running CC session's context.

        Returns True if the message was sent successfully.
        """
        with self._lock:
            port = self._session_ports.get(session_id)

        if port is None:
            logger.warning("No channel server for session %s", session_id)
            return False

        try:
            # session_id lets the channel server reject deliveries meant for
            # another session (inject-port collision/reuse on the same host).
            data = json.dumps(
                {"content": content, "meta": meta, "session_id": session_id}
            ).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/inject",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status == 200
        except Exception:
            logger.exception("Failed to inject into session %s", session_id)
            return False
