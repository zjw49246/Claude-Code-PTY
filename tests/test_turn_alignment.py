"""Turn-alignment + native sub-agent tracking tests.

Regression suite for the task-87 off-by-one incident: autonomous turns
(harness sub-agent notifications waking the session) were consumed by the
NEXT send_prompt, whose loop ended at the stale turn_duration — every reply
shifted one message back, permanently.
"""

import asyncio
import json
import os

import pytest

from claude_pty.config import PTYConfig
from claude_pty.events import EventType
from claude_pty.jsonl_reader import JsonlReader
from claude_pty.session import Session
from claude_pty.subagents import SubagentTracker


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


def _user_text(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _turn_duration():
    return {"type": "system", "subtype": "turn_duration", "durationMs": 1}


def _agent_tool_use(tool_use_id="toolu_1", name="Agent", **input_extra):
    inp = {"subagent_type": "Explore", "description": "查架构", **input_extra}
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": name, "input": inp}
            ],
        },
    }


def _tool_result(tool_use_id="toolu_1", text="done"):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
    }


# --------------------------------------------------------------- tracker


class TestSubagentTracker:
    def test_agent_spawn_and_done(self):
        t = SubagentTracker()
        block = {
            "id": "toolu_1",
            "name": "Agent",
            "input": {"subagent_type": "Explore", "description": "查架构"},
        }
        spawn = t.note_tool_use(block)
        assert spawn["kind"] == "native-agent"
        assert spawn["agent_type"] == "Explore"
        assert t.has_pending

        done = t.note_tool_result("toolu_1", "完成")
        assert done["kind"] == "native-agent"
        assert not t.has_pending

    def test_non_agent_tool_ignored(self):
        t = SubagentTracker()
        assert t.note_tool_use({"id": "x", "name": "Bash", "input": {}}) is None
        assert not t.has_pending

    def test_monitor_stays_pending_after_arm_result(self):
        t = SubagentTracker()
        t.note_tool_use(
            {"id": "toolu_m", "name": "Monitor", "input": {"description": "看日志"}}
        )
        # Arming result carries the harness task id; monitor stays pending
        done = t.note_tool_result(
            "toolu_m", "Monitor started (task bqirk840r, timeout 1800000ms)."
        )
        assert done is None
        assert t.has_pending
        assert t.pending["toolu_m"]["harness_task_id"] == "bqirk840r"

    def test_monitor_notification_progress_and_timeout_done(self):
        t = SubagentTracker()
        t.note_tool_use(
            {"id": "toolu_m", "name": "Monitor", "input": {"description": "看日志"}}
        )
        t.note_tool_result("toolu_m", "Monitor started (task bqirk840r, timeout 1ms)")

        progress = t.note_user_text(
            "<task-notification>\n<task-id>bqirk840r</task-id>\n"
            "<event>step: deploy</event>\n</task-notification>"
        )
        assert progress["event"] == "progress"
        assert t.has_pending

        done = t.note_user_text(
            "<task-notification>\n<task-id>bqirk840r</task-id>\n"
            "<event>[Monitor timed out — re-arm if needed]</event>\n"
            "</task-notification>"
        )
        assert done["event"] == "done"
        assert done["timed_out"] is True
        assert not t.has_pending

    def test_unrelated_notification_ignored(self):
        t = SubagentTracker()
        assert t.note_user_text("<task-notification><task-id>zzz</task-id>") is None
        assert t.note_user_text("普通消息") is None

    def test_meta_lookup(self, tmp_path):
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text("")
        sub = tmp_path / "sess" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-abc123.meta.json").write_text(
            json.dumps(
                {"agentType": "Explore", "description": "查架构", "toolUseId": "toolu_1"}
            )
        )
        t = SubagentTracker(str(jsonl))
        t.note_tool_use(
            {"id": "toolu_1", "name": "Agent", "input": {"description": "查架构"}}
        )
        done = t.note_tool_result("toolu_1", "ok")
        assert done["agent_id"] == "abc123"
        assert done["agent_type"] == "Explore"

    def test_transcripts_grew(self, tmp_path):
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text("")
        sub = tmp_path / "sess" / "subagents"
        sub.mkdir(parents=True)
        t = SubagentTracker(str(jsonl))
        t.note_tool_use(
            {"id": "toolu_1", "name": "Agent", "input": {"description": "x"}}
        )
        transcript = sub / "agent-abc.jsonl"
        transcript.write_text("line1\n")
        assert t.transcripts_grew() is True   # first observation
        assert t.transcripts_grew() is False  # unchanged
        transcript.write_text("line1\nline2\n")
        assert t.transcripts_grew() is True   # grew

    def test_no_pending_no_growth_signal(self, tmp_path):
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text("")
        t = SubagentTracker(str(jsonl))
        assert t.transcripts_grew() is False


