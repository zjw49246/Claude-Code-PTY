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

    The dispatcher calls process.wait() and reads process.returncode; on task
    timeout it calls kill() and awaits wait() again. kill()/terminate() must
    therefore actually tear the PTY session down and unblock wait().

    The dispatcher holds a direct reference to the proxy it launched with, so
    when pool rotation retries the same logical turn on a fresh proxy the old
    one must be `chain`ed to the new one — otherwise the dispatcher waits on
    an Event nobody will ever set (until the task-level timeout fires).
    """

    def __init__(self, on_kill=None):
        self._done = asyncio.Event()
        self.returncode: int | None = None
        self.pid = 0
        self._on_kill = on_kill
        self._chained: "_PTYProcessProxy | None" = None
        # The Session this proxy was launched with (bound by launch_for_ccm).
        # Exits and kills use it to tell THIS turn's registrations apart from
        # a newer occupant of the same instance slot.
        self.session = None

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode or 0

    def complete(self, exit_code: int | None = 0):
        if self._done.is_set():
            return
        self.returncode = exit_code if exit_code is not None else 0
        self._done.set()
        chained, self._chained = self._chained, None
        if chained is not None:
            chained.complete(self.returncode)

    def chain(self, proxy: "_PTYProcessProxy") -> None:
        """Forward this proxy's eventual completion to `proxy`.

        Pool rotation relaunches the same logical turn on a fresh proxy while
        the dispatcher still awaits the original one; the retry's outcome
        must propagate there.
        """
        if self._done.is_set():
            proxy.complete(self.returncode)
        else:
            self._chained = proxy

    def kill(self):
        if self._done.is_set():
            return
        if self._on_kill:
            try:
                self._on_kill()
            except Exception:
                logger.exception("PTY proxy on_kill failed")
        self.complete(-9)

    def terminate(self):
        self.kill()

    def send_signal(self, sig):
        if sig == signal.SIGINT:
            # SIGINT means "interrupt the turn" — handled by InstanceManager
            # calling backend.stop(); nothing to do at the proxy level.
            return

    @property
    def stdout(self):
        return None

    @property
    def stderr(self):
        return None


class CCMBackend(BasePTYBackend):
    """Drop-in PTY backend for CCM's InstanceManager."""

    def __init__(self, instance_manager):
        from ..bridge import BridgeHub

        # Own BridgeHub so sessions get channel injection (preferred input
        # path); without it send_prompt would fall back to PTY stdin.
        self._bridge = BridgeHub()
        self._bridge.start()
        super().__init__(max_sessions=20, bridge=self._bridge)
        self._im = instance_manager
        self._proxies: dict[int, _PTYProcessProxy] = {}

    async def _force_kill(self, instance_id: int, expected=None) -> None:
        """Tear down a session after proxy.kill() (dispatcher timeout).

        `expected` is the Session the killed proxy was launched with. By the
        time a task-level timeout fires, the instance slot may already host a
        newer turn (rotation relaunch, slot reuse); tearing the slot down
        blindly would kill that innocent live session. On a mismatch only the
        expected session is stopped and the occupant is left alone.
        """
        if expected is not None:
            current = self._sessions.get(instance_id)
            if current is not None and current is not expected:
                try:
                    await expected.stop()
                except Exception:
                    logger.exception(
                        "Failed to stop PTY session for instance %s", instance_id
                    )
                sid = expected.session_id
                if sid and await self._pool.get(sid) is expected:
                    await self._pool.remove(sid)
                return
        consumer = self._consumers.pop(instance_id, None)
        if consumer and not consumer.done():
            consumer.cancel()
        session = self._sessions.pop(instance_id, None)
        if session:
            sid = session.session_id
            try:
                await session.stop()
            except Exception:
                logger.exception(
                    "Failed to stop PTY session for instance %s", instance_id
                )
            if sid:
                # Dead sessions must not linger in the pool — a later launch
                # would find them, fail aliveness, and cold-resume confusingly.
                await self._pool.remove(sid)

    async def shutdown(self) -> None:
        await super().shutdown()
        self._bridge.stop()

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
        chat_initiated = context.get("chat_initiated", False)
        task_id = context.get("task_id")
        # The session this exiting consumer ran (passed by _consume). The
        # instance-keyed dicts may already describe a NEWER turn — rotation
        # relaunches inline, and a concurrent launch can reuse the slot — so
        # everything below must work on identities, not on dict lookups.
        session = context.get("session") or self._sessions.get(instance_id)
        own_proxy = getattr(session, "_ccm_proxy", None) if session else None
        if own_proxy is None:
            own_proxy = self._proxies.get(instance_id)

        # Chat turn finished: replace the full autonomous callback with a
        # lightweight one that only processes sub-agent completions (task-
        # notifications). This prevents replaying stale prompts while still
        # allowing background Agent completions to mark sub-agents as done.
        if chat_initiated:
            if session:
                async def _subagent_only_callback(event, **ctx):
                    # on_autonomous_event delivers PTYEvent objects (see
                    # Session.on_autonomous_event); convert like base.py's
                    # launch-time callback before the dict-shaped access below.
                    event_dict = event.to_dict()
                    if event_dict.get("subagent") and event_dict.get("event_type", "").startswith("subagent_"):
                        try:
                            await self._im._upsert_native_sub_agent(
                                task_id, event_dict["event_type"], event_dict["subagent"]
                            )
                        except Exception:
                            pass
                session.on_autonomous_event = _subagent_only_callback

                # Start background transcript polling for sub-agent progress
                if hasattr(session, '_reader') and hasattr(session._reader, '_tracker'):
                    tracker = session._reader._tracker
                    if tracker.has_pending:
                        asyncio.create_task(
                            self._poll_subagent_transcripts(tracker, task_id)
                        )

        # For chat-initiated runs, replicate _consume_output() status management
        if chat_initiated and task_id and instance_id not in self._im._stopping:
            from sqlalchemy import update
            from datetime import datetime
            from backend.models.instance import Instance
            from backend.models.task import Task

            ec = exit_code if exit_code is not None else 0

            # Pool rotation for rate-limited chat runs
            if ec not in (0, -2, 130):
                try:
                    rotated = await self._im._try_chat_pool_rotation(
                        instance_id, task_id, ec, ""
                    )
                    if rotated:
                        # A successful rotation relaunched this same logical
                        # turn inline: _proxies/_sessions/_consumers now
                        # describe the RETRY, not this exiting turn, so there
                        # is nothing of ours left to pop. The dispatcher still
                        # awaits own_proxy (a direct reference from launch
                        # time) — forward the retry's eventual outcome to it.
                        # Completing own_proxy here would wake the dispatcher
                        # mid-retry; popping the dicts (pre-fix behaviour)
                        # deregistered the live retry AND completed the wrong
                        # proxy, leaving own_proxy pending until the task
                        # timeout while follow-up messages queued unconsumed.
                        new_proxy = self._proxies.get(instance_id)
                        if own_proxy is not None:
                            if new_proxy is not None and new_proxy is not own_proxy:
                                new_proxy.chain(own_proxy)
                            else:
                                # Rotation reported success but no fresh proxy
                                # is registered — never leave the dispatcher
                                # hanging on an event nobody will set.
                                own_proxy.complete(ec)
                        return
                except Exception:
                    logger.exception("Pool rotation check failed for instance %s", instance_id)

            interrupted = ec in (-2, 130)
            new_status = "idle" if (ec == 0 or interrupted) else "error"

            async with self._im.db_factory() as db:
                await db.execute(
                    update(Instance).where(Instance.id == instance_id).values(
                        status=new_status, pid=None, current_task_id=None,
                    )
                )
                chat_active_statuses = ["executing", "in_progress", "failed", "pending"]
                if ec == 0 or interrupted:
                    result = await db.execute(
                        update(Task)
                        .where(Task.id == task_id, Task.status.in_(chat_active_statuses))
                        .values(status="completed", completed_at=datetime.utcnow(), error_message=None)
                    )
                    if result.rowcount:
                        await self._im.broadcaster.broadcast("tasks", {
                                "event": "status_change",
                                "task_id": task_id,
                                "new_status": "completed",
                                "instance_id": instance_id,
                            })
                else:
                    result = await db.execute(
                        update(Task)
                        .where(Task.id == task_id, Task.status.in_(chat_active_statuses))
                        .values(status="failed", error_message=f"Process exited with code {ec}")
                    )
                    if result.rowcount:
                        await self._im.broadcaster.broadcast("tasks", {
                            "event": "status_change",
                            "task_id": task_id,
                            "new_status": "failed",
                            "instance_id": instance_id,
                        })
                await db.commit()

            # Empty-reply retry: if chat turn produced only "No response requested."
            if ec == 0 and instance_id in self._im._launch_params:
                params = self._im._launch_params[instance_id]
                if not params.get("_retried"):
                    try:
                        assistant_texts = await self._get_recent_assistant_texts(task_id)
                        combined = " ".join(assistant_texts).strip().lower().rstrip(".")
                        _NO_RESPONSE = {"no response requested", "no response needed"}
                        if not assistant_texts or combined in _NO_RESPONSE:
                            params["_retried"] = True
                            logger.warning(
                                "Task %d got empty/non-response (%r), re-enqueueing",
                                task_id, combined[:80],
                            )
                            from backend.main import dispatcher
                            from backend.services.dispatcher import PRIORITY_USER
                            await dispatcher.enqueue_message(
                                task_id=task_id,
                                prompt=params["prompt"],
                                priority=PRIORITY_USER,
                                source="retry",
                            )
                    except Exception:
                        logger.exception("Empty-reply retry check failed for task %s", task_id)

            # Broadcast process exit
            await self._im.broadcaster.broadcast(f"task:{task_id}", {
                "event_type": "process_exit",
                "exit_code": ec,
                "stderr": None,
            })

        # Identity-guarded cleanup: pop only entries that still belong to THIS
        # turn. A newer launch may have re-registered the slot while we were
        # exiting (rotation relaunch, launch_for_ccm stale-session takeover);
        # popping blindly would deregister its live session, and completing
        # the dict's proxy would wake the wrong dispatcher while ours hangs.
        if self._proxies.get(instance_id) is own_proxy:
            self._proxies.pop(instance_id, None)
        if own_proxy is not None:
            own_proxy.complete(exit_code)
        if self._sessions.get(instance_id) is session:
            self._sessions.pop(instance_id, None)
        cur_consumer = self._consumers.get(instance_id)
        if cur_consumer is None or cur_consumer is asyncio.current_task():
            self._consumers.pop(instance_id, None)
        if self._im.processes.get(instance_id) is own_proxy:
            self._im.processes.pop(instance_id, None)
        cur_task = self._im._tasks.get(instance_id)
        if cur_task is None or cur_task is asyncio.current_task():
            self._im._tasks.pop(instance_id, None)

    async def _get_recent_assistant_texts(self, task_id: int) -> list[str]:
        """Get assistant message texts from the most recent turn (after last user_message)."""
        from sqlalchemy import select
        from backend.models.log_entry import LogEntry
        async with self._im.db_factory() as db:
            result = await db.execute(
                select(LogEntry.event_type, LogEntry.role, LogEntry.content)
                .where(LogEntry.task_id == task_id)
                .order_by(LogEntry.id.desc())
                .limit(20)
            )
            rows = list(result.all())
        texts = []
        for event_type, role, content in rows:
            if event_type == "user_message":
                break
            if event_type in ("message", "result") and role == "assistant" and content:
                texts.append(content)
        return texts

    async def _poll_subagent_transcripts(self, tracker, task_id: int):
        """Poll sub-agent transcripts every 5s and emit progress events."""
        import asyncio
        try:
            while tracker.has_pending:
                updates = tracker.read_transcript_updates()
                for u in updates:
                    info = {
                        "tool_use_id": u["tool_use_id"],
                        "summary": u.get("summary", ""),
                    }
                    try:
                        await self._im._upsert_native_sub_agent(
                            task_id, "subagent_progress", info
                        )
                    except Exception:
                        pass
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("subagent transcript poll stopped for task %s", task_id)

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
        disallowed_tools: list[str] | None = None,
    ) -> str:
        disallowed = list(disallowed_tools or ())
        if not enable_workflows:
            disallowed.append("Workflow")
        if enabled_skills:
            try:
                from backend.services.skill_loader import discover_skills, get_skill_disallowed_tools
                skills = discover_skills(project_dir=cwd)
                disallowed.extend(get_skill_disallowed_tools(skills, enabled_skills))
            except ImportError:
                try:
                    from backend.services.command_registry import COMMAND_REGISTRY
                    for skill, enabled in enabled_skills.items():
                        if enabled and skill in COMMAND_REGISTRY:
                            disallowed.extend(COMMAND_REGISTRY[skill].disallowed_builtins)
                except ImportError:
                    pass

        logger.info("launch_for_ccm: instance=%s mcp_config=%s skills=%s resume=%s",
                    instance_id, mcp_config_path, enabled_skills, resume_session_id)

        if config_dir:
            self._im._config_dirs[instance_id] = config_dir

        # Stop a stale session before resume — but never a live session that
        # IS the resume target (the pool will hot-reuse it; killing it here
        # would orphan the in-flight turn and force a cold resume).
        old_session = self._sessions.get(instance_id)
        if old_session and not (
            old_session.is_alive
            and resume_session_id
            and old_session.session_id == resume_session_id
        ):
            logger.info("Stopping stale PTY session for instance %s before launch", instance_id)
            old_sid = old_session.session_id
            try:
                await old_session.stop()
            except Exception:
                logger.exception("Failed to stop old session for instance %s", instance_id)
            self._sessions.pop(instance_id, None)
            if old_sid:
                await self._pool.remove(old_sid)
            old_consumer = self._consumers.pop(instance_id, None)
            if old_consumer and not old_consumer.done():
                old_consumer.cancel()

        proxy = _PTYProcessProxy(
            # `proxy.session` is late-bound below, so a timeout kill tears
            # down the session this proxy actually launched — not whatever
            # occupies the instance slot by the time the timeout fires.
            on_kill=lambda: asyncio.get_event_loop().create_task(
                self._force_kill(instance_id, expected=proxy.session)
            )
        )
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
            disallowed_tools=(
                list(dict.fromkeys(disallowed)) if disallowed else None
            ),
            mcp_config_path=mcp_config_path,
        )

        # Bind proxy↔session both ways so exits/kills can tell this turn's
        # registrations apart from a newer occupant of the same slot, and
        # surface the real PTY pid on the proxy (Instance.pid bookkeeping).
        sess = self._sessions.get(instance_id)
        if sess is not None:
            proxy.session = sess
            sess._ccm_proxy = proxy
            if sess._process is not None:
                proxy.pid = sess._process.pid or 0

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
