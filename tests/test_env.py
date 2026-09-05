"""Tests for _env.py — environment variable cleaning."""

import os
from unittest.mock import patch

from claude_pty._env import build_clean_env
from claude_pty.config import PTYConfig


class TestBuildCleanEnv:
    def test_removes_claude_vars(self):
        fake_env = {
            "PATH": "/usr/bin",
            "CLAUDE_API_KEY": "secret",
            "CLAUDECODE_DEBUG": "1",
            "MY_AI_AGENT_TOKEN": "token",
            "HOME": "/home/user",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            env = build_clean_env(PTYConfig())

        assert "CLAUDE_API_KEY" not in env
        assert "CLAUDECODE_DEBUG" not in env
        assert "MY_AI_AGENT_TOKEN" not in env
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/user"

    def test_sets_terminal_vars(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            env = build_clean_env(PTYConfig())

        assert env["TERM"] == "xterm-256color"
        assert env["LANG"] == "en_US.UTF-8"
        assert env["LC_ALL"] == "en_US.UTF-8"

    def test_overrides_existing_term(self):
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            env = build_clean_env(PTYConfig())

        assert env["TERM"] == "xterm-256color"

    def test_config_dir_set(self):
        with patch.dict(os.environ, {}, clear=True):
            env = build_clean_env(PTYConfig(config_dir="/tmp/claude-config"))

        assert env["CLAUDE_CONFIG_DIR"] == "/tmp/claude-config"

    def test_config_dir_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            env = build_clean_env(PTYConfig())

        assert "CLAUDE_CONFIG_DIR" not in env

    def test_case_insensitive_cleaning(self):
        fake_env = {"claude_lower": "val", "Claude_Mixed": "val2"}
        with patch.dict(os.environ, fake_env, clear=True):
            env = build_clean_env(PTYConfig())

        assert "claude_lower" not in env
        assert "Claude_Mixed" not in env


class TestClaudeJsonPath:
    """claude_json_path must name the file CC will actually read, in lockstep
    with build_clean_env's CLAUDE_CONFIG_DIR decision (CCM task 752: trust
    entries written to ~/.claude/.claude.json while CC read ~/.claude.json
    brought the startup dialogs back and wedged headless launches)."""

    def test_default_config_dir_uses_home_file(self, tmp_path):
        from claude_pty._env import claude_json_path

        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
            cfg = PTYConfig(config_dir=str(home / ".claude"))
            # build_clean_env would NOT export CLAUDE_CONFIG_DIR here...
            assert "CLAUDE_CONFIG_DIR" not in build_clean_env(cfg)
            # ...so CC reads $HOME/.claude.json and pretrust must write there.
            assert claude_json_path(cfg) == str(home / ".claude.json")

    def test_no_config_dir_uses_home_file(self, tmp_path):
        from claude_pty._env import claude_json_path

        with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
            assert claude_json_path(PTYConfig()) == str(
                tmp_path / ".claude.json"
            )

    def test_custom_config_dir_uses_its_file(self, tmp_path):
        from claude_pty._env import claude_json_path

        with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
            cfg = PTYConfig(config_dir="/tmp/claude-pool/account-2")
            assert claude_json_path(cfg) == "/tmp/claude-pool/account-2/.claude.json"

    def test_env_override_wins(self, tmp_path):
        from claude_pty._env import claude_json_path

        with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
            cfg = PTYConfig(
                config_dir=str(tmp_path / ".claude"),
                env_overrides={"CLAUDE_CONFIG_DIR": "/srv/claude-x"},
            )
            assert claude_json_path(cfg) == "/srv/claude-x/.claude.json"

    def test_symlinked_default_dir_uses_home_file(self, tmp_path):
        from claude_pty._env import claude_json_path

        home = tmp_path / "home"
        real = tmp_path / "real-claude"
        real.mkdir()
        home.mkdir()
        (home / ".claude").symlink_to(real)
        with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
            cfg = PTYConfig(config_dir=str(real))
            assert "CLAUDE_CONFIG_DIR" not in build_clean_env(cfg)
            assert claude_json_path(cfg) == str(home / ".claude.json")