# ---------------------------------------------------------------- reader


class TestPromptEcho:
    def test_channel_wrapped_echo_matches(self):
        r = JsonlReader("/nonexistent")
        raw = _user_text('<channel source="pty-bridge">\n现在情况是怎样\n</channel>')
        assert r.is_prompt_echo(raw, "现在情况是怎样") is True

    def test_list_content_echo_matches(self):
        r = JsonlReader("/nonexistent")
        raw = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello world"}],
            },
        }
        assert r.is_prompt_echo(raw, "hello world") is True

    def test_other_user_message_no_match(self):
        r = JsonlReader("/nonexistent")
        raw = _user_text("<task-notification><task-id>x</task-id>")
        assert r.is_prompt_echo(raw, "现在情况是怎样") is False

    def test_non_user_no_match(self):
        r = JsonlReader("/nonexistent")
        assert r.is_prompt_echo(_turn_duration(), "anything") is False
        assert r.is_prompt_echo(_assistant_text("hi"), "hi") is False


class TestReaderSubagentEvents:
    def test_spawn_event_emitted(self):
        r = JsonlReader("/nonexistent", tracker=SubagentTracker())
        events = r.normalize(_agent_tool_use())
        types = [e.event_type for e in events]
        assert EventType.TOOL_USE in types
        assert EventType.SUBAGENT_SPAWN in types
        spawn = next(e for e in events if e.event_type == EventType.SUBAGENT_SPAWN)
        assert spawn.subagent["kind"] == "native-agent"

    def test_done_event_emitted(self):
        r = JsonlReader("/nonexistent", tracker=SubagentTracker())
        r.normalize(_agent_tool_use())
        events = r.normalize(_tool_result())
        types = [e.event_type for e in events]
        assert EventType.TOOL_RESULT in types
        assert EventType.SUBAGENT_DONE in types

    def test_user_text_only_in_autonomous_mode(self):
        r = JsonlReader("/nonexistent")
        raw = _user_text("<task-notification>...</task-notification>")
        assert r.normalize(raw) == []
        events = r.normalize(raw, include_user_text=True)
        assert len(events) == 1
        assert events[0].role == "user"
        assert "task-notification" in events[0].content

    def test_no_tracker_no_subagent_events(self):
        r = JsonlReader("/nonexistent")
        events = r.normalize(_agent_tool_use())
        assert [e.event_type for e in events] == [EventType.TOOL_USE]


# --------------------------------------------------------------- session


def _make_session(tmp_path, config=None) -> Session:
    """A started Session over a real temp JSONL with a fake PTY process."""
    config = config or PTYConfig(
        jsonl_poll_interval=0.01,
        post_response_wait=0.0,
        response_timeout=5.0,
        idle_poll_interval=0.01,
        subagent_check_interval=0.01,
    )
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text("")

    session = Session(cwd=str(tmp_path), config=config)
    session._session_id = "sid-1"

    class FakeProc:
        session_id = "sid-1"
        is_alive = True
        exit_code = None
        jsonl_path = str(jsonl)
        rate_limited = False
        sent: list = []

        def send_prompt(self, text):
            FakeProc.sent.append(text)

        def stop(self):
            FakeProc.is_alive = False

    FakeProc.sent = []
    FakeProc.is_alive = True
    session._process = FakeProc()
    session._tracker.set_jsonl_path(str(jsonl))
    session._reader = JsonlReader(str(jsonl), tracker=session._tracker)
    session._started = True
    return session


def _append(session: Session, *objs):
    with open(session._process.jsonl_path, "a", encoding="utf-8") as f:
        for obj in objs:
            f.write(_line(obj))


