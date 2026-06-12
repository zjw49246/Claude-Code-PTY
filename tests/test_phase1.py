"""Phase 1 regression tests: JSONL path rule, turn_duration completion,
pre-trust workdir, Entertoconfirm auto-confirm, bracketed-paste send."""

import json
import os

import pytest

from claude_pty.config import PTYConfig
from claude_pty.jsonl_reader import JsonlReader
from claude_pty.pty_process import PTYProcess, _collapse_for_prompt_match


class TestJsonlPathRule:
    """CC's real rule (verified 2026-06-10 spike): every non-alphanumeric -> '-'."""

    def _path_for(self, cwd: str) -> str:
        proc = PTYProcess(cwd=cwd, session_id="sid-1")
        return os.path.basename(os.path.dirname(proc.jsonl_path))

    def test_slash_and_underscore(self):
        assert self._path_for("/tmp/foo_bar") == "-tmp-foo-bar"

    def test_dots_become_dashes(self):
        assert self._path_for("/tmp/pty_spike.v1/foo_bar.baz") == (
            "-tmp-pty-spike-v1-foo-bar-baz"
        )

    def test_spaces_and_symbols(self):
        assert self._path_for("/tmp/pty Spike@2/A b") == "-tmp-pty-Spike-2-A-b"

    def test_case_preserved(self):
        assert self._path_for("/home/Ubuntu/MyProj") == "-home-Ubuntu-MyProj"


class TestTurnDurationCompletion:
    """Interactive mode has no `result` event; the per-turn sentinel is
    system/turn_duration (written once, after all trailing messages)."""

    def setup_method(self):
        self.reader = JsonlReader("/nonexistent")

    def test_turn_duration_is_complete(self):
        raw = {"type": "system", "subtype": "turn_duration", "durationMs": 1234}
        assert self.reader.is_response_complete(raw) is True

    def test_end_turn_alone_is_not_complete(self):
        # end_turn appears on multiple messages of the same turn (thinking +
        # text blocks) — it must NOT terminate the event stream early.
        raw = {
            "type": "assistant",
            "message": {"stop_reason": "end_turn", "content": []},
        }
        assert self.reader.is_response_complete(raw) is False

    def test_other_system_subtypes_not_complete(self):
        raw = {"type": "system", "subtype": "init"}
        assert self.reader.is_response_complete(raw) is False


class TestNormalizeSkipsNoise:
    def setup_method(self):
        self.reader = JsonlReader("/nonexistent")

    @pytest.mark.parametrize(
        "msg_type",
        ["mode", "permission-mode", "file-history-snapshot", "attachment",
         "ai-title", "queue-operation", "last-prompt"],
    )
    def test_noise_types_skipped(self, msg_type):
        assert self.reader.normalize({"type": msg_type}) == []


