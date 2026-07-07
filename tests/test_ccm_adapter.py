"""CCM adapter regression tests: rotation must not orphan the dispatcher's proxy.

Incident (CCM prod 2026-07-05, tasks #19/#23): a chat turn hit a rate-limit
banner mid-turn → `_try_chat_pool_rotation` relaunched the turn inline, which
re-registered a fresh proxy/session/consumer under the same instance_id. The
exit handler then popped-and-completed the NEW proxy and deregistered the NEW
session, while the dispatcher kept awaiting the OLD proxy — parking the task's
queue consumer for the full 7200s task timeout while follow-up messages queued
unconsumed, and leaving the retried turn running detached.
"""

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from claude_pty.adapters.ccm import CCMBackend, _PTYProcessProxy


class _StubBridge:
    def start(self):
        pass

    def stop(self):
        pass


class _StoppableSession:
    def __init__(self, sid):
        self.session_id = sid
        self.stopped = False
        self._process = None

    async def stop(self):
        self.stopped = True


def _bound_pair():
    """A proxy↔session pair as launch_for_ccm binds them."""
    session = SimpleNamespace()
    proxy = _PTYProcessProxy()
    proxy.session = session
    session._ccm_proxy = proxy
    return proxy, session


@pytest.fixture
def backend(monkeypatch):
    import claude_pty.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "BridgeHub", _StubBridge)
    im = SimpleNamespace(
        processes={},
        _tasks={},
        _stopping=set(),
        _config_dirs={},
        _launch_params={},
    )
    return CCMBackend(im)


class _SAChain:
    """Chainable stand-in for sqlalchemy statement builders."""

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, name):
        return self


class _Col:
    """Column stand-in usable in filter expressions."""

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0

    def in_(self, *a, **k):
        return True


