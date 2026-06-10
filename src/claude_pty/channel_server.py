"""Built-in MCP channel server for claude-pty.

This module is the entry point for the MCP server that Claude Code spawns
as a subprocess. It communicates with CC over stdio (JSON-RPC 2.0) and
with the parent process over a localhost HTTP endpoint (the BridgeHub).

Usage in .mcp.json:
    {
        "mcpServers": {
            "pty-bridge": {
                "command": "claude-pty-channel",
                "args": ["--port", "8100", "--session-id", "<uuid>"]
            }
        }
    }

Claude Code loads it as a channel via:
    claude --dangerously-load-development-channels server:pty-bridge
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

_stdout_lock = threading.Lock()
_initialized = threading.Event()
_session_id: str | None = None
_bridge_port: int = 0

_reply_callbacks: list[dict] = []
_reply_lock = threading.Lock()

# Pending permission requests waiting for external resolution
_pending_permissions: dict[str, threading.Event] = {}
_permission_results: dict[str, str] = {}  # request_id → "allow"/"deny"
_permission_lock = threading.Lock()


def _write_message(msg: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _send_notification(content: str, meta: dict | None = None) -> None:
    """Send a channel notification that appears in CC's context."""
    params: dict = {"content": content}
    if meta:
        params["meta"] = meta
    _write_message(
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": params,
        }
    )


def _handle_initialize(msg: dict) -> None:
    _write_message(
        {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "tools": {},
                    "experimental": {
                        "claude/channel": {},
                        "claude/channel/permission": {},
                    },
                },
                "serverInfo": {"name": "pty-bridge", "version": "0.1.0"},
                "instructions": (
                    "Messages arriving as <channel source=\"pty-bridge\"> tags are "
                    "messages from the user, delivered through the session host. "
                    "Treat them exactly like normal user messages: respond directly "
                    "in the conversation with your full answer. Do NOT use any tool "
                    "to send your reply — your assistant response itself is what the "
                    "user sees."
                ),
            },
        }
    )


def _handle_tools_list(msg: dict) -> None:
    _write_message(
        {
            "jsonrpc": "2.0",
            "id": msg["id"],
            # No tools: replies must flow through the normal conversation
            # (the host reads the session JSONL). A reply tool here would lure
            # the model into "sending" answers that the user never sees.
            "result": {"tools": []},
        }
    )


def _handle_tools_call(msg: dict) -> None:
    params = msg.get("params", {})
    name = params.get("name")
    args = params.get("arguments", {})

    if name == "pty_bridge_reply":
        text = args.get("text", "")
        with _reply_lock:
            _reply_callbacks.append(
                {"text": text, "session_id": _session_id, "timestamp": time.time()}
            )
        # Also forward to bridge if available
        _forward_reply_to_bridge(text)
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {"content": [{"type": "text", "text": "sent"}]},
            }
        )
    else:
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": f"Unknown tool: {name}"}
                    ],
                    "isError": True,
                },
            }
        )


def _handle_permission_request(msg: dict) -> None:
    """CC asks us to approve/deny a tool execution."""
    params = msg.get("params", {})
    request_id = params.get("request_id", "")
    if not request_id:
        return

    # Forward to BridgeHub for external resolution
    forwarded = _forward_permission_to_bridge(params)

    if forwarded:
        # Wait for external resolution (BridgeHub will POST /permission_resolve)
        event = threading.Event()
        with _permission_lock:
            _pending_permissions[request_id] = event

        # Block up to 120s for human decision
        resolved = event.wait(timeout=120)

        with _permission_lock:
            _pending_permissions.pop(request_id, None)
            behavior = _permission_results.pop(request_id, None)

        if not resolved or not behavior:
            behavior = "deny"  # Timeout → deny
    else:
        behavior = "allow"  # No bridge → auto-allow (same as --dangerously-skip-permissions)

    _write_message(
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel/permission",
            "params": {
                "request_id": request_id,
                "behavior": behavior,
            },
        }
    )


def _forward_permission_to_bridge(params: dict) -> bool:
    if not _bridge_port:
        return False
    try:
        import urllib.request

        data = json.dumps(
            {
                "session_id": _session_id,
                "request_id": params.get("request_id"),
                "tool_name": params.get("tool_name"),
                "description": params.get("description"),
                "input_preview": params.get("input_preview"),
            }
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_bridge_port}/permission_request",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status == 200
    except Exception:
        return False


def _resolve_permission(request_id: str, behavior: str) -> bool:
    """Called when BridgeHub sends the user's decision."""
    with _permission_lock:
        event = _pending_permissions.get(request_id)
        if not event:
            return False
        _permission_results[request_id] = behavior
        event.set()
    return True


def _handle_ping(msg: dict) -> None:
    _write_message({"jsonrpc": "2.0", "id": msg["id"], "result": {}})


def _forward_reply_to_bridge(text: str) -> None:
    if not _bridge_port:
        return
    try:
        import urllib.request

        data = json.dumps(
            {"text": text, "session_id": _session_id}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_bridge_port}/reply",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


class _InjectHandler(BaseHTTPRequestHandler):
    """HTTP handler for receiving inject requests from BridgeHub."""

    def do_POST(self):
        if self.path == "/inject":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            content = body.get("content", "")
            meta = body.get("meta")
            if content:
                _send_notification(content, meta)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"empty content"}')
        elif self.path == "/permission_resolve":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            request_id = body.get("request_id", "")
            behavior = body.get("behavior", "deny")
            if request_id and _resolve_permission(request_id, behavior):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"unknown request_id"}')
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs (would pollute stderr)


def _run_inject_server(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _InjectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stdio_loop() -> None:
    """Main loop: read JSON-RPC from stdin, dispatch handlers."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")

        if method == "initialize":
            _handle_initialize(msg)
        elif method == "notifications/initialized":
            _initialized.set()
        elif method == "tools/list":
            _handle_tools_list(msg)
        elif method == "tools/call":
            _handle_tools_call(msg)
        elif method == "ping":
            _handle_ping(msg)
        elif method == "notifications/claude/channel/permission_request":
            threading.Thread(
                target=_handle_permission_request,
                args=(msg,),
                daemon=True,
            ).start()
        # Ignore other notifications/methods silently


def main() -> None:
    global _session_id, _bridge_port

    parser = argparse.ArgumentParser(description="claude-pty channel MCP server")
    parser.add_argument("--port", type=int, default=0, help="Inject HTTP port (0=auto)")
    parser.add_argument("--bridge-port", type=int, default=0, help="BridgeHub port to forward replies")
    parser.add_argument("--session-id", type=str, default=None)
    args = parser.parse_args()

    _session_id = args.session_id
    _bridge_port = args.bridge_port

    inject_port = args.port
    if inject_port:
        _run_inject_server(inject_port)

    _stdio_loop()


if __name__ == "__main__":
    main()
