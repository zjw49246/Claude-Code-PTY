"""Tests for pool.py — SessionPool LRU logic.

These tests mock Session to avoid spawning real CC processes.
"""

import asyncio
import time
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_pty.config import PTYConfig
from claude_pty.pool import SessionPool


def _make_mock_session(alive=True, idle=0.0, locked=False):
    session = MagicMock()
    session.is_alive = alive
    session.idle_seconds = idle
    session._send_lock = MagicMock()
    session._send_lock.locked.return_value = locked
    session.has_pending_subagents = False
    session.stop = AsyncMock()
    return session


class TestSessionPool:
    async def test_stats_empty(self):
        pool = SessionPool(config=PTYConfig(max_sessions=5))
        s = pool.stats()
        assert s["total"] == 0
        assert s["max"] == 5
        assert s["alive"] == 0

    async def test_stop_all(self):
        pool = SessionPool()
        s1 = _make_mock_session()
        s2 = _make_mock_session()
        pool._sessions = {"a": s1, "b": s2}
        pool._access_order = {"a": 1.0, "b": 2.0}

        await pool.stop_all()
        s1.stop.assert_awaited_once()
        s2.stop.assert_awaited_once()
        assert len(pool._sessions) == 0

    async def test_remove_session(self):
        pool = SessionPool()
        s1 = _make_mock_session()
        pool._sessions = {"a": s1}
        pool._access_order = {"a": 1.0}

        await pool.remove("a")
        assert "a" not in pool._sessions
        s1.stop.assert_awaited_once()

    async def test_remove_nonexistent(self):
        pool = SessionPool()
        await pool.remove("nope")  # should not raise

    async def test_evict_idle_first(self):
        pool = SessionPool(config=PTYConfig(max_sessions=2, idle_timeout=10))

        idle_session = _make_mock_session(idle=20.0)
        active_session = _make_mock_session(idle=1.0)
        pool._sessions = {"idle": idle_session, "active": active_session}
        pool._access_order = {"idle": 1.0, "active": 2.0}

        evicted = await pool._evict_one()
        assert evicted is True
        assert "idle" not in pool._sessions
        assert "active" in pool._sessions

    async def test_evict_oldest_unlocked_when_none_idle(self):
        pool = SessionPool(config=PTYConfig(max_sessions=2, idle_timeout=300))

        old = _make_mock_session(idle=5.0, locked=False)
        new = _make_mock_session(idle=1.0, locked=False)
        pool._sessions = {"old": old, "new": new}
        pool._access_order = {"old": 1.0, "new": 100.0}

        evicted = await pool._evict_one()
        assert evicted is True
        assert "old" not in pool._sessions

    async def test_evict_fails_when_all_locked(self):
        pool = SessionPool(config=PTYConfig(max_sessions=1, idle_timeout=300))

        locked = _make_mock_session(idle=1.0, locked=True)
        pool._sessions = {"locked": locked}
        pool._access_order = {"locked": 1.0}

        evicted = await pool._evict_one()
        assert evicted is False

    async def test_get_returns_existing(self):
        pool = SessionPool()
        s = _make_mock_session()
        pool._sessions = {"abc": s}
        assert await pool.get("abc") is s
        assert await pool.get("nope") is None

    async def test_failed_start_stops_unpublished_session(self):
        pool = SessionPool(config=PTYConfig(idle_reap_after=0))
        session = _make_mock_session()
        session.start = AsyncMock(
            side_effect=RuntimeError("failed after process spawn"),
        )

        with (
            patch("claude_pty.pool.Session", return_value=session),
            pytest.raises(RuntimeError, match="failed after process spawn"),
        ):
            await pool.get_or_create("failed", "/work")

        session.stop.assert_awaited_once()
        assert "failed" not in pool._sessions
        assert "failed" not in pool._access_order

    async def test_cancelled_start_settles_then_stops_unpublished_session(self):
        pool = SessionPool(config=PTYConfig(idle_reap_after=0))
        started = asyncio.Event()
        allow_start_to_settle = asyncio.Event()
        stop_started = asyncio.Event()
        allow_stop_to_finish = asyncio.Event()

        class SpawnedSession:
            def __init__(self):
                self.process_alive = False

            async def start(self, initial_prompt=None):
                self.process_alive = True
                started.set()
                await allow_start_to_settle.wait()

            async def stop(self):
                stop_started.set()
                await allow_stop_to_finish.wait()
                self.process_alive = False

        session = SpawnedSession()
        with patch("claude_pty.pool.Session", return_value=session):
            request = asyncio.create_task(
                pool.get_or_create("cancelled", "/work"),
            )
            try:
                await started.wait()
                request.cancel()
                await asyncio.sleep(0)

                # The caller's cancellation must not cancel Session.start in
                # the spawn-to-publication window.
                assert not request.done()
                assert session.process_alive

                allow_start_to_settle.set()
                await stop_started.wait()

                # A repeated cancellation must not interrupt process cleanup.
                request.cancel()
                await asyncio.sleep(0)
                assert not request.done()

                allow_stop_to_finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await request
            finally:
                allow_start_to_settle.set()
                allow_stop_to_finish.set()
                if not request.done():
                    request.cancel()
                with suppress(asyncio.CancelledError):
                    await request

        assert not session.process_alive
        assert "cancelled" not in pool._sessions
        assert "cancelled" not in pool._access_order