class TestTurnAlignment:
    """The task-87 regression: stale backlog must not complete a new turn."""

    async def test_backlog_yielded_as_orphan_and_reply_aligned(self, tmp_path):
        session = _make_session(tmp_path)

        # Backlog: an autonomous turn nobody consumed (notification + answer)
        _append(
            session,
            _user_text("<task-notification><task-id>b1</task-id></task-notification>"),
            _assistant_text("自主 turn 的回答（旧）"),
            _turn_duration(),
        )

        async def cc_responds():
            # simulate CC: echo our prompt, then answer, then sentinel
            await asyncio.sleep(0.1)
            _append(
                session,
                _user_text('<channel source="pty-bridge">\n新问题\n</channel>'),
                _assistant_text("新问题的回答"),
                _turn_duration(),
            )

        writer = asyncio.create_task(cc_responds())
        events = [e async for e in session.send_prompt("新问题")]
        await writer

        orphans = [e for e in events if e.orphan]
        replies = [
            e for e in events
            if not e.orphan and e.event_type == EventType.MESSAGE
            and e.role == "assistant"
        ]
        # Old autonomous answer surfaced but flagged orphan
        assert any("旧" in (e.content or "") for e in orphans)
        # The turn completed with OUR answer, not the stale one
        assert [e.content for e in replies] == ["新问题的回答"]
        # No timeout error
        assert not any(
            e.event_type == EventType.SYSTEM_EVENT and e.is_error for e in events
        )

    async def test_inflight_turn_duration_does_not_complete_new_turn(
        self, tmp_path
    ):
        """Prompt queued behind an in-flight autonomous turn: that turn's
        sentinel must not end ours."""
        session = _make_session(tmp_path)

        async def cc_responds():
            await asyncio.sleep(0.05)
            # in-flight autonomous turn finishes AFTER our prompt was sent
            _append(
                session,
                _assistant_text("自主 turn 收尾（旧）"),
                _turn_duration(),
            )
            await asyncio.sleep(0.05)
            # then CC dequeues our prompt and runs our turn
            _append(
                session,
                _user_text('<channel source="pty-bridge">\n第二个问题\n</channel>'),
                _assistant_text("第二个问题的回答"),
                _turn_duration(),
            )

        writer = asyncio.create_task(cc_responds())
        events = [e async for e in session.send_prompt("第二个问题")]
        await writer

        replies = [
            e for e in events
            if not e.orphan and e.event_type == EventType.MESSAGE
            and e.role == "assistant"
        ]
        assert [e.content for e in replies] == ["第二个问题的回答"]
        # the in-flight tail is orphan-flagged
        assert any(
            e.orphan and "旧" in (e.content or "") for e in events
        )

    async def test_clean_turn_unchanged(self, tmp_path):
        """No backlog: behaves exactly like before."""
        session = _make_session(tmp_path)

        async def cc_responds():
            await asyncio.sleep(0.05)
            _append(
                session,
                _user_text("普通问题"),
                _assistant_text("普通回答"),
                _turn_duration(),
            )

        writer = asyncio.create_task(cc_responds())
        events = [e async for e in session.send_prompt("普通问题")]
        await writer

        assert not any(e.orphan for e in events)
        replies = [e for e in events if e.event_type == EventType.MESSAGE]
        assert [e.content for e in replies] == ["普通回答"]


class TestIdleWatcher:
    async def test_autonomous_turn_streamed_between_prompts(self, tmp_path):
        session = _make_session(tmp_path)
        received: list = []

        async def cb(event):
            received.append(event)

        session.on_autonomous_event = cb
        watcher = asyncio.create_task(session._idle_watcher())
        try:
            await asyncio.sleep(0.05)
            _append(
                session,
                _user_text(
                    "<task-notification><task-id>b2</task-id></task-notification>"
                ),
                _assistant_text("自主处理结果"),
                _turn_duration(),
            )
            await asyncio.sleep(0.2)
        finally:
            watcher.cancel()

        assert received, "watcher should have consumed the autonomous turn"
        assert all(e.autonomous for e in received)
        contents = [e.content for e in received if e.content]
        assert any("自主处理结果" in c for c in contents)
        # notification user text surfaced too
        assert any("task-notification" in c for c in contents)

    async def test_watcher_idle_while_send_lock_held(self, tmp_path):
        session = _make_session(tmp_path)
        received: list = []

        async def cb(event):
            received.append(event)

        session.on_autonomous_event = cb
        watcher = asyncio.create_task(session._idle_watcher())
        try:
            async with session._send_lock:
                _append(session, _assistant_text("turn 中的事件"))
                await asyncio.sleep(0.1)
                assert received == []  # watcher must not steal mid-turn events
        finally:
            watcher.cancel()


class TestActivityAndEviction:
    async def test_pending_subagents_block_idle_eviction(self, tmp_path):
        from claude_pty.pool import SessionPool

        pool = SessionPool(config=PTYConfig(max_sessions=1, idle_timeout=0))
        session = _make_session(tmp_path)
        # mark a pending sub-agent
        session._tracker.note_tool_use(
            {"id": "toolu_1", "name": "Agent", "input": {"description": "x"}}
        )
        session._last_activity = 0  # ancient
        pool._sessions["sid-1"] = session
        evicted = await pool._evict_one()
        assert evicted is False  # never evict while sub-agents pending

    async def test_eviction_allowed_after_subagent_done(self, tmp_path):
        from claude_pty.pool import SessionPool

        pool = SessionPool(config=PTYConfig(max_sessions=1, idle_timeout=0))
        session = _make_session(tmp_path)
        session._tracker.note_tool_use(
            {"id": "toolu_1", "name": "Agent", "input": {"description": "x"}}
        )
        session._tracker.note_tool_result("toolu_1", "done")
        session._last_activity = 0
        pool._sessions["sid-1"] = session
        evicted = await pool._evict_one()
        assert evicted is True
