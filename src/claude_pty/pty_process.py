from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import threading
import time
import uuid
from typing import Callable

import logging

from .config import PTYConfig
from ._env import build_clean_env
from .exceptions import PTYSpawnError, PTYDeadError

logger = logging.getLogger(__name__)

# ANSI escape sequences (CSI, OSC, charset selection)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")

# Generic marker for CC startup confirmation dialogs (trust dialog, dev-channels
# warning, theme picker...). CC renders the TUI with cursor positioning, so
# visible spaces are not literal spaces — match on whitespace-collapsed text.
_CONFIRM_MARKER = "Entertoconfirm"
_CONFIRM_WINDOW = 20.0  # only auto-confirm during startup
_CONFIRM_COOLDOWN = 0.6  # absorb TUI redraw after answering

# Rate-limit detection in PTY output (whitespace-collapsed, lowercased).
# The TUI always renders the limit banner even though interactive JSONL may
# not record a structured event — this is the reliable signal for pool
# rotation in PTY mode.
_RATE_LIMIT_MARKERS = (
    "hityoursessionlimit",
    "hityourweeklylimit",
    "hityourlimit",
    "usagelimitreached",
    "sessionlimitreached",
)


def _match_rate_limit(collapsed_lower: str) -> bool:
    return any(m in collapsed_lower for m in _RATE_LIMIT_MARKERS)


def _collapse_for_prompt_match(data: bytes) -> str:
    """Strip ANSI escapes and collapse all whitespace for marker matching."""
    text = _ANSI_RE.sub("", data.decode("utf-8", errors="replace"))
    return re.sub(r"\s+", "", text)


