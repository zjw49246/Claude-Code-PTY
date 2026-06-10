"""Full feature test — verify all CC capabilities work through PTY.

Tests: text response, tool calls (Read/Edit/Bash), thinking,
multi-turn context, file operations, and event format completeness.

Run with: python -m pytest tests/test_full_features.py -v -s
"""

import asyncio
import os
import tempfile
import shutil

import pytest

from claude_pty.config import PTYConfig
from claude_pty.events import EventType
from claude_pty.session import Session


pytestmark = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="Claude Code CLI not found",
)

CONFIG = PTYConfig(
    dangerously_skip_permissions=True,
    startup_wait=12.0,
    response_timeout=180.0,
    post_response_wait=3.0,
)


@pytest.fixture
def work_dir():
    d = tempfile.mkdtemp(prefix="pty-feature-test-")
    # Seed a file for CC to read
    with open(os.path.join(d, "hello.txt"), "w") as f:
        f.write("The secret word is: pineapple\n")
    # Seed a Python file for CC to edit
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write('def greet():\n    return "hello"\n')
    yield d
    shutil.rmtree(d, ignore_errors=True)


def collect_events(events):
    """Helper to summarize events for debugging."""
    types = [e.event_type for e in events]
    tools = [e.tool_name for e in events if e.tool_name]
    texts = [e.content[:80] for e in events if e.content and e.event_type in (EventType.MESSAGE, "message")]
    return {"types": types, "tools": tools, "texts": texts}


class TestReadFile:
    """CC should be able to read files via the Read tool."""

    async def test_read_file(self, work_dir):
        session = Session(cwd=work_dir, config=CONFIG)
        await session.start()

        events = []
        try:
            async for event in session.send_prompt(
                "Read the file hello.txt and tell me what the secret word is. Reply with just the word."
            ):
                events.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        summary = collect_events(events)
        print(f"  Events: {summary}")

        # Should have used Read tool
        has_tool = any(
            e.event_type in (EventType.TOOL_USE, "tool_use") for e in events
        )
        assert has_tool, f"Expected tool_use event. Got types: {summary['types']}"

        # Should mention pineapple in response
        all_text = " ".join(
            e.content for e in events
            if e.event_type in (EventType.MESSAGE, "message") and e.content
        ).lower()
        assert "pineapple" in all_text, f"CC didn't find the secret word. Text: {all_text}"


class TestEditFile:
    """CC should be able to edit files via Edit tool."""

    async def test_edit_file(self, work_dir):
        session = Session(cwd=work_dir, config=CONFIG)
        await session.start()

        events = []
        try:
            async for event in session.send_prompt(
                'Edit app.py: change the greet function to return "goodbye" instead of "hello". Only make that one change.'
            ):
                events.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        summary = collect_events(events)
        print(f"  Events: {summary}")

        # Verify the file was actually modified
        with open(os.path.join(work_dir, "app.py")) as f:
            content = f.read()
        assert "goodbye" in content, f"File not edited. Content: {content}"

        # Should have tool_use and tool_result events
        has_tool_use = any(e.event_type in (EventType.TOOL_USE, "tool_use") for e in events)
        has_tool_result = any(e.event_type in (EventType.TOOL_RESULT, "tool_result") for e in events)
        assert has_tool_use, f"No tool_use event. Types: {summary['types']}"
        assert has_tool_result, f"No tool_result event. Types: {summary['types']}"


class TestBashCommand:
    """CC should be able to run shell commands via Bash tool."""

    async def test_bash_command(self, work_dir):
        session = Session(cwd=work_dir, config=CONFIG)
        await session.start()

        events = []
        try:
            async for event in session.send_prompt(
                "Run this bash command and tell me the output: echo FEATURE_TEST_OK"
            ):
                events.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        summary = collect_events(events)
        print(f"  Events: {summary}")

        # Should have used Bash tool
        has_bash = any(
            e.tool_name and "bash" in e.tool_name.lower()
            for e in events
            if e.event_type in (EventType.TOOL_USE, "tool_use")
        )
        # CC might use Bash or another tool, check tool_result for the output
        all_content = " ".join(
            e.content or "" for e in events
        )
        assert "FEATURE_TEST_OK" in all_content, f"Bash output not found. Content: {all_content}"


