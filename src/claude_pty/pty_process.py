from __future__ import annotations

import fcntl
import json
import os
import pty
import random
import select
import shutil
import struct
import subprocess
import termios
import threading
import time
import uuid
from typing import Callable

from .config import PTYConfig
from ._env import build_clean_env
from .exceptions import PTYSpawnError, PTYDeadError


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
        self.cwd = cwd
        self.session_id = session_id or str(uuid.uuid4())
        self.config = config or PTYConfig()
        self._on_death = on_death
        self._channel_inject_port = channel_inject_port
        self._bridge_port = bridge_port

        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None

        self._drain_thread: threading.Thread | None = None
        self._running = False
        self._child_dead = threading.Event()
        self._spawn_time: float | None = None
        self._mcp_config_path: str | None = None

    @property
    def jsonl_path(self) -> str:
        project_hash = self.cwd.replace("/", "-")
        config_base = self.config.config_dir or os.path.expanduser("~/.claude")
        return os.path.join(
            config_base, "projects", project_hash, f"{self.session_id}.jsonl"
        )

    @property
    def channels_enabled(self) -> bool:
        return self._channel_inject_port is not None

    def spawn(self, resume_session_id: str | None = None) -> None:
        if resume_session_id:
            self.session_id = resume_session_id

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
        return cmd

    def _drain_loop(self) -> None:
        while self._running:
            try:
                r, _, _ = select.select(
                    [self.master_fd], [], [], self.config.drain_interval
                )
                if r:
                    data = os.read(self.master_fd, self.config.drain_read_size)
                    if not data:
                        self._child_dead.set()
                        break
            except OSError:
                self._child_dead.set()
                break

        if self._on_death and self._child_dead.is_set():
            try:
                self._on_death(self)
            except Exception:
                pass

    def send_prompt(self, text: str) -> None:
        if not self.is_alive:
            raise PTYDeadError(f"Process {self.session_id} is not alive")

        for ch in text:
            os.write(self.master_fd, ch.encode("utf-8"))
            delay = random.gauss(
                self.config.char_send_delay_mean,
                self.config.char_send_delay_stddev,
            )
            time.sleep(
                max(
                    self.config.char_send_delay_min,
                    min(self.config.char_send_delay_max, delay),
                )
            )
        time.sleep(0.1)
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
