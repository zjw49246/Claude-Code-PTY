from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import AsyncIterator

from .events import PTYEvent, EventType
from .subagents import SubagentTracker


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

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_IMAGE_MARKER_RE = re.compile(r"\[Image #\d+\]")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _local_image_path(value: str) -> str | None:
    candidate = value.strip().strip("`\"'")
    if not candidate:
        return None
    if os.path.splitext(candidate)[1].lower() not in _IMAGE_EXTENSIONS:
        return None
    if not (
        os.path.isabs(candidate)
        or candidate.startswith(("./", "../", "~/", "\\\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(candidate)
    ):
        return None
    return candidate


def _multimodal_prompt_shape(prompt: str) -> tuple[str | None, tuple[str, ...]]:
    """Return the text/path evidence Claude records for image attachments."""

    normalized_lines: list[str] = []
    image_paths: list[str] = []
    for line in prompt.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        line_ending = line[len(body):]
        match = re.fullmatch(
            r"(?P<prefix>\s*(?:[-*+]\s+)?)(?P<path>.*?)(?P<trailing>\s*)",
            body,
        )
        image_path = (
            _local_image_path(match.group("path"))
            if match is not None
            else None
        )
        if image_path is not None:
            normalized_lines.append(match.group("prefix").rstrip() + line_ending)
            image_paths.append(image_path)
        else:
            normalized_lines.append(line)
    if not image_paths:
        return None, ()
    return "".join(normalized_lines).strip(), tuple(image_paths)


def _user_texts(raw: dict) -> tuple[str, ...]:
    if raw.get("type") != "user":
        return ()
    message = raw.get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return (content,)
    if not isinstance(content, list):
        return ()
    return tuple(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _image_source_path(raw: dict) -> str | None:
    texts = _user_texts(raw)
    if len(texts) != 1:
        return None
    text = texts[0].strip()
    prefix = "[Image: source: "
    if not text.startswith(prefix) or not text.endswith("]"):
        return None
    return text[len(prefix):-1]


class PromptEchoMatcher:
    """Correlate one delivered prompt with its native JSONL user records."""

    def __init__(self, prompt: str):
        self.prompt = prompt.strip()
        self._image_text, self._image_paths = _multimodal_prompt_shape(
            self.prompt
        )
        self._pending_image_sources: tuple[str, ...] = ()

    def observe(self, raw: dict) -> bool:
        texts = _user_texts(raw)
        if self.prompt and any(self.prompt in text for text in texts):
            self._pending_image_sources = ()
            return True

        if self._pending_image_sources:
            if raw.get("type") in _SKIP_TYPES:
                return False
            source_path = _image_source_path(raw)
            if source_path == self._pending_image_sources[0]:
                self._pending_image_sources = self._pending_image_sources[1:]
                return not self._pending_image_sources
            self._pending_image_sources = ()

        if self._image_text is None or not isinstance(
            raw.get("message"), dict
        ):
            return False
        content = raw["message"].get("content")
        if not isinstance(content, list):
            return False
        image_count = sum(
            1
            for block in content
            if isinstance(block, dict) and block.get("type") == "image"
        )
        if image_count != len(self._image_paths):
            return False
        if not any(
            self._image_text in _IMAGE_MARKER_RE.sub("", text)
            for text in texts
        ):
            return False
        self._pending_image_sources = self._image_paths
        return False


class JsonlReader:
    """Reads Claude Code session JSONL files and normalizes to PTYEvent.

    Handles partial-write safety via line buffering. Normalizes interactive-mode
    JSONL into events structurally identical to CCM's StreamParser.parse_line().
    """

    def __init__(self, path: str, tracker: SubagentTracker | None = None):
        self.path = path
        self._offset: int = 0
        self._buffer: str = ""
        # Optional native sub-agent tracker: when set, normalize() also emits
        # SUBAGENT_SPAWN/PROGRESS/DONE events for Agent/Task/Monitor tools.
        self.tracker = tracker

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

    def normalize(
        self, raw: dict, include_user_text: bool = False
    ) -> list[PTYEvent]:
        """Normalize a single interactive-mode JSONL message into PTYEvent(s).

        The output matches CCM's StreamParser.parse_line() event structure.

        include_user_text: also emit user text blocks as MESSAGE events. Off
        for normal turns (the host already knows the prompt it sent); on for
        autonomous turns, where the "user" text is a harness-injected
        <task-notification> the host has never seen.
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
            return self._normalize_user(
                message, raw_json, now, session_id, include_user_text
            )

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
                if self.tracker is not None:
                    spawn = self.tracker.note_tool_use(block)
                    if spawn:
                        events.append(
                            PTYEvent(
                                event_type=EventType.SUBAGENT_SPAWN,
                                role="assistant",
                                content=spawn.get("description"),
                                subagent=spawn,
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
        self,
        message: dict,
        raw_json: str,
        now: str,
        session_id: str | None,
        include_user_text: bool = False,
    ) -> list[PTYEvent]:
        msg_content = message.get("content", [])
        if isinstance(msg_content, str):
            # Plain-string user message (stdin-delivered prompts, harness
            # notifications). Only surfaced for autonomous turns.
            if include_user_text and msg_content.strip():
                return self._user_text_events(
                    msg_content, raw_json, now, session_id
                )
            return []
        if not isinstance(msg_content, list):
            return []

        events: list[PTYEvent] = []
        for block in msg_content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                if include_user_text and text.strip():
                    events.extend(
                        self._user_text_events(text, raw_json, now, session_id)
                    )
                continue
            if block.get("type") != "tool_result":
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
            if self.tracker is not None:
                done = self.tracker.note_tool_result(
                    block.get("tool_use_id"), tool_output or ""
                )
                if done:
                    events.append(
                        PTYEvent(
                            event_type=EventType.SUBAGENT_DONE,
                            role="assistant",
                            content=done.get("description"),
                            subagent=done,
                            timestamp=now,
                            session_id=session_id,
                        )
                    )
        return events

    def _user_text_events(
        self, text: str, raw_json: str, now: str, session_id: str | None
    ) -> list[PTYEvent]:
        events = [
            PTYEvent(
                event_type=EventType.MESSAGE,
                role="user",
                content=text,
                raw_json=raw_json,
                timestamp=now,
                session_id=session_id,
            )
        ]
        if self.tracker is not None:
            update = self.tracker.note_user_text(text)
            if update:
                kind = update.pop("event", "progress")
                events.append(
                    PTYEvent(
                        event_type=(
                            EventType.SUBAGENT_DONE
                            if kind == "done"
                            else EventType.SUBAGENT_PROGRESS
                        ),
                        role="assistant",
                        content=update.get("summary") or update.get("description"),
                        subagent=update,
                        timestamp=now,
                        session_id=session_id,
                    )
                )
        return events

    def is_prompt_echo(self, raw: dict, prompt: str) -> bool:
        """True when this JSONL line is the user-message echo of `prompt`.

        CC records every delivered prompt as a user message (channel-injected
        prompts arrive wrapped in a <channel ...> tag, stdin prompts verbatim),
        so substring containment over the user text identifies the start of
        OUR turn. Needed because turn_duration sentinels from earlier turns
        may still sit unread in the file — counting one of those would end
        the new turn with the previous turn's output (the task-87 off-by-one).
        """
        needle = prompt.strip()
        if not needle:
            return False
        return any(needle in text for text in _user_texts(raw))

    def prompt_echo_matcher(self, prompt: str) -> PromptEchoMatcher:
        return PromptEchoMatcher(prompt)

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
