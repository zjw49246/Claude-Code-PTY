"""Tests for pty_process.py — command building, path computation, MCP config."""

import json
import os
import tempfile

from claude_pty.config import PTYConfig
from claude_pty.pty_process import PTYProcess


class TestJsonlPath:
    def test_default_path(self):
        proc = PTYProcess(cwd="/home/user/project", session_id="abc-123")
        expected = os.path.expanduser(
            "~/.claude/projects/-home-user-project/abc-123.jsonl"
        )
        assert proc.jsonl_path == expected

    def test_custom_config_dir(self):
        proc = PTYProcess(
            cwd="/project",
            session_id="sess-1",
            config=PTYConfig(config_dir="/tmp/cc-config"),
        )
        assert proc.jsonl_path == "/tmp/cc-config/projects/-project/sess-1.jsonl"


class TestBuildCommand:
    def _proc(self, **kwargs):
        defaults = {"cwd": "/project", "session_id": "test-sid"}
        defaults.update(kwargs)
        return PTYProcess(**defaults)

    def test_basic_command(self):
        proc = self._proc()
        cmd = proc._build_command(None)
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "--session-id" in cmd
        assert cmd[cmd.index("--session-id") + 1] == "test-sid"

    def test_resume_command(self):
        proc = self._proc()
        cmd = proc._build_command("old-session")
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "old-session"
        assert "--session-id" not in cmd

    def test_model_and_effort(self):
        proc = self._proc(
            config=PTYConfig(default_model="opus", default_effort="high")
        )
        cmd = proc._build_command(None)
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "opus"
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == "high"

    def test_no_skip_permissions(self):
        proc = self._proc(config=PTYConfig(dangerously_skip_permissions=False))
        cmd = proc._build_command(None)
        assert "--dangerously-skip-permissions" not in cmd

    def test_channels_command(self):
        proc = self._proc(channel_inject_port=19100)
        cmd = proc._build_command(None)
        assert "--dangerously-load-development-channels" in cmd
        idx = cmd.index("--dangerously-load-development-channels")
        assert cmd[idx + 1] == "server:pty-bridge"

    def test_no_channels_by_default(self):
        proc = self._proc()
        cmd = proc._build_command(None)
        assert "--dangerously-load-development-channels" not in cmd


class TestChannelsEnabled:
    def test_enabled_with_port(self):
        proc = PTYProcess(cwd="/p", channel_inject_port=19100)
        assert proc.channels_enabled is True

    def test_disabled_by_default(self):
        proc = PTYProcess(cwd="/p")
        assert proc.channels_enabled is False


class TestSetupMcpConfig:
    def test_creates_mcp_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = PTYProcess(
                cwd=tmpdir,
                session_id="s1",
                channel_inject_port=19100,
                bridge_port=18000,
            )
            proc._setup_mcp_config()

            mcp_path = os.path.join(tmpdir, ".mcp.json")
            assert os.path.exists(mcp_path)

            with open(mcp_path) as f:
                data = json.load(f)

            server = data["mcpServers"]["pty-bridge"]
            assert "--port" in server["args"]
            assert "19100" in server["args"]
            assert "--bridge-port" in server["args"]
            assert "18000" in server["args"]
            assert "--session-id" in server["args"]
            assert "s1" in server["args"]

    def test_merges_existing_mcp_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mcp_path = os.path.join(tmpdir, ".mcp.json")
            existing = {"mcpServers": {"other-server": {"command": "other"}}}
            with open(mcp_path, "w") as f:
                json.dump(existing, f)

            proc = PTYProcess(
                cwd=tmpdir, session_id="s2", channel_inject_port=19200
            )
            proc._setup_mcp_config()

            with open(mcp_path) as f:
                data = json.load(f)

            assert "other-server" in data["mcpServers"]
            assert "pty-bridge" in data["mcpServers"]


class TestCwdNormalization:
    """cwd 必须归一化为绝对路径：jsonl_path 按字面推导，
    相对路径会推出错误的轮询目录（CCM PR-review task 实录）。"""

    def test_relative_dot_cwd_resolves_absolute(self, monkeypatch, tmp_path):
        from claude_pty.pty_process import PTYProcess
        import os, re
        monkeypatch.chdir(tmp_path)
        p = PTYProcess(cwd=".")
        assert os.path.isabs(p.cwd)
        expected = re.sub(r"[^A-Za-z0-9]", "-", str(tmp_path))
        assert f"/projects/{expected}/" in p.jsonl_path

    def test_absolute_cwd_unchanged(self):
        from claude_pty.pty_process import PTYProcess
        p = PTYProcess(cwd="/home/user/repo")
        assert p.cwd == "/home/user/repo"
