"""Periodic idle reaper: reclaim resident-but-unused sessions.

Without the reaper, sessions are only reclaimed on pool overflow
(max_sessions), so on a lightly loaded host every session ever launched sits
resident forever — a full `claude` process each (observed: 18 sessions /
2.5 GB RSS on a 3.7 GB box, oldest idle for 3.5 days).
"""

import asyncio

from claude_pty.config import PTYConfig
from claude_pty.pool import SessionPool


class FakeSession:
    def __init__(self, idle: float = 0.0, pending_subagents: bool = False):
        self._idle = idle
        self._send_lock = asyncio.Lock()
        self._pending = pending_subagents
        self.stopped = False

    @property
    def idle_seconds(self) -> float:
        return self._idle

    @property
    def has_pending_subagents(self) -> bool:
        return self._pending

    async def stop(self):
        self.stopped = True


def _pool(**overrides) -> SessionPool:
    cfg = PTYConfig(**overrides)
    return SessionPool(config=cfg)


class TestReapIdle:
    async def test_reaps_only_expired_sessions(self):
        pool = _pool(idle_reap_after=100)
        old = FakeSession(idle=101)
        young = FakeSession(idle=99)
        pool._sessions = {"old": old, "young": young}
        pool._access_order = {"old": 1.0, "young": 2.0}

        reaped = await pool.reap_idle()

        assert reaped == 1
        assert old.stopped
        assert not young.stopped
        assert "old" not in pool._sessions
        assert "old" not in pool._access_order
        assert "young" in pool._sessions

    async def test_skips_mid_prompt_sessions(self):
        pool = _pool(idle_reap_after=100)
        busy = FakeSession(idle=500)
        await busy._send_lock.acquire()
        pool._sessions = {"busy": busy}

        assert await pool.reap_idle() == 0
        assert not busy.stopped
        assert "busy" in pool._sessions

    async def test_skips_sessions_with_pending_subagents(self):
        pool = _pool(idle_reap_after=100)
        waiting = FakeSession(idle=500, pending_subagents=True)
        pool._sessions = {"waiting": waiting}

        assert await pool.reap_idle() == 0
        assert not waiting.stopped

    async def test_stop_failure_still_removes_from_pool(self):
        pool = _pool(idle_reap_after=100)

        class ExplodingSession(FakeSession):
            async def stop(self):
                raise RuntimeError("boom")

        bad = ExplodingSession(idle=500)
        pool._sessions = {"bad": bad}

        assert await pool.reap_idle() == 1
        assert "bad" not in pool._sessions


class TestReaperLifecycle:
    async def test_disabled_when_reap_after_zero(self):
        pool = _pool(idle_reap_after=0)
        pool._ensure_reaper()
        assert pool._reaper_task is None

    async def test_ensure_reaper_starts_once(self):
        pool = _pool(idle_reap_after=100, idle_reap_interval=3600)
        pool._ensure_reaper()
        task = pool._reaper_task
        assert task is not None and not task.done()
        pool._ensure_reaper()
        assert pool._reaper_task is task
        task.cancel()

    async def test_stop_all_cancels_reaper(self):
        pool = _pool(idle_reap_after=100, idle_reap_interval=3600)
        pool._ensure_reaper()
        task = pool._reaper_task
        await pool.stop_all()
        assert pool._reaper_task is None
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()

    async def test_reap_loop_reaps_periodically(self):
        pool = _pool(idle_reap_after=0.01, idle_reap_interval=0.01)
        old = FakeSession(idle=1.0)
        pool._sessions = {"old": old}
        pool._access_order = {"old": 1.0}
        pool._ensure_reaper()
        try:
            for _ in range(100):
                if old.stopped:
                    break
                await asyncio.sleep(0.01)
            assert old.stopped
            assert "old" not in pool._sessions
        finally:
            pool._reaper_task.cancel()

    async def test_loop_survives_scan_errors(self):
        pool = _pool(idle_reap_after=0.01, idle_reap_interval=0.01)
        calls = {"n": 0}

        async def flaky_reap():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return 0

        pool.reap_idle = flaky_reap
        pool._ensure_reaper()
        try:
            for _ in range(100):
                if calls["n"] >= 2:
                    break
                await asyncio.sleep(0.01)
            assert calls["n"] >= 2  # kept scanning after the first failure
        finally:
            pool._reaper_task.cancel()
