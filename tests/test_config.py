"""Tests for config.py — PTYConfig defaults and construction."""

from claude_pty.config import PTYConfig


class TestPTYConfig:
    def test_defaults(self):
        c = PTYConfig()
        assert c.claude_binary == "claude"
        assert c.dangerously_skip_permissions is True
        assert c.terminal_rows == 50
        assert c.terminal_cols == 200
        assert c.drain_interval == 0.05
        assert c.startup_wait == 8.0
        assert c.post_response_wait == 3.0
        assert c.response_timeout == 7200.0
        assert c.jsonl_poll_interval == 0.3
        assert c.max_sessions == 20
        assert c.idle_timeout == 300.0
        assert c.max_restart_attempts == 3
        assert c.default_model is None
        assert c.default_effort is None
        assert c.config_dir is None

    def test_override(self):
        c = PTYConfig(
            max_sessions=5,
            default_model="opus",
            startup_wait=15.0,
        )
        assert c.max_sessions == 5
        assert c.default_model == "opus"
        assert c.startup_wait == 15.0
        assert c.claude_binary == "claude"  # other defaults unchanged
