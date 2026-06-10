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
