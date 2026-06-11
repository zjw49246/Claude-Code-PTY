from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import AsyncIterator

from .events import PTYEvent, EventType


# JSONL message types that carry no useful event data
_SKIP_TYPES = frozenset(
    {
        "queue-operation", "attachment", "ai-title", "last-prompt",
        "mode", "permission-mode", "file-history-snapshot",
    }
)

# System subtypes that are noisy telemetry
_SKIP_SUBTYPES = frozenset(
    {"thinking_tokens", "token_usage", "api_request", "api_response"}
)


class JsonlReader:
    """Reads Claude Code session JSONL files and normalizes to PTYEvent.

    Handles partial-write safety via line buffering. Normalizes interactive-mode
    JSONL into events structurally identical to CCM's StreamParser.parse_line().
    """

    def __init__(self, path: str):
        self.path = path
        self._offset: int = 0
        self._buffer: str = ""

    def read_new_messages(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []

        with open(self.path, encoding="utf-8") as f:
            f.seek(self._offset)
            new_data = f.read()

        if not new_data:
            return []

        # Always advance file offset by what was actually read
        self._offset += len(new_data.encode("utf-8"))

        combined = self._buffer + new_data
        lines = combined.split("\n")

        self._buffer = lines[-1]
        complete_lines = lines[:-1]

        results = []
        for line in complete_lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                results.append(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return results

    def normalize(self, raw: dict) -> list[PTYEvent]:
        """Normalize a single interactive-mode JSONL message into PTYEvent(s).

        The output matches CCM's StreamParser.parse_line() event structure.
        """
        msg_type = raw.get("type", "")
        now = raw.get(
            "timestamp", datetime.now(timezone.utc).isoformat()
        )
        raw_json = json.dumps(raw)

        if msg_type in _SKIP_TYPES:
            return []

        # Structured rate-limit signal (observed in -p stream; interactive
        # JSONL may or may not record it — PTY output scan is the fallback)
        if msg_type == "rate_limit_event":
            return [
                PTYEvent(
                    event_type=EventType.SYSTEM_EVENT,
                    content="rate_limit_event",
                    is_error=True,
                    raw_json=raw_json,
                    timestamp=now,
                    session_id=raw.get("sessionId") or raw.get("session_id"),
                )
            ]

        # Interactive JSONL nests content under "message"
        message = raw.get("message", {})
        if not isinstance(message, dict):
            message = {}
        session_id = raw.get("sessionId") or raw.get("session_id")

        if msg_type == "system":
            subtype = raw.get("subtype", "system")
            if subtype == "init":
                return [
                    PTYEvent(
                        event_type=EventType.SYSTEM_INIT,
                        session_id=raw.get("session_id") or session_id,
                        raw_json=raw_json,
                        timestamp=now,
                    )
                ]
            if subtype in _SKIP_SUBTYPES:
                return []
            return [
                PTYEvent(
                    event_type=EventType.SYSTEM_EVENT,
                    content=subtype,
                    raw_json=raw_json,
                    timestamp=now,
                    session_id=session_id,
                )
            ]

        if msg_type == "result":
            return self._normalize_result(raw, raw_json, now, session_id)

        if msg_type == "assistant":
            events = self._normalize_assistant(message, raw_json, now, session_id)
            # CC records upstream API failures (e.g. usage-policy rejections)
            # as a synthetic assistant message; the turn is aborted after it.
            if raw.get("isApiErrorMessage"):
                for event in events:
                    event.is_error = True
            return events

        if msg_type == "user":
            return self._normalize_user(message, raw_json, now, session_id)

        return []

    def _normalize_result(
        self, raw: dict, raw_json: str, now: str, session_id: str | None
    ) -> list[PTYEvent]:
        event = PTYEvent(
            event_type=EventType.RESULT,
            content=self._extract_content(raw),
            raw_json=raw_json,
            timestamp=now,
            session_id=raw.get("session_id") or session_id,
        )
        cost = raw.get("total_cost_usd")
        if cost is not None:
            event.cost_usd = cost

        model_usage = raw.get("modelUsage")
        if isinstance(model_usage, dict):
            for _model_name, model_data in model_usage.items():
                if isinstance(model_data, dict) and "contextWindow" in model_data:
                    usage = raw.get("usage", {})
                    if isinstance(usage, dict):
                        inp = usage.get("input_tokens", 0)
                        cr = usage.get("cache_read_input_tokens", 0)
                        cc = usage.get("cache_creation_input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        event.context_usage = {
                            "input_tokens": inp,
                            "cache_read_input_tokens": cr,
                            "cache_creation_input_tokens": cc,
                            "output_tokens": out,
                            "total_input_tokens": inp + cr + cc,
                            "context_window": model_data["contextWindow"],
                        }
                    break

        if raw.get("is_error"):
            event.is_error = True
        return [event]

    def _normalize_assistant(
        self, message: dict, raw_json: str, now: str, session_id: str | None
    ) -> list[PTYEvent]:
        usage = message.get("usage")
        usage_data = None
        if isinstance(usage, dict):
            inp = usage.get("input_tokens", 0)
            cr = usage.get("cache_read_input_tokens", 0)
            cc = usage.get("cache_creation_input_tokens", 0)
            out = usage.get("output_tokens", 0)
            usage_data = {
                "input_tokens": inp,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
                "output_tokens": out,
                "total_input_tokens": inp + cr + cc,
            }

        content_blocks = message.get("content", [])
        if not isinstance(content_blocks, list):
            evt = PTYEvent(
                event_type=EventType.MESSAGE,
                role="assistant",
                raw_json=raw_json,
                timestamp=now,
                session_id=session_id,
            )
            if usage_data:
                evt.context_usage = usage_data
            return [evt]

        events: list[PTYEvent] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                events.append(
                    PTYEvent(
                        event_type=EventType.MESSAGE,
                        role="assistant",
                        content=block.get("text", ""),
                        raw_json=raw_json,
                        timestamp=now,
                        session_id=session_id,
                    )
                )
            elif block_type == "tool_use":
                events.append(
                    PTYEvent(
                        event_type=EventType.TOOL_USE,
                        role="assistant",
                        tool_name=block.get("name"),
                        tool_input=json.dumps(block.get("input", {})),
                        raw_json=raw_json,
                        timestamp=now,
                        session_id=session_id,
                    )
                )
            elif block_type == "thinking":
                events.append(
                    PTYEvent(
                        event_type=EventType.THINKING,
                        role="assistant",
                        content=_extract_thinking_text(block),
                        raw_json=raw_json,
                        timestamp=now,
                        session_id=session_id,
                    )
                )

        if not events:
            evt = PTYEvent(
                event_type=EventType.MESSAGE,
                role="assistant",
                raw_json=raw_json,
                timestamp=now,
                session_id=session_id,
            )
            if usage_data:
                evt.context_usage = usage_data
            return [evt]

        if usage_data and events:
            events[0].context_usage = usage_data
        return events

    def _normalize_user(
        self, message: dict, raw_json: str, now: str, session_id: str | None
    ) -> list[PTYEvent]:
        msg_content = message.get("content", [])
        if not isinstance(msg_content, list):
            return []

        events: list[PTYEvent] = []
        for block in msg_content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw_content = block.get("content", "")
            if isinstance(raw_content, list):
                texts = [
                    b.get("text", "")
                    for b in raw_content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                tool_output = "\n".join(texts) if texts else str(raw_content)
            else:
                tool_output = raw_content
            events.append(
                PTYEvent(
                    event_type=EventType.TOOL_RESULT,
                    role="tool",
                    tool_output=tool_output,
                    is_error=bool(block.get("is_error")),
                    raw_json=raw_json,
                    timestamp=now,
                    session_id=session_id,
                )
            )
        return events

    def is_response_complete(self, raw: dict) -> bool:
        """Turn-complete sentinel for interactive-mode JSONL.

        CC writes exactly one `system/turn_duration` line per turn, after all
        trailing messages. (`stop_reason == "end_turn"` is NOT reliable: it
        appears on multiple messages of the same turn — e.g. separate thinking
        and text block lines — and would truncate the event stream early.)
        """
        return (
            raw.get("type") == "system"
            and raw.get("subtype") == "turn_duration"
        )

    async def poll_events(
        self, interval: float = 0.3
    ) -> AsyncIterator[PTYEvent]:
        loop = asyncio.get_running_loop()
        while True:
            messages = await loop.run_in_executor(None, self.read_new_messages)
            for msg in messages:
                for event in self.normalize(msg):
                    yield event
            await asyncio.sleep(interval)

    @staticmethod
    def _extract_content(data: dict) -> str | None:
        content = data.get("content")
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            return "\n".join(texts) if texts else None
        if isinstance(content, str):
            return content
        message = data.get("message")
        if isinstance(message, dict):
            return JsonlReader._extract_content(message)
        return None


def _extract_thinking_text(block: dict) -> str:
    for key in ("thinking", "text", "content", "summary"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            texts = [
                b.get("text", "")
                for b in value
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
    if block.get("signature") or block.get("data"):
        return "[encrypted thinking — no plaintext returned by the API]"
    return ""
