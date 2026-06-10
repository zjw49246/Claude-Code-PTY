"""Integration tests — spawn real Claude Code processes.

Requires `claude` CLI to be installed and authenticated.
Run with: pytest tests/test_integration.py -v -s
"""

import asyncio
import os
import shutil
import tempfile
import time

import pytest

from claude_pty.config import PTYConfig
from claude_pty.events import EventType
from claude_pty.session import Session
from claude_pty.pty_process import PTYProcess


pytestmark = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="Claude Code CLI not found",
)

INTEGRATION_CWD = "/home/ubuntu/Projects/PTY"


class TestPTYProcessIntegration:
    """Test that we can spawn and stop a real CC process."""

    def test_spawn_and_stop(self):
        config = PTYConfig(dangerously_skip_permissions=True)
        proc = PTYProcess(cwd=INTEGRATION_CWD, config=config)
        proc.spawn()

        assert proc.is_alive
        assert proc.pid is not None
        assert proc.master_fd is not None

        exit_code = proc.stop(timeout=10)
        assert not proc.is_alive

    def test_send_prompt_and_jsonl_created(self):
        config = PTYConfig(dangerously_skip_permissions=True)
        proc = PTYProcess(cwd=INTEGRATION_CWD, config=config)
        proc.spawn()

        try:
            # Wait for CC to initialize
            time.sleep(10)

            # Send a simple prompt
            proc.send_prompt("Say exactly: test123")

            # Wait for response and JSONL write
            time.sleep(15)

            jsonl_path = proc.jsonl_path
            assert os.path.exists(jsonl_path), f"JSONL file not found: {jsonl_path}"

            with open(jsonl_path) as f:
                content = f.read()
            assert len(content) > 0, "JSONL file is empty"
        finally:
            proc.stop(timeout=10)


class TestSessionIntegration:
    """Test full prompt → response cycle with a real CC session."""

    async def test_simple_prompt(self):
        config = PTYConfig(
            dangerously_skip_permissions=True,
            startup_wait=12.0,
            response_timeout=120.0,
            post_response_wait=2.0,
        )
        session = Session(cwd=INTEGRATION_CWD, config=config)
        await session.start()

        assert session.is_alive

        events = []
        try:
            async for event in session.send_prompt(
                "Say exactly these two words only: hello world"
            ):
                events.append(event)
                print(f"  [{event.event_type}] {event.content[:80] if event.content else ''}")
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        event_types = [e.event_type for e in events]
        print(f"  Event types: {event_types}")

        has_message = any(
            e.event_type in (EventType.MESSAGE, "message") for e in events
        )
        assert has_message, f"No message event found. Got: {event_types}"

        text_events = [
            e for e in events
            if e.event_type in (EventType.MESSAGE, "message") and e.content
        ]
        assert len(text_events) > 0, "No text content received"

    async def test_multi_turn(self):
        config = PTYConfig(
            dangerously_skip_permissions=True,
            startup_wait=12.0,
            response_timeout=120.0,
            post_response_wait=3.0,
        )
        session = Session(cwd=INTEGRATION_CWD, config=config)
        await session.start()

        try:
            # First turn
            events1 = []
            async for event in session.send_prompt(
                "Remember this number for me: 42. Just confirm you remembered it."
            ):
                events1.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break

            print(f"  Turn 1 types: {[e.event_type for e in events1]}")
            assert any(
                e.event_type in (EventType.MESSAGE, "message") for e in events1
            )

            # Second turn — CC should still have context
            events2 = []
            async for event in session.send_prompt(
                "What number did I just ask you to remember? Reply with just the number."
            ):
                events2.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break

            print(f"  Turn 2 types: {[e.event_type for e in events2]}")
            text_content = " ".join(
                e.content for e in events2
                if e.event_type in (EventType.MESSAGE, "message") and e.content
            )
            print(f"  Turn 2 text: {text_content}")
            assert "42" in text_content, f"CC didn't remember 42. Got: {text_content}"

        finally:
            await session.stop()

    async def test_event_format_ccm_compatible(self):
        """Verify to_dict() output has all required CCM StreamParser fields."""
        config = PTYConfig(
            dangerously_skip_permissions=True,
            startup_wait=12.0,
            response_timeout=60.0,
        )
        session = Session(cwd=INTEGRATION_CWD, config=config)
        await session.start()

        required_keys = {
            "event_type", "role", "content", "is_error",
            "timestamp", "tool_name", "tool_input", "tool_output", "raw_json",
        }

        try:
            async for event in session.send_prompt("Say hi"):
                d = event.to_dict()
                missing = required_keys - set(d.keys())
                assert not missing, f"Missing keys: {missing} in {d}"
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()
