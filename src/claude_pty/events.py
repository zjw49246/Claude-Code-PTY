from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class EventType(str, Enum):
    SYSTEM_INIT = "system_init"
    SYSTEM_EVENT = "system_event"
    MESSAGE = "message"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    PROCESS_EXIT = "process_exit"
    PARSE_ERROR = "parse_error"
    SESSION_STARTED = "session_started"
    SESSION_CRASHED = "session_crashed"
    SESSION_RESUMED = "session_resumed"


@dataclass
class PTYEvent:
    """Event structure compatible with CCM's StreamParser output."""

    event_type: str
    role: str | None = None
    content: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    raw_json: str | None = None
    is_error: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    session_id: str | None = None
    cost_usd: float | None = None
    context_usage: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "event_type": self.event_type,
            "role": self.role,
            "content": self.content,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "tool_output": self.tool_output,
            "raw_json": self.raw_json,
            "is_error": self.is_error,
            "timestamp": self.timestamp,
        }
        if self.session_id is not None:
            d["session_id"] = self.session_id
        if self.cost_usd is not None:
            d["cost_usd"] = self.cost_usd
        if self.context_usage is not None:
            d["context_usage"] = self.context_usage
        return d
