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
from claude_pty.exceptions import SteerDeliveryUncertainError
from claude_pty.jsonl_reader import JsonlReader
from claude_pty.session import Session
from claude_pty.subagents import SubagentTracker


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


def _user_text(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _user_image_echo(text, *, image_count=1):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                *[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    }
                    for _ in range(image_count)
                ],
            ],
        },
    }


def _image_source_echo(path):
    return _user_text(f"[Image: source: {path}]")


def _assistant_text(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _turn_duration():
    return {"type": "system", "subtype": "turn_duration", "durationMs": 1}


def _queue_operation(operation, content=None):
    event = {"type": "queue-operation", "operation": operation}
    if content is not None:
        event["content"] = content
    return event


def _rate_limit_event(status, *, hard_limit=False):
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "hard_limit": hard_limit,
        },
    }


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

    def test_image_attachment_echo_matches_after_cli_path_conversion(self):
        r = JsonlReader("/nonexistent")
        image_path = (
            "/srv/ccm/uploads/11111111-1111-4111-8111-111111111111.png"
        )
        prompt = (
            "检查截图中的状态\n\n"
            "请用 Read 工具查看以下文件：\n"
            f"- {image_path}"
        )
        matcher = r.prompt_echo_matcher(prompt)

        assert matcher.observe(
            _user_image_echo(
                "[Image #16]检查截图中的状态\n\n"
                "请用 Read 工具查看以下文件：\n-"
            )
        ) is False
        assert matcher.observe(_image_source_echo(image_path)) is True

    def test_image_echo_matches_when_claude_removes_blank_lines(self):
        """Image user records collapse blank lines in the text block."""

        r = JsonlReader("/nonexistent")
        image_path = "/srv/ccm/uploads/screen.png"
        prompt = (
            "<ccm_task_artifact_policy>\n"
            "规则一\n"
            "</ccm_task_artifact_policy>\n\n"
            "数学排版提示：使用有效 LaTeX。\n\n"
            "请用 Read 工具查看以下文件：\n"
            f"- {image_path}\n"
            "\n"
            "任务：检查截图"
        )
        matcher = r.prompt_echo_matcher(prompt)

        # Claude keeps the image marker and text but omits empty lines when
        # serializing a multimodal user message. The source companion record
        # still carries the exact path and must complete the match.
        assert matcher.observe(
            _user_image_echo(
                "[Image #1]<ccm_task_artifact_policy>\n"
                "规则一\n"
                "</ccm_task_artifact_policy>\n"
                "数学排版提示：使用有效 LaTeX。\n"
                "请用 Read 工具查看以下文件：\n-\n"
                "任务：检查截图"
            )
        ) is False
        assert matcher.observe(_image_source_echo(image_path)) is True

    def test_multiple_image_sources_must_match_in_prompt_order(self):
        r = JsonlReader("/nonexistent")
        first = "/srv/ccm/uploads/first image.png"
        second = "/srv/ccm/uploads/second.jpg"
        prompt = (
            "比较截图\n\n请用 Read 工具查看以下文件：\n"
            f"- {first}\n- notes.txt\n- {second}"
        )
        matcher = r.prompt_echo_matcher(prompt)

        assert matcher.observe(
            _user_image_echo(
                "[Image #3][Image #4]比较截图\n\n"
                "请用 Read 工具查看以下文件：\n-\n- notes.txt\n-",
                image_count=2,
            )
        ) is False
        assert matcher.observe(_image_source_echo(first)) is False
        assert matcher.observe(_image_source_echo(second)) is True

    def test_unrelated_image_attachment_echo_does_not_match(self):
        r = JsonlReader("/nonexistent")
        expected_path = "/srv/ccm/uploads/expected.png"
        prompt = (
            "检查截图\n\n"
            "请用 Read 工具查看以下文件：\n"
            f"- {expected_path}"
        )
        matcher = r.prompt_echo_matcher(prompt)

        assert matcher.observe(
            _user_image_echo(
                "[Image #17]检查截图\n\n"
                "请用 Read 工具查看以下文件：\n-"
            )
        ) is False
        assert matcher.observe(
            _image_source_echo("/srv/ccm/uploads/other.png")
        ) is False

    def test_text_only_echo_cannot_omit_attachment_path(self):
        r = JsonlReader("/nonexistent")
        prompt = (
            "检查截图\n\n请用 Read 工具查看以下文件：\n"
            "- /srv/ccm/uploads/screen.png"
        )

        raw = _user_text("检查截图\n\n请用 Read 工具查看以下文件：\n-")
        assert r.is_prompt_echo(raw, prompt) is False

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
        stop_count = 0

        def send_prompt(self, text):
            FakeProc.sent.append(text)

        def stop(self):
            FakeProc.stop_count += 1
            FakeProc.is_alive = False

    FakeProc.sent = []
    FakeProc.is_alive = True
    FakeProc.stop_count = 0
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

    async def test_image_attachment_echo_starts_and_completes_turn(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        prompt = (
            "比较两个结果\n\n请用 Read 工具查看以下文件：\n"
            "- /srv/ccm/uploads/result.png"
        )

        async def cc_responds():
            await asyncio.sleep(0.05)
            _append(
                session,
                _user_image_echo(
                    "[Image #4]比较两个结果\n\n"
                    "请用 Read 工具查看以下文件：\n-"
                ),
                _image_source_echo("/srv/ccm/uploads/result.png"),
                _assistant_text("图片分析完成"),
                _turn_duration(),
            )

        writer = asyncio.create_task(cc_responds())
        events = [e async for e in session.send_prompt(prompt, timeout=0.3)]
        await writer

        replies = [
            e
            for e in events
            if e.event_type == EventType.MESSAGE and e.role == "assistant"
        ]
        assert [e.content for e in replies] == ["图片分析完成"]
        assert not any(e.orphan for e in replies)
        assert not any(
            e.event_type == EventType.SYSTEM_EVENT and e.is_error
            for e in events
        )

    async def test_same_text_with_different_image_stays_turn_aligned(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        expected_path = "/srv/ccm/uploads/new.png"
        prompt = (
            "检查截图\n\n请用 Read 工具查看以下文件：\n"
            f"- {expected_path}"
        )

        async def cc_responds():
            await asyncio.sleep(0.05)
            common_echo = (
                "[Image #8]检查截图\n\n"
                "请用 Read 工具查看以下文件：\n-"
            )
            _append(
                session,
                _user_image_echo(common_echo),
                _image_source_echo("/srv/ccm/uploads/old.png"),
                _assistant_text("旧截图回答"),
                _turn_duration(),
            )
            await asyncio.sleep(0.05)
            _append(
                session,
                _user_image_echo(common_echo),
                _image_source_echo(expected_path),
                _assistant_text("新截图回答"),
                _turn_duration(),
            )

        writer = asyncio.create_task(cc_responds())
        events = [e async for e in session.send_prompt(prompt, timeout=0.5)]
        await writer

        assert any(
            e.orphan and e.content == "旧截图回答" for e in events
        )
        assert any(
            not e.orphan and e.content == "新截图回答" for e in events
        )

    async def test_unrelated_queue_operation_does_not_cancel_channel_fallback(
        self, tmp_path
    ):
        """Child queue records cannot suppress a dropped channel prompt."""

        config = PTYConfig(
            jsonl_poll_interval=0.01,
            post_response_wait=0.0,
            response_timeout=1.0,
            inject_confirm_timeout=0.05,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
        )
        session = _make_session(tmp_path, config)
        channel_calls = []

        async def channel_delivery(_text):
            channel_calls.append(_text)
            return "channel"

        session._deliver_prompt = channel_delivery
        prompt = "follow-up prompt"

        async def cc_responds():
            await asyncio.sleep(0.02)
            _append(
                session,
                _queue_operation(
                    "enqueue",
                    "<task-notification>child finished</task-notification>",
                ),
                _queue_operation("dequeue"),
                _user_text(
                    "<task-notification>child finished</task-notification>"
                ),
                _assistant_text("child reply"),
                _turn_duration(),
            )
            # The confirmation window must still trigger exactly one stdin
            # retry despite the unrelated child records above.
            deadline = asyncio.get_running_loop().time() + 0.5
            while not session._process.sent and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            assert session._process.sent == [prompt]
            _append(
                session,
                _user_text(prompt),
                _assistant_text("follow-up reply"),
                _turn_duration(),
            )

        writer = asyncio.create_task(cc_responds())
        events = [e async for e in session.send_prompt(prompt, timeout=1.0)]
        await writer

        assert channel_calls == [prompt]
        assert session._process.sent == [prompt]
        replies = [
            e
            for e in events
            if not e.orphan
            and e.event_type == EventType.MESSAGE
            and e.role == "assistant"
        ]
        assert [e.content for e in replies] == ["follow-up reply"]

    async def test_matching_queue_operation_confirms_channel_delivery(
        self, tmp_path
    ):
        """An enqueue carrying this prompt still suppresses duplicate stdin."""

        config = PTYConfig(
            jsonl_poll_interval=0.01,
            post_response_wait=0.0,
            response_timeout=1.0,
            inject_confirm_timeout=0.05,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
        )
        session = _make_session(tmp_path, config)

        async def channel_delivery(_text):
            return "channel"

        session._deliver_prompt = channel_delivery
        prompt = "queued follow-up"

        async def cc_responds():
            await asyncio.sleep(0.02)
            _append(
                session,
                _queue_operation("enqueue", prompt),
                _queue_operation("dequeue"),
                _user_text(prompt),
                _assistant_text("queued reply"),
                _turn_duration(),
            )

        writer = asyncio.create_task(cc_responds())
        events = [e async for e in session.send_prompt(prompt, timeout=1.0)]
        await writer

        assert session._process.sent == []
        replies = [
            e
            for e in events
            if e.event_type == EventType.MESSAGE and e.role == "assistant"
        ]
        assert [e.content for e in replies] == ["queued reply"]


class TestLiveSteering:
    async def _wait_until(self, predicate, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("condition was not reached")
            await asyncio.sleep(0.005)

    def test_structured_rate_limit_distinguishes_soft_status(self):
        assert Session._structured_rate_limit_is_hard(
            _rate_limit_event("allowed_warning")
        ) is False
        assert Session._structured_rate_limit_is_hard(
            _rate_limit_event("allowed")
        ) is False
        assert Session._structured_rate_limit_is_hard(
            _rate_limit_event("rejected")
        ) is True
        assert Session._structured_rate_limit_is_hard({
            "type": "rate_limit_event",
        }) is True

    async def test_steers_only_after_prompt_echo_without_channels(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])

        # Delivery to stdin alone is not enough: until CC echoes this exact
        # prompt, it may still be queued behind another foreground turn.
        assert session.active_turn_process is None
        assert await session.steer_active_turn("too early") is False

        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)
        native_process = session.active_turn_process

        steering = asyncio.create_task(session.steer_active_turn(
            "change direction", expected_process=native_process
        ))
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "change direction"]
        )
        _append(
            session,
            _queue_operation("enqueue", "change direction"),
            _queue_operation("remove"),
        )
        assert await steering is True

        _append(session, _assistant_text("done"), _turn_duration())
        await turn
        assert session.active_turn_process is None

    async def test_terminal_race_keeps_dequeued_followup_managed(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        steering = asyncio.create_task(
            session.steer_active_turn("boundary update")
        )
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "boundary update"]
        )
        # This is the production race signature: the original terminal lands
        # after preflight but before CC records the stdin prompt.  Dequeue then
        # starts a new turn; the original consumer must stay attached to it.
        _append(
            session,
            _assistant_text("original done"),
            _turn_duration(),
            _queue_operation("enqueue", "boundary update"),
            _queue_operation("dequeue"),
            _user_text("boundary update"),
            _assistant_text("followup done"),
            _turn_duration(),
        )

        assert await steering is True
        events = await turn
        contents = [event.content for event in events if event.content]
        assert "original done" in contents
        assert "followup done" in contents
        assert session.active_turn_process is None

    async def test_old_same_content_enqueue_cannot_ack_new_write(
        self, tmp_path
    ):
        config = PTYConfig(
            jsonl_poll_interval=0.2,
            post_response_wait=0.0,
            response_timeout=5.0,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
            inject_confirm_timeout=1.0,
        )
        session = _make_session(tmp_path, config=config)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(
            lambda: session.active_turn_process is not None,
            timeout=2.0,
        )

        # This durable record predates the new PTY write and has identical
        # text. Preflight must preserve it for the consumer without treating
        # it as acknowledgement of this injection.
        _append(session, _queue_operation("enqueue", "repeatable update"))
        steering = asyncio.create_task(
            session.steer_active_turn("repeatable update")
        )
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "repeatable update"]
        )
        assert session._pending_steer is not None
        assert session._pending_steer.prewrite_message_ids
        await asyncio.sleep(0.05)
        assert not steering.done()

        _append(
            session,
            _queue_operation("enqueue", "repeatable update"),
            _queue_operation("remove"),
        )
        assert await steering is True
        _append(session, _assistant_text("done"), _turn_duration())
        await turn

    async def test_enqueue_ack_does_not_release_routing_fence(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        steering = asyncio.create_task(
            session.steer_active_turn("accepted update")
        )
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "accepted update"]
        )
        _append(session, _queue_operation("enqueue", "accepted update"))
        assert await steering is True

        # API acknowledgement is intentionally earlier than queue routing.
        # The foreground consumer must retain ownership until remove/dequeue.
        assert session._pending_steer is not None
        assert session._pending_steer.enqueued is True
        assert session._unsettled_steer_process is session._process
        _append(session, _assistant_text("still working"))
        await asyncio.sleep(0.03)
        assert session._pending_steer is not None

        _append(
            session,
            _queue_operation("remove"),
            _assistant_text("done"),
            _turn_duration(),
        )
        await turn
        assert session._pending_steer is None
        assert session._unsettled_steer_process is None

    async def test_unacknowledged_write_is_quarantined_until_turn_boundary(
        self, tmp_path
    ):
        config = PTYConfig(
            jsonl_poll_interval=0.01,
            post_response_wait=0.0,
            response_timeout=1.0,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
            inject_confirm_timeout=0.05,
        )
        session = _make_session(tmp_path, config=config)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        with pytest.raises(
            SteerDeliveryUncertainError,
            match="delivery is uncertain",
        ):
            await session.steer_active_turn("never acknowledged")

        # A complete PTY write is an at-most-once side effect.  Missing JSONL
        # acknowledgement must quarantine further steering, not kill Claude
        # while its original turn is still doing useful work.
        assert session._process.is_alive is True
        assert session._process.stop_count == 0
        assert session._pending_steer is not None
        assert session._unsettled_steer_process is session._process
        assert await session.steer_active_turn("must not be sent") is False
        assert session._process.sent == ["initial task", "never acknowledged"]

        _append(session, _assistant_text("original completed"), _turn_duration())
        events = await turn
        assert session._process.is_alive is True
        assert session._process.stop_count == 0
        assert session._pending_steer is None
        assert session._unsettled_steer_process is None
        assert not any(
            event.event_type == EventType.SESSION_CRASHED for event in events
        )

    async def test_late_ack_after_uncertain_result_keeps_followup_aligned(
        self, tmp_path
    ):
        config = PTYConfig(
            jsonl_poll_interval=0.01,
            post_response_wait=0.0,
            response_timeout=1.0,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
            inject_confirm_timeout=0.05,
        )
        session = _make_session(tmp_path, config=config)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        with pytest.raises(SteerDeliveryUncertainError):
            await session.steer_active_turn("late boundary update")

        # Claude can publish the queue receipt just after the API-facing
        # deadline. The original consumer must still own the boundary and
        # collect the resulting next turn even though the caller has already
        # received the delivery-uncertain result.
        _append(
            session,
            _assistant_text("original completed"),
            _turn_duration(),
            _queue_operation("enqueue", "late boundary update"),
            _queue_operation("dequeue"),
            _user_text("late boundary update"),
            _assistant_text("late followup completed"),
            _turn_duration(),
        )
        events = await turn
        contents = [event.content for event in events if event.content]
        assert "original completed" in contents
        assert "late followup completed" in contents
        assert session._process.is_alive is True
        assert session._process.stop_count == 0
        assert session._pending_steer is None
        assert session._unsettled_steer_process is None

    async def test_unacknowledged_write_still_reaps_if_consumer_is_lost(
        self, tmp_path
    ):
        config = PTYConfig(
            jsonl_poll_interval=0.01,
            post_response_wait=0.0,
            response_timeout=1.0,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
            inject_confirm_timeout=0.05,
        )
        session = _make_session(tmp_path, config=config)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        with pytest.raises(
            SteerDeliveryUncertainError,
            match="delivery is uncertain",
        ):
            await session.steer_active_turn("uncertain before cancellation")

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        assert session._process.is_alive is False
        assert session._process.stop_count == 1
        assert session._pending_steer is None
        assert session._unsettled_steer_process is None

    async def test_api_error_reaps_process_before_unacked_steer_fails(
        self, tmp_path
    ):
        config = PTYConfig(
            jsonl_poll_interval=0.01,
            post_response_wait=0.0,
            response_timeout=1.0,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
            inject_confirm_timeout=0.5,
        )
        session = _make_session(tmp_path, config=config)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        steering = asyncio.create_task(
            session.steer_active_turn("queued before API failure")
        )
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "queued before API failure"]
        )
        _append(
            session,
            _queue_operation("enqueue", "queued before API failure"),
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {"role": "assistant", "content": []},
            },
        )

        assert await steering is False
        # A False result is not observable until exact-process reap completed.
        assert session._process.stop_count == 1
        assert session._process.is_alive is False
        await turn

    async def test_api_error_later_in_batch_overrides_enqueue_ack(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        steering = asyncio.create_task(
            session.steer_active_turn("accepted before API failure")
        )
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "accepted before API failure"]
        )
        _append(
            session,
            _queue_operation("enqueue", "accepted before API failure"),
            _queue_operation("remove"),
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {"role": "assistant", "content": []},
            },
        )

        assert await steering is False
        assert session._pending_steer is None
        assert session._unsettled_steer_process is None
        assert session._process.stop_count == 0
        await turn

    async def test_allowed_warning_does_not_override_enqueue_ack(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        steering = asyncio.create_task(
            session.steer_active_turn("accepted near quota")
        )
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "accepted near quota"]
        )
        _append(
            session,
            _queue_operation("enqueue", "accepted near quota"),
            _queue_operation("remove"),
            _rate_limit_event("allowed_warning"),
        )

        assert await steering is True
        assert session._process.is_alive is True
        assert session._rate_limited_turn is False
        _append(session, _assistant_text("done"), _turn_duration())
        await turn

    async def test_dequeued_followup_timeout_stops_exact_process(
        self, tmp_path
    ):
        config = PTYConfig(
            jsonl_poll_interval=0.01,
            post_response_wait=0.0,
            response_timeout=0.08,
            idle_poll_interval=0.01,
            subagent_check_interval=0.01,
            inject_confirm_timeout=0.5,
        )
        session = _make_session(tmp_path, config=config)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        steering = asyncio.create_task(
            session.steer_active_turn("follow-up without echo")
        )
        await self._wait_until(
            lambda: session._process.sent
            == ["initial task", "follow-up without echo"]
        )
        _append(
            session,
            _assistant_text("original done"),
            _turn_duration(),
            _queue_operation("enqueue", "follow-up without echo"),
            _queue_operation("dequeue"),
        )
        assert await steering is True

        events = await turn
        assert session._process.is_alive is False
        assert session._process.stop_count == 1
        assert any(
            event.event_type == EventType.SYSTEM_EVENT and event.is_error
            for event in events
        )

    async def test_unread_terminal_is_rejected_and_preserved_for_consumer(
        self, tmp_path
    ):
        session = _make_session(tmp_path)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        # Persist terminal output without giving the poller another cycle.
        # steer_active_turn must preflight that unread JSONL and hand it back
        # to the original consumer instead of starting an orphan turn.
        _append(session, _assistant_text("done"), _turn_duration())
        assert await session.steer_active_turn("must not run") is False
        assert session._process.sent == ["initial task"]

        events = await turn
        assert any((event.content or "") == "done" for event in events)
        assert session.active_turn_process is None

    async def test_rejects_unsafe_or_replaced_steering_input(self, tmp_path):
        session = _make_session(tmp_path)
        turn = asyncio.create_task(
            self._collect(session.send_prompt("initial task"))
        )
        await self._wait_until(lambda: session._process.sent == ["initial task"])
        _append(session, _user_text("initial task"))
        await self._wait_until(lambda: session.active_turn_process is not None)

        assert await session.steer_active_turn("/exit") is False
        assert await session.steer_active_turn("bad\x1b[201~payload") is False
        assert await session.steer_active_turn(
            "stale process", expected_process=object()
        ) is False
        assert session._process.sent == ["initial task"]

        _append(session, _assistant_text("done"), _turn_duration())
        await turn

    @staticmethod
    async def _collect(stream):
        return [event async for event in stream]


class TestIdleWatcher:
    async def test_idle_watcher_drains_prefetched_handoff(self, tmp_path):
        session = _make_session(tmp_path)
        received: list = []

        async def capture(event):
            received.append(event)

        session.on_autonomous_event = capture
        session._prefetched_messages.append(
            _assistant_text("preserved after foreground exit")
        )

        watcher = asyncio.create_task(session._idle_watcher())
        try:
            deadline = asyncio.get_running_loop().time() + 1.0
            while not received:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("prefetched event was not handed off")
                await asyncio.sleep(0.01)
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher

        assert session._prefetched_messages == []
        assert received[0].content == "preserved after foreground exit"
        assert received[0].autonomous is True

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
