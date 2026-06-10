"""CCM (Claude Code Manager) adapter.

CCM-dev side needs only:
    try:
        from claude_pty.adapters.ccm import CCMBackend
        self._pty_backend = CCMBackend(self)
    except ImportError:
        self._pty_backend = None
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime
from typing import Any

from .base import BasePTYBackend
from ..config import PTYConfig

logger = logging.getLogger(__name__)


class _PTYProcessProxy:
    """Mimics asyncio.subprocess.Process for dispatcher compatibility.

    The dispatcher calls process.wait() and reads process.returncode.
    This proxy bridges PTY's consumer task into that interface.
    """

    def __init__(self):
        self._done = asyncio.Event()
        self.returncode: int | None = None
        self.pid = 0

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode or 0

    def complete(self, exit_code: int | None = 0):
        self.returncode = exit_code if exit_code is not None else 0
        self._done.set()

    def kill(self):
        pass

    def send_signal(self, sig):
        pass

    @property
    def stdout(self):
        return None

    @property
    def stderr(self):
        return None


class CCMBackend(BasePTYBackend):
    """Drop-in PTY backend for CCM's InstanceManager."""

    def __init__(self, instance_manager):
        super().__init__(max_sessions=20)
        self._im = instance_manager
        self._proxies: dict[int, _PTYProcessProxy] = {}

    def build_config(self, **kwargs) -> PTYConfig:
        env_overrides = {}
        if kwargs.get("git_env"):
            env_overrides.update(kwargs["git_env"])
        if kwargs.get("thinking_budget") and kwargs["thinking_budget"] > 0:
            env_overrides["MAX_THINKING_TOKENS"] = str(kwargs["thinking_budget"])

        return PTYConfig(
            default_model=kwargs.get("model"),
            default_effort=kwargs.get("effort_level"),
            config_dir=kwargs.get("config_dir"),
            disallowed_tools=kwargs.get("disallowed_tools"),
            mcp_config_path=kwargs.get("mcp_config_path"),
            env_overrides=env_overrides or None,
        )

    async def on_event(self, key: Any, event_dict: dict, **context) -> None:
        instance_id = key
        task_id = context.get("task_id")
        loop_iteration = context.get("loop_iteration")
        try:
            await self._im._process_event(
                instance_id, task_id, event_dict, loop_iteration
            )
        except Exception:
            logger.exception(
                "PTY on_event failed for instance %s task %s", instance_id, task_id
            )

    async def on_exit(self, key: Any, exit_code: int | None, **context) -> None:
        instance_id = key
        proxy = self._proxies.pop(instance_id, None)
        if proxy:
            proxy.complete(exit_code)
        self._sessions.pop(instance_id, None)
        self._consumers.pop(instance_id, None)

    async def launch_for_ccm(
        self,
        instance_id: int,
        prompt: str,
        task_id: int | None = None,
        cwd: str | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        loop_iteration: int | None = None,
        git_env: dict | None = None,
        thinking_budget: int | None = None,
        effort_level: str | None = None,
        chat_initiated: bool = False,
        config_dir: str | None = None,
        enable_workflows: bool = False,
        enabled_skills: dict | None = None,
        mcp_config_path: str | None = None,
    ) -> str:
        disallowed = []
        if not enable_workflows:
            disallowed.append("Workflow")
        if enabled_skills:
            try:
                from backend.services.command_registry import COMMAND_REGISTRY
                for skill, enabled in enabled_skills.items():
                    if enabled and skill in COMMAND_REGISTRY:
                        disallowed.extend(COMMAND_REGISTRY[skill].disallowed_builtins)
            except ImportError:
                pass

        if config_dir:
            self._im._config_dirs[instance_id] = config_dir

        proxy = _PTYProcessProxy()
        self._proxies[instance_id] = proxy
        self._im.processes[instance_id] = proxy

        session_id = await self.launch(
            key=instance_id,
            prompt=prompt,
            cwd=cwd or os.getcwd(),
            resume_session_id=resume_session_id,
            task_id=task_id,
            loop_iteration=loop_iteration,
            model=model,
            git_env=git_env,
            thinking_budget=thinking_budget,
            effort_level=effort_level,
            chat_initiated=chat_initiated,
            config_dir=config_dir,
            disallowed_tools=disallowed if disallowed else None,
            mcp_config_path=mcp_config_path,
        )

        consumer = self._consumers.get(instance_id)
        if consumer:
            self._im._tasks[instance_id] = consumer

        if chat_initiated:
            self._im._launch_params[instance_id] = {
                "prompt": prompt,
                "task_id": task_id,
                "cwd": cwd,
                "model": model,
                "git_env": git_env,
                "thinking_budget": thinking_budget,
                "effort_level": effort_level,
                "enable_workflows": enable_workflows,
                "enabled_skills": enabled_skills,
            }

        return session_id
