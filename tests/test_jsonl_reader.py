import json
import os
import tempfile

from claude_pty import JsonlReader, EventType


def _write_jsonl(path: str, objects: list[dict]) -> None:
    with open(path, "w") as f:
        for obj in objects:
            f.write(json.dumps(obj) + "\n")


def _append_jsonl(path: str, obj: dict, trailing_newline: bool = True) -> None:
    with open(path, "a") as f:
        line = json.dumps(obj)
        if trailing_newline:
            f.write(line + "\n")
        else:
            f.write(line[:len(line) // 2])


class TestReadNewMessages:
    def test_reads_complete_lines(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        _write_jsonl(path, [{"type": "user", "message": {"content": "hi"}}])

        reader = JsonlReader(path)
        msgs = reader.read_new_messages()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "user"

    def test_incremental_reads(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        _write_jsonl(path, [{"type": "user", "message": {"content": "first"}}])

        reader = JsonlReader(path)
        msgs1 = reader.read_new_messages()
        assert len(msgs1) == 1

        _append_jsonl(path, {"type": "user", "message": {"content": "second"}})
        msgs2 = reader.read_new_messages()
        assert len(msgs2) == 1
        assert msgs2[0]["message"]["content"] == "second"

    def test_handles_partial_writes(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        obj = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn"}}
        full_line = json.dumps(obj)

        with open(path, "w") as f:
            f.write(full_line[:20])

        reader = JsonlReader(path)
        msgs = reader.read_new_messages()
        assert len(msgs) == 0

        with open(path, "a") as f:
            f.write(full_line[20:] + "\n")

        msgs = reader.read_new_messages()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "assistant"

    def test_nonexistent_file(self):
        reader = JsonlReader("/nonexistent/path.jsonl")
        assert reader.read_new_messages() == []


class TestNormalize:
    def test_assistant_text_message(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello world"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                },
            },
            "sessionId": "sess-123",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = reader.normalize(raw)
        assert len(events) == 1
        e = events[0]
        assert e.event_type == EventType.MESSAGE
        assert e.role == "assistant"
        assert e.content == "Hello world"
        assert e.session_id == "sess-123"
        assert e.context_usage is not None
        assert e.context_usage["total_input_tokens"] == 115

    def test_assistant_tool_use(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me check"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
                "stop_reason": "tool_use",
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = reader.normalize(raw)
        assert len(events) == 2
        assert events[0].event_type == EventType.MESSAGE
        assert events[0].content == "Let me check"
        assert events[1].event_type == EventType.TOOL_USE
        assert events[1].tool_name == "Bash"
        assert events[1].tool_input == '{"command": "ls"}'

    def test_user_tool_result(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "file1.py\nfile2.py",
                        "is_error": False,
                    }
                ]
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = reader.normalize(raw)
        assert len(events) == 1
        assert events[0].event_type == EventType.TOOL_RESULT
        assert events[0].role == "tool"
        assert events[0].tool_output == "file1.py\nfile2.py"
        assert events[0].is_error is False

    def test_user_tool_result_with_error(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "command failed", "is_error": True}
                ]
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = reader.normalize(raw)
        assert len(events) == 1
        assert events[0].is_error is True

    def test_user_tool_result_list_content(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}],
                    }
                ]
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = reader.normalize(raw)
        assert events[0].tool_output == "line1\nline2"

    def test_thinking_block(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": "Let me consider..."}],
                "stop_reason": "end_turn",
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = reader.normalize(raw)
        assert len(events) == 1
        assert events[0].event_type == EventType.THINKING
        assert events[0].content == "Let me consider..."

    def test_encrypted_thinking(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "signature": "abc", "data": "xyz"}],
                "stop_reason": "end_turn",
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = reader.normalize(raw)
        assert "encrypted" in events[0].content

    def test_system_init(self):
        reader = JsonlReader("/dev/null")
        raw = {"type": "system", "subtype": "init", "session_id": "sess-456"}
        events = reader.normalize(raw)
        assert len(events) == 1
        assert events[0].event_type == EventType.SYSTEM_INIT
        assert events[0].session_id == "sess-456"

    def test_skip_noisy_system_events(self):
        reader = JsonlReader("/dev/null")
        for subtype in ("thinking_tokens", "token_usage", "api_request", "api_response"):
            events = reader.normalize({"type": "system", "subtype": subtype})
            assert events == []

    def test_skip_non_message_types(self):
        reader = JsonlReader("/dev/null")
        for t in ("queue-operation", "attachment", "ai-title", "last-prompt"):
            assert reader.normalize({"type": t}) == []

    def test_result_event(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "result",
            "session_id": "sess-789",
            "total_cost_usd": 0.05,
            "content": "Done",
        }
        events = reader.normalize(raw)
        assert len(events) == 1
        assert events[0].event_type == EventType.RESULT
        assert events[0].session_id == "sess-789"
        assert events[0].cost_usd == 0.05


class TestIsResponseComplete:
    def test_end_turn(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "assistant",
            "message": {"stop_reason": "end_turn"},
        }
        assert reader.is_response_complete(raw) is True

    def test_tool_use_not_complete(self):
        reader = JsonlReader("/dev/null")
        raw = {
            "type": "assistant",
            "message": {"stop_reason": "tool_use"},
        }
        assert reader.is_response_complete(raw) is False

    def test_user_message_not_complete(self):
        reader = JsonlReader("/dev/null")
        assert reader.is_response_complete({"type": "user"}) is False


class TestToDict:
    def test_matches_stream_parser_shape(self):
        """Verify to_dict output has exactly the keys CCM expects."""
        from claude_pty import PTYEvent

        evt = PTYEvent(
            event_type="message",
            role="assistant",
            content="hello",
            raw_json="{}",
        )
        d = evt.to_dict()
        required_keys = {
            "event_type", "role", "content", "tool_name", "tool_input",
            "tool_output", "raw_json", "is_error", "timestamp",
        }
        assert required_keys.issubset(set(d.keys()))

    def test_optional_fields_included_when_set(self):
        from claude_pty import PTYEvent

        evt = PTYEvent(
            event_type="result",
            session_id="s1",
            cost_usd=0.01,
            context_usage={"input_tokens": 100},
        )
        d = evt.to_dict()
        assert d["session_id"] == "s1"
        assert d["cost_usd"] == 0.01
        assert d["context_usage"]["input_tokens"] == 100

    def test_optional_fields_excluded_when_none(self):
        from claude_pty import PTYEvent

        evt = PTYEvent(event_type="message")
        d = evt.to_dict()
        assert "session_id" not in d
        assert "cost_usd" not in d
        assert "context_usage" not in d
