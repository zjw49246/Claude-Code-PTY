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


async def _stop_unpublished_session(
    session: Session,
    start_task: asyncio.Task,
) -> None:
    """Let a cancellation-unsafe start settle, then stop its process.

    ``Session.start`` spawns through ``run_in_executor``. Cancelling the
    coroutine does not stop that executor thread, so calling ``session.stop``
    immediately can race with a process that has not finished spawning yet.
    Keep start alive in its own task, wait for it to settle, and only then
    tear down the unpublished Session.
    """

    try:
        await start_task
    except BaseException:
        # The original start error is re-raised by get_or_create. This await is
        # only here to settle the transaction and retrieve its exception.
        pass
    try:
        await session.stop()
    except BaseException:
        # Cleanup failure must not hide the original start/cancellation cause.
        logger.exception("Failed to stop unpublished PTY session")


async def _await_cleanup_shielded(cleanup_task: asyncio.Task) -> None:
    """Wait for cleanup despite repeated cancellation of the caller."""

    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # The cleanup task remains alive. Defer every cancellation until
            # the spawned process has been stopped, then the caller re-raises
            # its original BaseException.
            continue
    cleanup_task.result()


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
        self._reaper_task: asyncio.Task | None = None

    @staticmethod
    def _has_resident_work(session: Session) -> bool:
        """Return whether automatic eviction must retain this Session."""

        return bool(
            session.has_pending_subagents
            or getattr(session, "idle_reap_protected", False)
        )

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
        # Lazily started here (not in __init__) so a running event loop is
        # guaranteed; hosts construct the pool at import/startup time.
        self._ensure_reaper()
        async with self._lock:
            self._access_order[session_id] = time.monotonic()

            if session_id in self._sessions:
                session = self._sessions[session_id]
                same_config = (
                    config_override is None
                    or (
                        session.config.config_dir == config_override.config_dir
                        and session.config.default_model == config_override.default_model
                        and session.config.default_effort == config_override.default_effort
                    )
                )
                if session.is_alive and same_config:
                    return session
                if session.is_alive:
                    # config changed (account rotation, model/effort switch):
                    # the old session must not be reused — stop it and respawn.
                    logger.info(
                        "Session %s config changed (config_dir/model/effort), recreating",
                        session_id,
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
            # Session.start has a spawn-to-publication window. Run it in an
            # independent task so cancellation of get_or_create cannot cancel
            # the executor-backed spawn and leave a live, untracked process.
            start_task = asyncio.create_task(
                session.start(initial_prompt=initial_prompt)
            )
            try:
                await asyncio.shield(start_task)
            except BaseException:
                cleanup_task = asyncio.create_task(
                    _stop_unpublished_session(session, start_task)
                )
                await _await_cleanup_shielded(cleanup_task)
                self._access_order.pop(session_id, None)
                raise
            self._sessions[session_id] = session
            return session

    async def _evict_one(self) -> bool:
        # Prefer idle sessions past timeout. Sessions with pending native
        # sub-agents are never idle — the main JSONL is silent only because
        # a sub-agent is still working and will wake it.
        candidates = []
        for sid, session in self._sessions.items():
            if (
                session.idle_seconds >= self.config.idle_timeout
                and not self._has_resident_work(session)
            ):
                candidates.append((self._access_order.get(sid, 0), sid))

        if not candidates:
            # Force-evict oldest that isn't mid-prompt or awaiting sub-agents
            for sid, session in self._sessions.items():
                if (
                    not session._send_lock.locked()
                    and not self._has_resident_work(session)
                ):
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
                and not session.has_pending_subagents
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

    def _ensure_reaper(self) -> None:
        """Start the periodic idle reaper if enabled and not yet running."""
        if self.config.idle_reap_after <= 0:
            return
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.idle_reap_interval)
            try:
                await self.reap_idle()
            except Exception:
                logger.exception("Idle reaper scan failed")

    async def reap_idle(self) -> int:
        """Stop and remove sessions idle for at least `idle_reap_after`.

        Unlike overflow eviction (`_evict_one`, which only runs when the pool
        is full) this reclaims resident-but-unused sessions on a schedule.
        Mid-prompt sessions and sessions awaiting native sub-agents are never
        touched; a reaped session's context survives on disk, so the next
        message on it simply cold-resumes.
        """
        async with self._lock:
            expired = [
                sid for sid, session in self._sessions.items()
                if session.idle_seconds >= self.config.idle_reap_after
                and not session._send_lock.locked()
                and not self._has_resident_work(session)
            ]
            reaped = 0
            for sid in expired:
                session = self._sessions.pop(sid)
                self._access_order.pop(sid, None)
                idle = session.idle_seconds
                try:
                    await session.stop()
                except Exception:
                    logger.exception("Failed to stop idle session %s", sid)
                reaped += 1
                logger.info(
                    "Reaped idle session %s (idle %.0fs >= %.0fs)",
                    sid, idle, self.config.idle_reap_after,
                )
            return reaped

    async def stop_all(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
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