def _install_host_stubs(monkeypatch):
    """Fake the host-app modules on_exit imports (sqlalchemy, backend.models)."""
    sa = types.ModuleType("sqlalchemy")
    sa.update = _SAChain()
    sa.select = _SAChain()
    b = types.ModuleType("backend")
    bm = types.ModuleType("backend.models")
    bmi = types.ModuleType("backend.models.instance")
    bmi.Instance = type("Instance", (), {"id": _Col()})
    bmt = types.ModuleType("backend.models.task")
    bmt.Task = type("Task", (), {"id": _Col(), "status": _Col()})
    for name, mod in {
        "sqlalchemy": sa,
        "backend": b,
        "backend.models": bm,
        "backend.models.instance": bmi,
        "backend.models.task": bmt,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


class TestProxyChaining:
    def test_complete_is_first_wins(self):
        p = _PTYProcessProxy()
        p.complete(143)
        p.complete(0)
        assert p.returncode == 143

    async def test_chain_forwards_completion(self):
        old = _PTYProcessProxy()
        new = _PTYProcessProxy()
        new.chain(old)
        assert old.returncode is None
        new.complete(0)
        assert old.returncode == 0
        assert old._done.is_set()

    async def test_chain_on_already_done_completes_immediately(self):
        old = _PTYProcessProxy()
        new = _PTYProcessProxy()
        new.complete(7)
        new.chain(old)
        assert old.returncode == 7

    async def test_multi_hop_chain(self):
        # Double rotation in one message: C retries B retries A.
        a, b, c = _PTYProcessProxy(), _PTYProcessProxy(), _PTYProcessProxy()
        b.chain(a)
        c.chain(b)
        c.complete(0)
        assert b.returncode == 0
        assert a.returncode == 0

    async def test_killed_chain_target_keeps_first_result(self):
        # Dispatcher timeout kills the old proxy; the retry finishing later
        # must not overwrite the kill result.
        old = _PTYProcessProxy()
        new = _PTYProcessProxy()
        new.chain(old)
        old.kill()
        assert old.returncode == -9
        new.complete(0)
        assert old.returncode == -9


class TestRotationExit:
    async def test_rotation_chains_old_proxy_and_keeps_retry_registrations(
        self, backend, monkeypatch
    ):
        _install_host_stubs(monkeypatch)
        im = backend._im
        old_proxy, old_session = _bound_pair()
        new_proxy, new_session = _bound_pair()
        new_consumer = object()

        backend._proxies[3] = old_proxy
        backend._sessions[3] = old_session
        im.processes[3] = old_proxy

        async def fake_rotation(instance_id, task_id, ec, stderr):
            # The real rotation relaunches inline: launch_for_ccm registers a
            # fresh proxy/session/consumer under the same instance_id.
            backend._proxies[instance_id] = new_proxy
            backend._sessions[instance_id] = new_session
            backend._consumers[instance_id] = new_consumer
            im.processes[instance_id] = new_proxy
            return True

        im._try_chat_pool_rotation = fake_rotation

        await backend.on_exit(
            3, 1, chat_initiated=True, task_id=42, session=old_session
        )

        # Dispatcher (awaiting old_proxy) must NOT be woken mid-retry...
        assert old_proxy.returncode is None
        # ...and the retry's registrations must survive the old turn's exit.
        assert backend._proxies[3] is new_proxy
        assert backend._sessions[3] is new_session
        assert backend._consumers[3] is new_consumer
        assert im.processes[3] is new_proxy

        # When the retried turn finishes, its outcome reaches the dispatcher.
        new_proxy.complete(0)
        assert old_proxy.returncode == 0

    async def test_rotation_without_relaunch_completes_old_proxy(
        self, backend, monkeypatch
    ):
        _install_host_stubs(monkeypatch)
        im = backend._im
        old_proxy, old_session = _bound_pair()
        backend._proxies[3] = old_proxy
        backend._sessions[3] = old_session

        async def fake_rotation(instance_id, task_id, ec, stderr):
            return True  # rotated, but no fresh proxy appeared

        im._try_chat_pool_rotation = fake_rotation

        await backend.on_exit(
            3, 1, chat_initiated=True, task_id=42, session=old_session
        )

        # Never leave the dispatcher hanging on an event nobody will set.
        assert old_proxy.returncode == 1


class TestExitCleanupIdentity:
    async def test_exit_leaves_newer_registrations_and_completes_own_proxy(
        self, backend
    ):
        im = backend._im
        old_proxy, old_session = _bound_pair()
        new_proxy, new_session = _bound_pair()
        newer_consumer = object()
        newer_task = object()

        # The slot was already taken over by a newer launch.
        backend._proxies[7] = new_proxy
        backend._sessions[7] = new_session
        backend._consumers[7] = newer_consumer
        im.processes[7] = new_proxy
        im._tasks[7] = newer_task

        await backend.on_exit(7, 143, chat_initiated=False, session=old_session)

        # Our dispatcher is released with OUR exit code...
        assert old_proxy.returncode == 143
        # ...and the newer turn keeps every registration.
        assert backend._proxies[7] is new_proxy
        assert new_proxy.returncode is None
        assert backend._sessions[7] is new_session
        assert backend._consumers[7] is newer_consumer
        assert im.processes[7] is new_proxy
        assert im._tasks[7] is newer_task

    async def test_exit_pops_own_registrations(self, backend):
        im = backend._im
        proxy, session = _bound_pair()
        backend._proxies[5] = proxy
        backend._sessions[5] = session
        backend._consumers[5] = asyncio.current_task()
        im.processes[5] = proxy
        im._tasks[5] = asyncio.current_task()

        await backend.on_exit(5, 0, chat_initiated=False, session=session)

        assert proxy.returncode == 0
        assert 5 not in backend._proxies
        assert 5 not in backend._sessions
        assert 5 not in backend._consumers
        assert 5 not in im.processes
        assert 5 not in im._tasks


class TestForceKillScoping:
    async def test_mismatched_slot_kills_only_expected_session(self, backend):
        expected = _StoppableSession("sid-old")
        occupant = _StoppableSession("sid-new")
        backend._sessions[9] = occupant
        backend._pool._sessions["sid-old"] = expected

        await backend._force_kill(9, expected=expected)

        assert expected.stopped
        assert not occupant.stopped
        assert backend._sessions[9] is occupant
        assert "sid-old" not in backend._pool._sessions

    async def test_matching_slot_tears_down_normally(self, backend):
        occupant = _StoppableSession("sid-cur")
        backend._sessions[9] = occupant
        backend._pool._sessions["sid-cur"] = occupant

        await backend._force_kill(9, expected=occupant)

        assert occupant.stopped
        assert 9 not in backend._sessions
        assert "sid-cur" not in backend._pool._sessions


class TestBasePassesSession:
    async def test_consume_passes_session_to_on_exit(self):
        from claude_pty.adapters.base import BasePTYBackend

        seen = {}

        class RecBackend(BasePTYBackend):
            async def on_exit(self, key, exit_code, **ctx):
                seen["key"] = key
                seen.update(ctx)

        class FakeSession:
            _process = None

            async def send_prompt(self, prompt):
                return
                yield  # pragma: no cover

        b = RecBackend(max_sessions=1)
        s = FakeSession()
        await b._consume("k", s, "hi", task_id=1)
        assert seen["session"] is s
        assert seen["task_id"] == 1


class TestSubagentOnlyCallback:
    """The between-turns callback receives PTYEvent objects, not dicts.

    Session.on_autonomous_event delivers PTYEvent (see session.py); the
    subagent-only callback installed by on_exit indexed it like a dict and
    crashed with AttributeError on every idle-time autonomous event (CCM prod
    2026-07-07, task #27), so background sub-agent completions were never
    mirrored while the session sat between chat turns.
    """

    @pytest.fixture
    def chat_exit_im(self, backend):
        from contextlib import asynccontextmanager

        calls = []

        async def upsert(task_id, event_type, payload):
            calls.append((task_id, event_type, payload))

        class _FakeDB:
            async def execute(self, *a, **k):
                return SimpleNamespace(rowcount=0)

            async def commit(self):
                pass

        @asynccontextmanager
        async def db_factory():
            yield _FakeDB()

        async def broadcast(*a, **k):
            pass

        backend._im._upsert_native_sub_agent = upsert
        backend._im.db_factory = db_factory
        backend._im.broadcaster = SimpleNamespace(broadcast=broadcast)
        return calls

    async def test_callback_handles_ptyevent(self, backend, chat_exit_im, monkeypatch):
        from claude_pty.events import PTYEvent

        _install_host_stubs(monkeypatch)
        session = SimpleNamespace(session_id="sid-idle", _ccm_proxy=None)

        await backend.on_exit(9, 0, chat_initiated=True, task_id=77, session=session)

        cb = session.on_autonomous_event
        assert cb is not None

        payload = {"id": "agent-1", "status": "completed"}
        await cb(
            PTYEvent(
                event_type="subagent_completed", subagent=payload, autonomous=True
            )
        )
        assert chat_exit_im == [(77, "subagent_completed", payload)]

        # Non-subagent autonomous chatter is ignored, and must not raise.
        await cb(
            PTYEvent(
                event_type="message", role="assistant", content="hi", autonomous=True
            )
        )
        assert len(chat_exit_im) == 1