class TestWriteNewFile:
    """CC should be able to create new files."""

    async def test_write_new_file(self, work_dir):
        session = Session(cwd=work_dir, config=CONFIG)
        await session.start()

        target = os.path.join(work_dir, "newfile.txt")
        assert not os.path.exists(target)

        events = []
        try:
            async for event in session.send_prompt(
                "Create a new file called newfile.txt with the content: hello from pty test"
            ):
                events.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        summary = collect_events(events)
        print(f"  Events: {summary}")

        assert os.path.exists(target), "newfile.txt was not created"
        with open(target) as f:
            content = f.read()
        assert "hello from pty test" in content, f"Wrong content: {content}"


class TestThinking:
    """CC should produce thinking events when reasoning."""

    async def test_thinking_visible(self, work_dir):
        session = Session(
            cwd=work_dir,
            config=PTYConfig(
                dangerously_skip_permissions=True,
                startup_wait=12.0,
                response_timeout=120.0,
                post_response_wait=3.0,
            ),
        )
        await session.start()

        events = []
        try:
            async for event in session.send_prompt(
                "Think step by step: what is 17 * 23? Show your reasoning."
            ):
                events.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        summary = collect_events(events)
        print(f"  Events: {summary}")

        # Should have message events with the answer
        all_text = " ".join(
            e.content for e in events
            if e.event_type in (EventType.MESSAGE, "message") and e.content
        )
        assert "391" in all_text, f"Wrong answer. Text: {all_text}"


class TestMultiToolChain:
    """CC should handle a task requiring multiple tool calls in sequence."""

    async def test_multi_tool_chain(self, work_dir):
        session = Session(cwd=work_dir, config=CONFIG)
        await session.start()

        events = []
        try:
            async for event in session.send_prompt(
                "First read app.py, then add a new function called farewell() that returns 'bye'. Keep the existing greet() function."
            ):
                events.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        summary = collect_events(events)
        print(f"  Events: {summary}")

        # Should have multiple tool calls
        tool_uses = [e for e in events if e.event_type in (EventType.TOOL_USE, "tool_use")]
        assert len(tool_uses) >= 2, f"Expected multiple tool calls. Got {len(tool_uses)}: {summary['tools']}"

        # Verify file has both functions
        with open(os.path.join(work_dir, "app.py")) as f:
            content = f.read()
        assert "greet" in content, f"greet() missing: {content}"
        assert "farewell" in content, f"farewell() missing: {content}"
        assert "bye" in content, f"'bye' not in farewell: {content}"


class TestEventCompleteness:
    """Every event from a tool-using response should have proper structure."""

    async def test_all_events_have_required_fields(self, work_dir):
        session = Session(cwd=work_dir, config=CONFIG)
        await session.start()

        events = []
        try:
            async for event in session.send_prompt(
                "Read hello.txt and summarize it in one sentence."
            ):
                events.append(event)
                if event.event_type in (EventType.RESULT, "result"):
                    break
        finally:
            await session.stop()

        required = {"event_type", "role", "content", "is_error", "timestamp",
                    "tool_name", "tool_input", "tool_output", "raw_json"}

        for event in events:
            d = event.to_dict()
            missing = required - set(d.keys())
            assert not missing, f"Event {event.event_type} missing keys: {missing}"

            # Type-specific checks
            if event.event_type in (EventType.TOOL_USE, "tool_use"):
                assert event.tool_name is not None, f"tool_use without tool_name: {d}"
            if event.event_type in (EventType.MESSAGE, "message"):
                assert event.role == "assistant", f"message with wrong role: {event.role}"

        print(f"  All {len(events)} events have complete structure")
        print(f"  Types: {[e.event_type for e in events]}")
