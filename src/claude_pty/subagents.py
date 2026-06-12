"""Native sub-agent tracking from session JSONL + subagents/ directory.

Claude Code leaves a full audit trail of model-spawned sub-agents on disk:

- ``tool_use`` blocks named ``Agent``/``Task`` in the main JSONL (sync agents:
  the matching ``tool_result`` closes them);
- ``tool_use`` blocks named ``Monitor`` (background monitors: the immediate
  tool_result only confirms arming and carries a harness task id; later
  ``<task-notification>`` user messages reference it via ``<task-id>``);
- ``<session-dir>/subagents/agent-<id>.meta.json`` files carrying
  ``agentType``/``description``/``toolUseId`` for Agent-tool spawns, next to
  the per-agent transcript ``agent-<id>.jsonl``.

SubagentTracker turns those into spawn/progress/done records so a host app
(e.g. CCM's sub-agent registry) can show native sub-agents alongside its own.
"""

from __future__ import annotations

import json
import os
import re

# Tool names that spawn a synchronous (turn-blocking) sub-agent.
AGENT_TOOL_NAMES = frozenset({"Agent", "Task"})
# Tool names that arm a background monitor (turn ends; harness wakes the
# session later with <task-notification> messages).
MONITOR_TOOL_NAMES = frozenset({"Monitor"})

_MONITOR_TASK_RE = re.compile(r"\(task (\S+?)[,)]")
_NOTIFICATION_TASK_RE = re.compile(r"<task-id>\s*(\S+?)\s*</task-id>")
_MONITOR_TIMEOUT_MARKER = "Monitor timed out"


class SubagentTracker:
    """Tracks pending native sub-agents for one session JSONL."""

    def __init__(self, jsonl_path: str | None = None):
        self._jsonl_path = jsonl_path
        # tool_use_id -> info dict (type/agent_type/description/background/...)
        self.pending: dict[str, dict] = {}
        # harness task id ("bqirk840r") -> tool_use_id, for Monitor correlation
        self._monitor_tasks: dict[str, str] = {}
        # meta.json files already parsed (by filename)
        self._seen_meta: set[str] = set()
        # tool_use_id -> {"agent_id":..., "agent_type":...} from meta.json
        self._meta_by_tool_use: dict[str, dict] = {}
        # per-agent transcript sizes, for activity detection
        self._transcript_sizes: dict[str, int] = {}

    # ------------------------------------------------------------------ paths

    @property
    def _subagents_dir(self) -> str | None:
        if not self._jsonl_path or not self._jsonl_path.endswith(".jsonl"):
            return None
        return os.path.join(self._jsonl_path[: -len(".jsonl")], "subagents")

    def set_jsonl_path(self, path: str) -> None:
        self._jsonl_path = path

    # ----------------------------------------------------------------- spawns

    def note_tool_use(self, block: dict) -> dict | None:
        """Record a sub-agent-spawning tool_use block.

        Returns the spawn info dict (for a SUBAGENT_SPAWN event), or None if
        the block is not a sub-agent spawn.
        """
        name = block.get("name")
        tool_use_id = block.get("id")
        if not tool_use_id:
            return None
        tool_input = block.get("input") or {}

        if name in AGENT_TOOL_NAMES:
            info = {
                "tool_use_id": tool_use_id,
                "kind": "native-agent",
                "agent_type": tool_input.get("subagent_type") or "general-purpose",
                "description": tool_input.get("description") or "",
                "background": bool(tool_input.get("run_in_background")),
            }
        elif name in MONITOR_TOOL_NAMES:
            info = {
                "tool_use_id": tool_use_id,
                "kind": "native-monitor",
                "agent_type": "monitor",
                "description": tool_input.get("description") or "",
                "background": True,
            }
        else:
            return None

        self.pending[tool_use_id] = info
        return dict(info)

    # ------------------------------------------------------------ completions

    def note_tool_result(self, tool_use_id: str | None, output: str) -> dict | None:
        """Record a tool_result; returns done info when it closes a sub-agent.

        Agent/Task results close the sub-agent. A Monitor result only confirms
        arming — it carries the harness task id used to correlate later
        notifications — so the monitor stays pending.
        """
        if not tool_use_id or tool_use_id not in self.pending:
            return None
        info = self.pending[tool_use_id]

        if info["kind"] == "native-monitor":
            m = _MONITOR_TASK_RE.search(output or "")
            if m:
                info["harness_task_id"] = m.group(1)
                self._monitor_tasks[m.group(1)] = tool_use_id
            return None

        del self.pending[tool_use_id]
        done = dict(info)
        done.update(self._lookup_meta(tool_use_id))
        return done

    def note_user_text(self, text: str) -> dict | None:
        """Inspect autonomous-turn user text for monitor notifications.

        Returns progress/done info for a tracked monitor, or None. The caller
        decides whether to emit it (e.g. only for <task-notification> turns).
        """
        if "<task-notification>" not in (text or ""):
            return None
        m = _NOTIFICATION_TASK_RE.search(text)
        if not m:
            return None
        tool_use_id = self._monitor_tasks.get(m.group(1))
        if not tool_use_id or tool_use_id not in self.pending:
            return None
        info = self.pending[tool_use_id]
        if _MONITOR_TIMEOUT_MARKER in text:
            del self.pending[tool_use_id]
            self._monitor_tasks.pop(m.group(1), None)
            done = dict(info)
            done["timed_out"] = True
            return {"event": "done", **done}
        return {"event": "progress", **dict(info), "summary": text[:2000]}

    # ----------------------------------------------------------------- extras

    def _lookup_meta(self, tool_use_id: str) -> dict:
        """Map a tool_use_id to its agent-<id>.meta.json, if written."""
        if tool_use_id in self._meta_by_tool_use:
            return self._meta_by_tool_use[tool_use_id]
        d = self._subagents_dir
        if not d or not os.path.isdir(d):
            return {}
        try:
            for fn in os.listdir(d):
                if not fn.endswith(".meta.json") or fn in self._seen_meta:
                    continue
                self._seen_meta.add(fn)
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                tid = meta.get("toolUseId")
                if tid:
                    self._meta_by_tool_use[tid] = {
                        "agent_id": fn[len("agent-"):-len(".meta.json")],
                        "agent_type": meta.get("agentType") or "general-purpose",
                    }
        except OSError:
            return {}
        return self._meta_by_tool_use.get(tool_use_id, {})

    @property
    def has_pending(self) -> bool:
        return bool(self.pending)

    def transcripts_grew(self) -> bool:
        """True when any sub-agent transcript grew since the last call.

        Used as an activity signal: the main JSONL is silent while the model
        waits on a sync sub-agent, but the agent's own transcript keeps
        growing — the session must not be considered idle then.
        """
        d = self._subagents_dir
        if not self.pending or not d or not os.path.isdir(d):
            return False
        grew = False
        try:
            for fn in os.listdir(d):
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(d, fn)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size != self._transcript_sizes.get(fn):
                    self._transcript_sizes[fn] = size
                    grew = True
        except OSError:
            return False
        return grew
