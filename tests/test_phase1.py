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
