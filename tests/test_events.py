"""Tests for events.py — PTYEvent and EventType."""

from claude_pty.events import PTYEvent, EventType


class TestEventType:
    def test_all_values(self):
        expected = {
            "system_init", "system_event", "message", "thinking",
            "tool_use", "tool_result", "result", "process_exit",
            "parse_error", "session_started", "session_crashed",
            "session_resumed",
            "subagent_spawn", "subagent_progress", "subagent_done",
        }
        actual = {e.value for e in EventType}
        assert actual == expected

    def test_string_enum(self):
        assert EventType.MESSAGE == "message"
        assert str(EventType.TOOL_USE) == "EventType.TOOL_USE"


class TestPTYEvent:
    def test_to_dict_required_fields(self):
        event = PTYEvent(event_type="message", role="assistant", content="hello")
        d = event.to_dict()
        assert d["event_type"] == "message"
        assert d["role"] == "assistant"
        assert d["content"] == "hello"
        assert d["is_error"] is False
        assert "timestamp" in d
        assert d["tool_name"] is None
        assert d["tool_input"] is None
        assert d["tool_output"] is None

    def test_to_dict_excludes_none_optional(self):
        event = PTYEvent(event_type="message")
        d = event.to_dict()
        assert "session_id" not in d
        assert "cost_usd" not in d
        assert "context_usage" not in d

    def test_to_dict_includes_set_optional(self):
        event = PTYEvent(
            event_type="result",
            session_id="abc-123",
            cost_usd=0.05,
            context_usage={"input": 100, "output": 50},
        )
        d = event.to_dict()
        assert d["session_id"] == "abc-123"
        assert d["cost_usd"] == 0.05
        assert d["context_usage"] == {"input": 100, "output": 50}

    def test_error_event(self):
        event = PTYEvent(
            event_type="session_crashed",
            content="Process died",
            is_error=True,
        )
        d = event.to_dict()
        assert d["is_error"] is True
        assert d["content"] == "Process died"

    def test_timestamp_auto_set(self):
        event = PTYEvent(event_type="message")
        assert event.timestamp is not None
        assert "T" in event.timestamp  # ISO format
