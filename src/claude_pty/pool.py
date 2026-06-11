from __future__ import annotations

import asyncio
import logging
import socket
import time

from .config import PTYConfig
from .session import Session
from .bridge import BridgeHub
from .exceptions import PoolExhaustedError

logger = logging.getLogger(__name__)


class SessionPool:
    """Manages multiple concurrent Sessions with LRU eviction."""

    def __init__(
        self,
        config: PTYConfig | None = None,
        bridge: BridgeHub | None = None,
    ):
        self.config = config or PTYConfig()
        self.bridge = bridge
        self._sessions: dict[str, Session] = {}
        self._access_order: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _allocate_inject_port() -> int:
        """Pick a free port for a session's channel server.

        A fixed base counter (the old `19100 + n` scheme) collides across
        host processes on the same machine: two pools both hand out 19100,
        and injection cross-talks into a foreign session. Let the OS pick a
        free ephemeral port instead. The remaining close-to-bind race is
        covered by the channel server's session_id check on /inject.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def get_or_create(
        self,
        session_id: str,
        cwd: str,
        config_override: PTYConfig | None = None,
        channels: bool = False,
        initial_prompt: str | None = None,
        resume: bool = False,
    ) -> Session:
        async with self._lock:
            self._access_order[session_id] = time.monotonic()

            if session_id in self._sessions:
                session = self._sessions[session_id]
                same_config = (
                    config_override is None
                    or session.config.config_dir == config_override.config_dir
                )
                if session.is_alive and same_config:
                    return session
                if session.is_alive:
                    # config_dir changed (account rotation): the old-account
                    # session must not be reused — stop it and respawn below.
                    logger.info(
                        "Session %s config_dir changed, recreating", session_id
                    )
                    await session.stop()
                del self._sessions[session_id]

            while len(self._sessions) >= self.config.max_sessions:
                evicted = await self._evict_one()
                if not evicted:
                    raise PoolExhaustedError(
                        f"Cannot create session: all {self.config.max_sessions} "
                        "sessions are active (none idle for eviction)"
                    )

            config = config_override or self.config
            inject_port = None
            bridge = None
            if channels and self.bridge:
                inject_port = self._allocate_inject_port()
                bridge = self.bridge
            session = Session(
                cwd=cwd,
                session_id=session_id or None,
                config=config,
                bridge=bridge,
                channel_inject_port=inject_port,
                resume_existing=resume,
            )
            await session.start(initial_prompt=initial_prompt)
            self._sessions[session_id] = session
            return session

    async def _evict_one(self) -> bool:
        # Prefer idle sessions past timeout
        candidates = []
        for sid, session in self._sessions.items():
            if session.idle_seconds >= self.config.idle_timeout:
                candidates.append((self._access_order.get(sid, 0), sid))

        if not candidates:
            # Force-evict oldest that isn't mid-prompt
            for sid, session in self._sessions.items():
                if not session._send_lock.locked():
                    candidates.append((self._access_order.get(sid, 0), sid))

        if not candidates:
            return False

        candidates.sort()
        _, evict_id = candidates[0]
        session = self._sessions.pop(evict_id)
        self._access_order.pop(evict_id, None)
        await session.stop()
        logger.info(
            "Evicted session %s (idle %.1fs)", evict_id, session.idle_seconds
        )
        return True

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            self._access_order.pop(session_id, None)
            if session:
                await session.stop()

    async def drain_idle(self) -> int:
        """Stop and remove all sessions not currently mid-prompt.

        Used when the host app switches PTY mode off: idle sessions are
        reclaimed immediately, in-flight ones finish their turn and are
        left to normal lifecycle.
        """
        async with self._lock:
            idle_ids = [
                sid for sid, session in self._sessions.items()
                if not session._send_lock.locked()
            ]
            stopped = 0
            for sid in idle_ids:
                session = self._sessions.pop(sid)
                self._access_order.pop(sid, None)
                try:
                    await session.stop()
                except Exception:
                    logger.exception("Failed to stop idle session %s", sid)
                stopped += 1
            if stopped:
                logger.info("Drained %d idle session(s)", stopped)
            return stopped

    async def stop_all(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.stop()
            self._sessions.clear()
            self._access_order.clear()

    def stats(self) -> dict:
        now = time.monotonic()
        sessions = []
        for sid, session in self._sessions.items():
            sessions.append(
                {
                    "session_id": sid,
                    "alive": session.is_alive,
                    "idle_seconds": round(session.idle_seconds, 1),
                    "last_access": round(
                        now - self._access_order.get(sid, now), 1
                    ),
                }
            )
        return {
            "total": len(self._sessions),
            "max": self.config.max_sessions,
            "alive": sum(1 for s in sessions if s["alive"]),
            "sessions": sessions,
        }
