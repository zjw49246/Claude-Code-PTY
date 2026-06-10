from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import PTYConfig
from ..session import Session
from ..pool import SessionPool
from ..events import PTYEvent, EventType
from ..bridge import BridgeHub

logger = logging.getLogger(__name__)


class BasePTYBackend:
    """Base adapter for integrating claude-pty into external projects.

    Subclasses implement on_event() and on_exit() to wire events
    into their own DB/broadcast/lifecycle logic.
    """

    def __init__(self, max_sessions: int = 20, bridge: BridgeHub | None = None):
        self._pool = SessionPool(
            config=PTYConfig(max_sessions=max_sessions),
            bridge=bridge,
        )
        self._sessions: dict[Any, Session] = {}
        self._consumers: dict[Any, asyncio.Task] = {}
        self._launch_params: dict[Any, dict] = {}

    def build_config(self, **kwargs) -> PTYConfig:
        return PTYConfig(
            default_model=kwargs.get("model"),
            default_effort=kwargs.get("effort_level"),
            config_dir=kwargs.get("config_dir"),
            disallowed_tools=kwargs.get("disallowed_tools"),
            mcp_config_path=kwargs.get("mcp_config_path"),
            env_overrides=kwargs.get("env_overrides"),
        )

    async def on_event(self, key: Any, event_dict: dict, **context) -> None:
        pass

    async def on_exit(self, key: Any, exit_code: int | None, **context) -> None:
        pass

    async def launch(
        self,
        key: Any,
        prompt: str,
        cwd: str,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        **kwargs,
    ) -> str:
        config = self.build_config(**kwargs)
        sid = resume_session_id or session_id
        session = await self._pool.get_or_create(
            session_id=sid or "",
            cwd=cwd,
            config_override=config,
            channels=self._pool.bridge is not None,
            initial_prompt=prompt if resume_session_id else None,
            resume=bool(resume_session_id),
        )
        self._sessions[key] = session

        # Re-key pool entry if the actual session_id differs from the lookup key
        actual_sid = session.session_id
        if actual_sid and actual_sid != (sid or ""):
            async with self._pool._lock:
                self._pool._sessions.pop(sid or "", None)
                self._pool._sessions[actual_sid] = session
                self._pool._access_order[actual_sid] = self._pool._access_order.pop(sid or "", 0)

        self._launch_params[key] = {"prompt": prompt, "cwd": cwd, **kwargs}

        logger.info("PTY session created: key=%s session_id=%s alive=%s", key, session.session_id, session.is_alive)
        consumer = asyncio.create_task(
            self._consume(key, session, prompt, **kwargs)
        )
        self._consumers[key] = consumer
        return session.session_id

    async def _consume(self, key: Any, session: Session, prompt: str, **kwargs):
        try:
            logger.info("PTY consumer starting for key=%s, prompt=%d chars", key, len(prompt))
            async for event in session.send_prompt(prompt):
                logger.debug("PTY event for key=%s: %s", key, event.event_type)
                await self.on_event(key, event.to_dict(), **kwargs)
            logger.info("PTY consumer finished normally for key=%s", key)
        except Exception:
            logger.exception("Error consuming events for key=%s", key)
        finally:
            exit_code = None
            if session._process:
                exit_code = session._process.exit_code
            if getattr(session, "rate_limited", False) and not exit_code:
                # Surface the limit as a failed run so the host's pool
                # rotation kicks in (it triggers on non-zero exit).
                exit_code = 1
            logger.info("PTY consumer exiting for key=%s, exit_code=%s", key, exit_code)
            await self.on_exit(key, exit_code, **kwargs)

    async def stop(self, key: Any) -> None:
        session = self._sessions.get(key)
        if session:
            sid = session.session_id
            await session.send_interrupt()
            consumer = self._consumers.get(key)
            if consumer and not consumer.done():
                try:
                    await asyncio.wait_for(consumer, timeout=15)
                except asyncio.TimeoutError:
                    consumer.cancel()
            await session.stop()
            self._sessions.pop(key, None)
            self._consumers.pop(key, None)
            if sid:
                # Remove the dead session from the pool as well, otherwise a
                # later launch finds a corpse and takes the cold-resume path.
                await self._pool.remove(sid)

    async def migrate_and_relaunch(
        self, key: Any, new_config_dir: str, resume_session_id: str
    ) -> str:
        session = self._sessions.get(key)
        if session:
            await session.migrate_session(new_config_dir)

        params = self._launch_params.get(key, {})
        params["config_dir"] = new_config_dir
        return await self.launch(
            key=key,
            prompt=params.get("prompt", "continue"),
            cwd=params.get("cwd", "."),
            resume_session_id=resume_session_id,
            **{k: v for k, v in params.items() if k not in ("prompt", "cwd")},
        )

    async def drain_idle_sessions(self) -> int:
        """Reclaim idle PTY sessions (host toggled PTY mode off)."""
        return await self._pool.drain_idle()

    async def shutdown(self) -> None:
        for consumer in self._consumers.values():
            consumer.cancel()
        await self._pool.stop_all()
        self._sessions.clear()
        self._consumers.clear()