class TestPretrustWorkdir:
    def test_writes_trust_entry(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"projects": {
            "/existing": {"hasTrustDialogAccepted": False, "allowedTools": ["X"]},
        }}))

        proc = PTYProcess(cwd="/some/workdir", session_id="sid-1")
        proc._pretrust_workdir(claude_json_path=str(claude_json))

        cfg = json.loads(claude_json.read_text())
        entry = cfg["projects"]["/some/workdir"]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasClaudeMdExternalIncludesApproved"] is True
        # existing entries untouched
        assert cfg["projects"]["/existing"]["allowedTools"] == ["X"]

    def test_preserves_existing_entry_fields(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"projects": {
            "/some/workdir": {"allowedTools": ["Bash"], "lastCost": 1.5},
        }}))

        proc = PTYProcess(cwd="/some/workdir", session_id="sid-1")
        proc._pretrust_workdir(claude_json_path=str(claude_json))

        entry = json.loads(claude_json.read_text())["projects"]["/some/workdir"]
        assert entry["allowedTools"] == ["Bash"]
        assert entry["lastCost"] == 1.5
        assert entry["hasTrustDialogAccepted"] is True

    def test_channels_pre_approves_mcp_server(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        proc = PTYProcess(
            cwd="/some/workdir", session_id="sid-1", channel_inject_port=19999
        )
        proc._pretrust_workdir(claude_json_path=str(claude_json))

        entry = json.loads(claude_json.read_text())["projects"]["/some/workdir"]
        assert "pty-bridge" in entry["enabledMcpjsonServers"]

    def test_marks_onboarding_complete(self, tmp_path):
        # First interactive run of a fresh config_dir must not hit the
        # theme picker (headless-provisioned dirs have no onboarding state).
        claude_json = tmp_path / ".claude.json"
        proc = PTYProcess(cwd="/w", session_id="sid-1")
        proc._pretrust_workdir(claude_json_path=str(claude_json))
        cfg = json.loads(claude_json.read_text())
        assert cfg["hasCompletedOnboarding"] is True
        assert cfg["theme"] == "dark"

    def test_existing_onboarding_state_preserved(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps(
            {"hasCompletedOnboarding": True, "theme": "light"}
        ))
        proc = PTYProcess(cwd="/w", session_id="sid-1")
        proc._pretrust_workdir(claude_json_path=str(claude_json))
        cfg = json.loads(claude_json.read_text())
        assert cfg["theme"] == "light"  # user choice untouched

    def test_missing_file_created(self, tmp_path):
        claude_json = tmp_path / "sub" / ".claude.json"
        proc = PTYProcess(cwd="/w", session_id="sid-1")
        proc._pretrust_workdir(claude_json_path=str(claude_json))
        assert json.loads(claude_json.read_text())["projects"]["/w"][
            "hasTrustDialogAccepted"] is True


class TestEnterToConfirmMatching:
    """CC renders TUI with cursor-positioning, so visible spaces vanish.
    Matching must strip ANSI and collapse all whitespace (Teleos approach)."""

    def test_collapses_ansi_and_whitespace(self):
        chunk = b"\x1b[2K\x1b[1G  Enter\x1b[7m to \x1b[0mconfirm \xc2\xb7 Esc to cancel"
        assert "Entertoconfirm" in _collapse_for_prompt_match(chunk)

    def test_dev_channels_dialog_matches(self):
        chunk = (b"WARNING: Loading development channels\r\n"
                 b"\x1b[1m 1. I am using this for local development\x1b[0m\r\n"
                 b" 2. Exit\r\n Enter to confirm \xc2\xb7 Esc to cancel")
        assert "Entertoconfirm" in _collapse_for_prompt_match(chunk)

    def test_normal_output_no_match(self):
        chunk = b"I ran the tests and they pass. Press any key."
        assert "Entertoconfirm" not in _collapse_for_prompt_match(chunk)


class TestDeliverPrompt:
    """Channel injection preferred, PTY stdin fallback."""

    def _make_session(self, bridge, inject_port):
        from claude_pty.session import Session

        session = Session(
            cwd="/w", bridge=bridge, channel_inject_port=inject_port
        )
        session._session_id = "sid-1"

        class FakeProc:
            session_id = "sid-1"
            sent: list = []

            def send_prompt(self, text):
                FakeProc.sent.append(text)

        FakeProc.sent = []
        session._process = FakeProc()
        return session, session._process

    async def test_channel_injection_used_when_available(self):
        class FakeBridge:
            calls = []

            def inject(self, sid, content, meta=None):
                FakeBridge.calls.append((sid, content))
                return True

        FakeBridge.calls = []
        session, proc = self._make_session(FakeBridge(), 19999)
        await session._deliver_prompt("hello")
        assert FakeBridge.calls == [("sid-1", "hello")]
        assert proc.sent == []  # stdin not touched

    async def test_falls_back_to_stdin_after_retries(self):
        class FailingBridge:
            calls = 0

            def inject(self, sid, content, meta=None):
                FailingBridge.calls += 1
                return False

        FailingBridge.calls = 0
        session, proc = self._make_session(FailingBridge(), 19999)
        session._INJECT_RETRY_INTERVAL = 0.01
        await session._deliver_prompt("hello")
        assert FailingBridge.calls == session._INJECT_ATTEMPTS
        assert proc.sent == ["hello"]

    async def test_stdin_direct_without_channels(self):
        session, proc = self._make_session(None, None)
        await session._deliver_prompt("hi")
        assert proc.sent == ["hi"]


class TestResumeExisting:
    """A session created with a pre-existing session_id must spawn with
    --resume, not --session-id (which would collide with the on-disk session)."""

    def test_session_resume_existing_uses_resume_flag(self):
        from claude_pty.session import Session

        s = Session(cwd="/w", session_id="existing-id", resume_existing=True)
        assert s._resume_existing is True

    async def test_pool_passes_resume(self, monkeypatch):
        from claude_pty.pool import SessionPool
        from claude_pty import session as session_mod

        captured = {}

        class FakeSession:
            def __init__(self, **kw):
                captured.update(kw)
                self.session_id = kw.get("session_id")
                self.is_alive = True

            async def start(self, initial_prompt=None):
                captured["initial_prompt"] = initial_prompt

        monkeypatch.setattr("claude_pty.pool.Session", FakeSession)
        pool = SessionPool()
        await pool.get_or_create(
            session_id="sid-x", cwd="/w", resume=True, initial_prompt="go"
        )
        assert captured["resume_existing"] is True
        assert captured["initial_prompt"] == "go"

    def test_start_resume_command(self):
        """Session.start with resume_existing must pass resume_id to spawn."""
        from claude_pty.pty_process import PTYProcess

        proc = PTYProcess(cwd="/w", session_id="abc")
        cmd = proc._build_command("abc")
        assert "--resume" in cmd and "abc" in cmd
        cmd2 = proc._build_command(None)
        assert "--session-id" in cmd2 and "--resume" not in cmd2


class TestDrainIdle:
    async def test_drain_stops_only_idle_sessions(self):
        import asyncio
        from claude_pty.pool import SessionPool

        pool = SessionPool()

        class FakeSession:
            def __init__(self, locked):
                self._send_lock = asyncio.Lock()
                self._locked = locked
                self.stopped = False
                self.is_alive = True
                self.idle_seconds = 0.0
                self.has_pending_subagents = False

            async def stop(self):
                self.stopped = True

        idle = FakeSession(locked=False)
        busy = FakeSession(locked=True)
        await busy._send_lock.acquire()  # mid-prompt
        pool._sessions = {"idle": idle, "busy": busy}
        pool._access_order = {"idle": 1.0, "busy": 2.0}

        stopped = await pool.drain_idle()
        assert stopped == 1
        assert idle.stopped is True
        assert busy.stopped is False
        assert list(pool._sessions) == ["busy"]


class TestReplyToolRemoved:
    """The pty_bridge_reply tool lured the model into 'sending' answers the
    user never saw (replies must flow through the conversation/JSONL)."""

    def test_tools_list_empty(self, monkeypatch, capsys):
        from claude_pty import channel_server
        import io, json as _json

        out = []
        monkeypatch.setattr(channel_server, "_write_message", out.append)
        channel_server._handle_tools_list({"jsonrpc": "2.0", "id": 1})
        assert out[0]["result"]["tools"] == []

    def test_instructions_say_reply_in_conversation(self, monkeypatch):
        from claude_pty import channel_server

        out = []
        monkeypatch.setattr(channel_server, "_write_message", out.append)
        channel_server._handle_initialize({"jsonrpc": "2.0", "id": 1})
        instructions = out[0]["result"]["instructions"]
        assert "Do NOT use any tool" in instructions
        assert "pty_bridge_reply" not in instructions


class TestNoSpawnTimeStdinSend:
    async def test_start_does_not_send_prompt_at_spawn(self, monkeypatch):
        """Cold resume must NOT write the prompt at spawn (TUI not ready,
        write gets swallowed -> turn never starts)."""
        from claude_pty.session import Session

        sent = []

        class FakeProc:
            session_id = "sid-1"
            pid = 1
            jsonl_path = "/tmp/nonexistent.jsonl"
            channels_enabled = False

            def __init__(self, **kw):
                pass

            def spawn(self, resume_id=None):
                pass

            def send_prompt(self, text):
                sent.append(text)

        monkeypatch.setattr("claude_pty.session.PTYProcess", lambda **kw: FakeProc())
        from claude_pty.config import PTYConfig

        s = Session(cwd="/w", session_id="sid-1",
                    config=PTYConfig(startup_wait=0.01), resume_existing=True)
        s._restart_count = 1  # force resume path
        await s.start(initial_prompt="hello")
        assert sent == []  # nothing written at spawn time


class TestRateLimitDetection:
    """PTY-mode rate-limit detection (Phase 3): banner scan + JSONL signals."""

    def test_collapsed_banner_matches(self):
        from claude_pty.pty_process import _match_rate_limit, _collapse_for_prompt_match

        # 真实文案经 TUI 光标排版后空格消失
        chunk = b"\x1b[2K You've hit \x1b[1myour session limit\x1b[0m \xc2\xb7 resets 5:50pm (UTC)"
        collapsed = _collapse_for_prompt_match(chunk).lower()
        assert _match_rate_limit(collapsed)

    def test_weekly_and_usage_wordings(self):
        from claude_pty.pty_process import _match_rate_limit

        assert _match_rate_limit("you'vehityourweeklylimit·resets8am")
        assert _match_rate_limit("usagelimitreached")

    def test_normal_output_not_matched(self):
        from claude_pty.pty_process import _match_rate_limit

        assert not _match_rate_limit("iimplementedtheratelimitermiddleware")

    def test_normalize_rate_limit_event(self):
        from claude_pty.jsonl_reader import JsonlReader
        from claude_pty.events import EventType

        reader = JsonlReader("/nonexistent")
        events = reader.normalize({"type": "rate_limit_event",
                                   "rate_limit_info": {"status": "rejected"}})
        assert len(events) == 1
        assert events[0].event_type == EventType.SYSTEM_EVENT
        assert events[0].is_error is True

    async def test_session_ends_turn_on_banner(self, tmp_path):
        """横幅 + turn 零 JSONL 输出（真撞限签名）：静默确认期过后以错误事件结束。"""
        import asyncio
        from claude_pty.session import Session
        from claude_pty.config import PTYConfig
        from claude_pty.events import EventType

        session = Session(cwd="/w", session_id="sid-rl",
                          config=PTYConfig(response_timeout=10, post_response_wait=0,
                                           jsonl_poll_interval=0.01,
                                           rate_limit_confirm_quiet=0.05))

        class FakeReader:
            def read_new_messages(self): return []
            def normalize(self, raw, include_user_text=False):
                return []
            def is_prompt_echo(self, raw, prompt):
                c = (raw.get("message") or {}).get("content")
                return (raw.get("type") == "user"
                        and isinstance(c, str) and prompt in c)
            def is_response_complete(self, raw): return False

        class FakeProc:
            session_id = "sid-rl"
            is_alive = True
            exit_code = None
            rate_limited = True  # banner detected

            def send_prompt(self, text): pass
            def clear_rate_limited(self): self.rate_limited = False

        session._process = FakeProc()
        session._reader = FakeReader()
        session._started = True

        events = []
        async for ev in session.send_prompt("hello"):
            events.append(ev)
        assert any(ev.is_error and "usage limit reached" in (ev.content or "")
                   for ev in events)
        assert session.rate_limited is True

    async def test_banner_false_positive_when_jsonl_flowing(self):
        """横幅标记被 TUI 渲染的对话正文误中（CCM task 81/82 事故）：turn 有
        JSONL 活动时不得判限流，应清 flag 并正常完成 turn。"""
        from claude_pty.session import Session
        from claude_pty.config import PTYConfig

        session = Session(cwd="/w", session_id="sid-fp",
                          config=PTYConfig(response_timeout=10, post_response_wait=0,
                                           jsonl_poll_interval=0.01,
                                           rate_limit_confirm_quiet=0.05))

        class FakeReader:
            """第一轮吐 tool_result（内容含 limit 字样），第二轮吐哨兵。"""
            def __init__(self):
                self.batches = [
                    [],  # 投递前的 backlog drain
                    [{"type": "user", "message": {"content": "hello"}},
                     {"type": "user", "toolUseResult": True}],
                    [{"type": "system", "subtype": "turn_duration"}],
                ]
            def read_new_messages(self):
                return self.batches.pop(0) if self.batches else []
            def normalize(self, raw, include_user_text=False):
                return []
            def is_prompt_echo(self, raw, prompt):
                c = (raw.get("message") or {}).get("content")
                return (raw.get("type") == "user"
                        and isinstance(c, str) and prompt in c)
            def is_response_complete(self, raw):
                return raw.get("subtype") == "turn_duration"

        class FakeProc:
            session_id = "sid-fp"
            is_alive = True
            exit_code = None
            rate_limited = True  # drain loop 误中渲染正文

            def send_prompt(self, text): pass
            def clear_rate_limited(self): self.rate_limited = False

        proc = FakeProc()
        session._process = proc
        session._reader = FakeReader()
        session._started = True

        events = []
        async for ev in session.send_prompt("hello"):
            events.append(ev)
        assert not any(ev.is_error and "usage limit" in (ev.content or "")
                       for ev in events)
        assert proc.rate_limited is False  # 误报已清除
        assert session.rate_limited is False

    async def test_banner_at_turn_end_does_not_poison_next_turn(self):
        """turn 末尾才误中横幅、message loop 直接 break：完成时必须清残留
        flag，否则下一 turn 开局零 JSONL 会被误判真撞限。"""
        from claude_pty.session import Session
        from claude_pty.config import PTYConfig

        session = Session(cwd="/w", session_id="sid-st",
                          config=PTYConfig(response_timeout=10, post_response_wait=0,
                                           jsonl_poll_interval=0.01,
                                           rate_limit_confirm_quiet=0.05))

        class FakeReader:
            def __init__(self):
                self.batches = [
                    [],  # 投递前的 backlog drain
                    [{"type": "user", "message": {"content": "hello"}},
                     {"type": "system", "subtype": "turn_duration"}],
                ]
            def read_new_messages(self):
                return self.batches.pop(0) if self.batches else []
            def normalize(self, raw, include_user_text=False):
                return []
            def is_prompt_echo(self, raw, prompt):
                c = (raw.get("message") or {}).get("content")
                return (raw.get("type") == "user"
                        and isinstance(c, str) and prompt in c)
            def is_response_complete(self, raw):
                return raw.get("subtype") == "turn_duration"

        class FakeProc:
            session_id = "sid-st"
            is_alive = True
            exit_code = None
            rate_limited = True  # turn 末尾误中，banner 分支没机会跑

            def send_prompt(self, text): pass
            def clear_rate_limited(self): self.rate_limited = False

        proc = FakeProc()
        session._process = proc
        session._reader = FakeReader()
        session._started = True

        async for _ in session.send_prompt("hello"):
            pass
        assert proc.rate_limited is False
        assert session.rate_limited is False

    async def test_structured_jsonl_signal_trusted_immediately(self):
        """JSONL 结构化 rate_limit_event 不需要静默确认，立即结束 turn。"""
        from claude_pty.session import Session
        from claude_pty.config import PTYConfig

        session = Session(cwd="/w", session_id="sid-js",
                          config=PTYConfig(response_timeout=10, post_response_wait=0,
                                           jsonl_poll_interval=0.01,
                                           rate_limit_confirm_quiet=60.0))

        class FakeReader:
            def __init__(self):
                self.batches = [
                    [],  # 投递前的 backlog drain
                    [{"type": "rate_limit_event",
                      "rate_limit_info": {"status": "rejected"}}],
                ]
            def read_new_messages(self):
                return self.batches.pop(0) if self.batches else []
            def normalize(self, raw, include_user_text=False):
                return []
            def is_prompt_echo(self, raw, prompt):
                c = (raw.get("message") or {}).get("content")
                return (raw.get("type") == "user"
                        and isinstance(c, str) and prompt in c)
            def is_response_complete(self, raw): return False

        class FakeProc:
            session_id = "sid-js"
            is_alive = True
            exit_code = None
            rate_limited = False

            def send_prompt(self, text): pass
            def clear_rate_limited(self): self.rate_limited = False

        session._process = FakeProc()
        session._reader = FakeReader()
        session._started = True

        events = []
        async for ev in session.send_prompt("hello"):
            events.append(ev)
        assert any(ev.is_error and "usage limit reached" in (ev.content or "")
                   for ev in events)
        assert session.rate_limited is True

    async def test_consumer_exit_code_nonzero_on_rate_limit(self):
        from claude_pty.adapters.base import BasePTYBackend

        captured = {}

        class FakeBackend(BasePTYBackend):
            async def on_exit(self, key, exit_code, **ctx):
                captured["exit_code"] = exit_code

        class FakeProc:
            exit_code = None  # 进程还活着

        class FakeSession:
            rate_limited = True
            _process = FakeProc()

            async def send_prompt(self, prompt):
                return
                yield  # pragma: no cover

        backend = FakeBackend()
        await backend._consume("k", FakeSession(), "hi")
        assert captured["exit_code"] == 1

    async def test_pool_recreates_on_config_dir_change(self, monkeypatch):
        from claude_pty.pool import SessionPool
        from claude_pty.config import PTYConfig

        created = []

        class FakeSession:
            def __init__(self, **kw):
                self.config = kw.get("config")
                self.session_id = kw.get("session_id")
                self.is_alive = True
                self.stopped = False
                created.append(self)

            async def start(self, initial_prompt=None): pass
            async def stop(self): self.stopped = True

        monkeypatch.setattr("claude_pty.pool.Session", FakeSession)
        pool = SessionPool()
        s1 = await pool.get_or_create("sid-1", "/w",
                                      config_override=PTYConfig(config_dir="/acct-1"))
        # 同 config_dir → 复用
        s_same = await pool.get_or_create("sid-1", "/w",
                                          config_override=PTYConfig(config_dir="/acct-1"))
        assert s_same is s1
        # 换号（config_dir 变化）→ 停旧建新
        s2 = await pool.get_or_create("sid-1", "/w",
                                      config_override=PTYConfig(config_dir="/acct-2"))
        assert s2 is not s1
        assert s1.stopped is True
        assert len(created) == 2


class TestApiErrorTurnAbort:
    """isApiErrorMessage: API 掐断 turn 时不会有 turn_duration 哨兵。

    生产实录（task 80, session 29106cf4）：turn 中途 API 返回 Usage Policy
    错误，JSONL 最后一条是 isApiErrorMessage=true 的 assistant 消息，之后
    再无输出 —— 轮询层若只等哨兵就会静默挂到 response_timeout。
    """

    def test_normalize_marks_api_error_as_error(self):
        from claude_pty.events import EventType

        reader = JsonlReader("/nonexistent")
        events = reader.normalize({
            "type": "assistant",
            "isApiErrorMessage": True,
            "message": {"content": [{"type": "text",
                                     "text": "API Error: Usage Policy"}]},
        })
        assert len(events) == 1
        assert events[0].event_type == EventType.MESSAGE
        assert events[0].is_error is True

    async def test_session_ends_turn_on_api_error(self):
        from claude_pty.session import Session
        from claude_pty.events import EventType

        session = Session(cwd="/w", session_id="sid-ae",
                          config=PTYConfig(response_timeout=10,
                                           post_response_wait=0,
                                           jsonl_poll_interval=0.01))

        class FakeReader:
            step = 0

            def read_new_messages(self):
                self.step += 1
                if self.step == 1:
                    return []  # 投递前的 backlog drain
                if self.step == 2:
                    return [{
                        "type": "assistant",
                        "isApiErrorMessage": True,
                        "message": {"content": [{"type": "text",
                                                 "text": "API Error: x"}]},
                    }]
                return []

            def normalize(self, raw, include_user_text=False):
                return []

            def is_prompt_echo(self, raw, prompt):
                c = (raw.get("message") or {}).get("content")
                return (raw.get("type") == "user"
                        and isinstance(c, str) and prompt in c)

            def is_response_complete(self, raw):
                return False

        class FakeProc:
            session_id = "sid-ae"
            is_alive = True
            exit_code = None
            rate_limited = False

            def send_prompt(self, text):
                pass

        session._process = FakeProc()
        session._reader = FakeReader()
        session._started = True

        events = []
        async for ev in session.send_prompt("hello"):
            events.append(ev)
        # turn 以 api_error 错误事件提前结束，而不是等到 response_timeout
        assert any(ev.is_error and "api_error" in (ev.content or "")
                   for ev in events)
        assert not any("timed out" in (ev.content or "") for ev in events)


class TestInjectDeliveryConfirm:
    """channel 注入“成功”≠ CC 真的收到了。

    生产实录（task 80, 04:47）：resume spawn 后 13ms 注入返回 HTTP 200，
    但 CC 仍在初始化，notification 被丢弃 —— JSONL 永远没有 user 事件，
    消息黑洞 30 分钟。修复：注入后 N 秒内 JSONL 无任何活动 → stdin 重投。
    """

    def _make_session(self, reader, proc, bridge):
        from claude_pty.session import Session

        session = Session(cwd="/w", session_id=proc.session_id,
                          config=PTYConfig(response_timeout=10,
                                           post_response_wait=0,
                                           jsonl_poll_interval=0.01,
                                           inject_confirm_timeout=0.05),
                          bridge=bridge, channel_inject_port=12345)
        session._process = proc
        session._reader = reader
        session._started = True
        return session

    async def test_fallback_to_stdin_when_no_jsonl_activity(self):
        stdin_calls = []

        class FakeReader:
            unblocked = False

            def read_new_messages(self):
                if self.unblocked:
                    return [{"type": "user", "message": {"content": "hello"}},
                            {"type": "system", "subtype": "turn_duration"}]
                return []

            def normalize(self, raw, include_user_text=False):
                return []

            def is_prompt_echo(self, raw, prompt):
                c = (raw.get("message") or {}).get("content")
                return (raw.get("type") == "user"
                        and isinstance(c, str) and prompt in c)

            def is_response_complete(self, raw):
                return raw.get("subtype") == "turn_duration"

        reader = FakeReader()

        class FakeProc:
            session_id = "sid-ic"
            is_alive = True
            exit_code = None
            rate_limited = False

            def send_prompt(self, text):
                stdin_calls.append(text)
                reader.unblocked = True

        class FakeBridge:
            def inject(self, session_id, content, meta=None):
                return True  # HTTP 200 — 但 CC 实际丢弃了

        session = self._make_session(reader, FakeProc(), FakeBridge())
        events = []
        async for ev in session.send_prompt("hello"):
            events.append(ev)

        assert stdin_calls == ["hello"]
        assert not any(ev.is_error for ev in events)

    async def test_no_fallback_when_turn_starts(self):
        stdin_calls = []

        class FakeReader:
            step = 0

            def read_new_messages(self):
                self.step += 1
                if self.step == 1:
                    return []  # 投递前的 backlog drain
                if self.step == 2:
                    return [{"type": "user",
                             "message": {"content": "hello"}}]
                if self.step == 3:
                    return [{"type": "system", "subtype": "turn_duration"}]
                return []

            def normalize(self, raw, include_user_text=False):
                return []

            def is_prompt_echo(self, raw, prompt):
                c = (raw.get("message") or {}).get("content")
                return (raw.get("type") == "user"
                        and isinstance(c, str) and prompt in c)

            def is_response_complete(self, raw):
                return raw.get("subtype") == "turn_duration"

        class FakeProc:
            session_id = "sid-ok"
            is_alive = True
            exit_code = None
            rate_limited = False

            def send_prompt(self, text):
                stdin_calls.append(text)

        class FakeBridge:
            def inject(self, session_id, content, meta=None):
                return True

        session = self._make_session(FakeReader(), FakeProc(), FakeBridge())
        async for _ in session.send_prompt("hello"):
            pass

        assert stdin_calls == []  # turn 已启动，不应 stdin 重投