class PTYProcess:
    """Low-level PTY wrapper around a single Claude Code interactive process."""

    def __init__(
        self,
        cwd: str,
        session_id: str | None = None,
        config: PTYConfig | None = None,
        on_death: Callable[[PTYProcess], None] | None = None,
        channel_inject_port: int | None = None,
        bridge_port: int | None = None,
    ):
        # 归一化为绝对路径：jsonl_path 按 cwd 字面推导，相对路径（如 "."）
        # 会推出错误的 projects 目录，轮询永远读不到事件（CCM task 55 实录）
        self.cwd = os.path.abspath(os.path.expanduser(cwd or "."))
        self.session_id = session_id or str(uuid.uuid4())
        self.config = config or PTYConfig()
        self._on_death = on_death
        self._channel_inject_port = channel_inject_port
        self._bridge_port = bridge_port

        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None

        self._drain_thread: threading.Thread | None = None
        self._last_output: float = 0.0  # monotonic ts of last PTY output
        self.rate_limited: bool = False  # set by drain loop on limit banner
        self._running = False
        self._child_dead = threading.Event()
        self._spawn_time: float | None = None
        self._mcp_config_path: str | None = None

    @property
    def jsonl_path(self) -> str:
        # CC's actual rule (verified): every non-alphanumeric char becomes '-'
        project_hash = re.sub(r"[^A-Za-z0-9]", "-", self.cwd)
        config_base = self.config.config_dir or os.path.expanduser("~/.claude")
        return os.path.join(
            config_base, "projects", project_hash, f"{self.session_id}.jsonl"
        )

    @property
    def channels_enabled(self) -> bool:
        return self._channel_inject_port is not None

    def clear_rate_limited(self) -> None:
        """Reset the banner flag after the host judged it a false positive
        (marker text rendered from conversation content, not a real banner).
        The drain loop resumes scanning with a fresh buffer."""
        self.rate_limited = False

    def spawn(self, resume_session_id: str | None = None) -> None:
        if resume_session_id:
            self.session_id = resume_session_id

        self.rate_limited = False
        self._pretrust_workdir()
        if self._channel_inject_port:
            self._setup_mcp_config()

        master, slave = pty.openpty()
        winsize = struct.pack(
            "HHHH", self.config.terminal_rows, self.config.terminal_cols, 0, 0
        )
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)

        env = build_clean_env(self.config)
        cmd = self._build_command(resume_session_id)

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
                cwd=self.cwd,
                env=env,
            )
        except Exception as e:
            os.close(master)
            os.close(slave)
            raise PTYSpawnError(f"Failed to spawn Claude Code: {e}") from e

        os.close(slave)
        self.master_fd = master
        self.pid = self.proc.pid
        self._spawn_time = time.monotonic()

        self._running = True
        self._child_dead.clear()
        self._drain_thread = threading.Thread(
            target=self._drain_loop,
            name=f"pty-drain-{self.session_id[:8]}",
            daemon=True,
        )
        self._drain_thread.start()

    def _pretrust_workdir(self, claude_json_path: str | None = None) -> None:
        """Pre-accept the trust dialog (and pre-approve our MCP server) by
        writing the project entry into .claude.json before spawn.

        This is the primary mechanism for suppressing startup dialogs; the
        drain-loop auto-confirm is only a fallback. Atomic write (tmp+rename),
        existing entry fields are preserved. Best-effort: failure is logged,
        the auto-confirm fallback still covers us.
        """
        if claude_json_path is None:
            if self.config.config_dir:
                claude_json_path = os.path.join(
                    self.config.config_dir, ".claude.json"
                )
            else:
                claude_json_path = os.path.expanduser("~/.claude.json")

        try:
            cfg: dict = {}
            if os.path.exists(claude_json_path):
                try:
                    with open(claude_json_path, encoding="utf-8") as f:
                        cfg = json.load(f)
                except (json.JSONDecodeError, OSError):
                    logger.warning(
                        "pretrust[%s]: could not parse %s, leaving it alone",
                        self.session_id[:8], claude_json_path,
                    )
                    return

            # First interactive run of a config_dir shows the global
            # onboarding (theme picker) — `-p` mode never does, so dirs
            # provisioned for headless use hit it on their first PTY spawn
            # and the session wedges. Mark onboarding complete up front.
            cfg.setdefault("hasCompletedOnboarding", True)
            cfg.setdefault("theme", "dark")

            projects = cfg.setdefault("projects", {})
            entry = projects.setdefault(self.cwd, {})
            entry["hasTrustDialogAccepted"] = True
            entry["hasClaudeMdExternalIncludesApproved"] = True
            entry["hasClaudeMdExternalIncludesWarningShown"] = True
            entry.setdefault("projectOnboardingSeenCount", 1)
            if self._channel_inject_port:
                enabled = entry.setdefault("enabledMcpjsonServers", [])
                if "pty-bridge" not in enabled:
                    enabled.append("pty-bridge")

            os.makedirs(os.path.dirname(claude_json_path) or ".", exist_ok=True)
            tmp = claude_json_path + ".pty-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.rename(tmp, claude_json_path)
        except OSError:
            logger.exception(
                "pretrust[%s]: failed to write %s",
                self.session_id[:8], claude_json_path,
            )

    def _setup_mcp_config(self) -> None:
        """Write .mcp.json with the channel server config."""
        channel_cmd = shutil.which("claude-pty-channel")
        if not channel_cmd:
            channel_cmd = "claude-pty-channel"

        config = {
            "mcpServers": {
                "pty-bridge": {
                    "command": channel_cmd,
                    "args": [
                        "--port",
                        str(self._channel_inject_port),
                        "--session-id",
                        self.session_id,
                    ],
                }
            }
        }
        if self._bridge_port:
            config["mcpServers"]["pty-bridge"]["args"].extend(
                ["--bridge-port", str(self._bridge_port)]
            )

        mcp_path = os.path.join(self.cwd, ".mcp.json")
        self._mcp_config_path = mcp_path

        existing = {}
        if os.path.exists(mcp_path):
            try:
                with open(mcp_path) as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        servers = existing.get("mcpServers", {})
        servers["pty-bridge"] = config["mcpServers"]["pty-bridge"]
        existing["mcpServers"] = servers

        with open(mcp_path, "w") as f:
            json.dump(existing, f, indent=2)

    def _build_command(self, resume_session_id: str | None) -> list[str]:
        cmd = [self.config.claude_binary]
        if self._channel_inject_port:
            cmd.extend([
                "--dangerously-load-development-channels", "server:pty-bridge"
            ])
        if self.config.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        else:
            cmd.extend(["--session-id", self.session_id])
        if self.config.default_model:
            cmd.extend(["--model", self.config.default_model])
        if self.config.default_effort:
            cmd.extend(["--effort", self.config.default_effort])
        if self.config.disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(self.config.disallowed_tools)])
        if self.config.mcp_config_path:
            cmd.extend(["--mcp-config", self.config.mcp_config_path])
        return cmd

    def _drain_loop(self) -> None:
        # Teleos-style generic startup auto-confirm: any dialog ending with
        # "Enter to confirm" (trust dialog, dev-channels warning, theme picker)
        # gets a \r. Whitespace-collapsed matching because CC positions text
        # with cursor moves. After answering, cool down briefly, then re-check
        # the buffer — a second consecutive dialog finishes rendering while CC
        # is idle, so no further onData would drive the check.
        confirm_buf = ""
        confirm_deadline = time.monotonic() + _CONFIRM_WINDOW
        cooldown_until = 0.0
        rl_buf = ""  # always-on rolling buffer for rate-limit banner

        while self._running:
            try:
                r, _, _ = select.select(
                    [self.master_fd], [], [], self.config.drain_interval
                )
                now = time.monotonic()
                if r:
                    data = os.read(self.master_fd, self.config.drain_read_size)
                    if not data:
                        self._child_dead.set()
                        break
                    logger.debug("drain[%s]: %d bytes", self.session_id[:8], len(data))
                    self._last_output = now
                    collapsed = _collapse_for_prompt_match(data)
                    if now < confirm_deadline:
                        confirm_buf = (confirm_buf + collapsed)[-2000:]
                    if not self.rate_limited:
                        rl_buf = (rl_buf + collapsed.lower())[-3000:]
                        if _match_rate_limit(rl_buf):
                            logger.warning(
                                "drain[%s]: rate-limit banner detected in PTY output",
                                self.session_id[:8],
                            )
                            self.rate_limited = True
                            rl_buf = ""  # fresh scan if the host clears the flag
                if (
                    now < confirm_deadline
                    and now >= cooldown_until
                    and _CONFIRM_MARKER in confirm_buf
                ):
                    logger.info(
                        "drain[%s]: startup dialog detected, sending \\r",
                        self.session_id[:8],
                    )
                    os.write(self.master_fd, b"\r")
                    confirm_buf = ""
                    cooldown_until = now + _CONFIRM_COOLDOWN
            except OSError:
                self._child_dead.set()
                break

        if self._on_death and self._child_dead.is_set():
            try:
                self._on_death(self)
            except Exception:
                pass

    def send_prompt(self, text: str) -> None:
        """Write a prompt to CC's stdin and submit it.

        The text is wrapped in bracketed-paste markers and written in one
        chunk: no UTF-8 multibyte splitting, embedded newlines don't submit
        early, and arbitrarily long prompts arrive instantly. (The old
        char-by-char human-typing simulation was both slow and unsafe.)
        """
        if not self.is_alive:
            raise PTYDeadError(f"Process {self.session_id} is not alive")

        logger.info(
            "send_prompt[%s]: %d chars, master_fd=%s",
            self.session_id[:8], len(text), self.master_fd,
        )
        payload = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"
        os.write(self.master_fd, payload)
        time.sleep(0.15)  # let Ink process the paste before submitting
        os.write(self.master_fd, b"\r")

    def send_interrupt(self) -> None:
        if self.master_fd is not None:
            os.write(self.master_fd, b"\x1b")

    @property
    def is_alive(self) -> bool:
        if self._child_dead.is_set():
            return False
        if self.proc:
            return self.proc.poll() is None
        return False

    @property
    def exit_code(self) -> int | None:
        if self.proc:
            return self.proc.poll()
        return None

    @property
    def uptime(self) -> float:
        if self._spawn_time is None:
            return 0.0
        return time.monotonic() - self._spawn_time

    def stop(self, timeout: float = 5.0) -> int | None:
        self._running = False

        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            else:
                self.proc.kill()
                self.proc.wait()

        if self._drain_thread and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=2)

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

        return self.proc.returncode if self.proc else None
