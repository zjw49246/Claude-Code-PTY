from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Callable

from .config import PTYConfig
from .events import PTYEvent, EventType
from .pty_process import PTYProcess
from .jsonl_reader import JsonlReader
from .bridge import BridgeHub
from .exceptions import SessionError

logger = logging.getLogger(__name__)


class Session:
    """High-level session combining PTYProcess + JsonlReader.

    Core API:
        session = Session(cwd="/project")
        await session.start()
        async for event in session.send_prompt("do something"):
            print(event.to_dict())
    """

    def __init__(
        self,
        cwd: str,
        session_id: str | None = None,
        config: PTYConfig | None = None,
        bridge: BridgeHub | None = None,
        channel_inject_port: int | None = None,
    ):
        self.config = config or PTYConfig()
        self._process: PTYProcess | None = None
        self._reader: JsonlReader | None = None
        self._started = False
        self._restart_count = 0
        self._last_activity: float = time.monotonic()
        self._cwd = cwd
        self._session_id = session_id
        self._send_lock = asyncio.Lock()
        self._bridge = bridge
        self._channel_inject_port = channel_inject_port

    @property
    def session_id(self) -> str | None:
        if self._process:
            return self._process.session_id
        return self._session_id

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    @property
    def jsonl_path(self) -> str | None:
        return self._process.jsonl_path if self._process else None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        resume_id = self._session_id if self._restart_count > 0 else None

        self._process = PTYProcess(
            cwd=self._cwd,
            session_id=self._session_id,
            config=self.config,
            on_death=self._on_process_death,
            channel_inject_port=self._channel_inject_port,
            bridge_port=self._bridge.port if self._bridge else None,
        )

        await loop.run_in_executor(None, self._process.spawn, resume_id)

        self._session_id = self._process.session_id
        self._reader = JsonlReader(self._process.jsonl_path)

        await asyncio.sleep(self.config.startup_wait)
        await loop.run_in_executor(None, self._reader.read_new_messages)

        if self._bridge and self._channel_inject_port:
            self._bridge.register_session(
                self._process.session_id, self._channel_inject_port
            )

        self._started = True
        self._last_activity = time.monotonic()
        logger.info(
            "Session %s started (pid=%s, cwd=%s, channels=%s)",
            self.session_id,
            self._process.pid,
            self._cwd,
            self._process.channels_enabled,
        )

    async def send_prompt(
        self,
        text: str,
        timeout: float | None = None,
    ) -> AsyncIterator[PTYEvent]:
        async with self._send_lock:
            async for event in self._send_prompt_inner(text, timeout):
                yield event

    async def _send_prompt_inner(
        self,
        text: str,
        timeout: float | None,
    ) -> AsyncIterator[PTYEvent]:
        if not self._started or not self._process:
            raise SessionError("Session not started. Call start() first.")

        if not self._process.is_alive:
            await self._auto_resume()

        timeout = timeout or self.config.response_timeout
        loop = asyncio.get_running_loop()

        await loop.run_in_executor(None, self._process.send_prompt, text)
        self._last_activity = time.monotonic()

        deadline = time.monotonic() + timeout
        response_complete = False

        while not response_complete and time.monotonic() < deadline:
            if not self._process.is_alive:
                yield PTYEvent(
                    event_type=EventType.SESSION_CRASHED,
                    content=f"Process died (exit_code={self._process.exit_code})",
                    is_error=True,
                    session_id=self.session_id,
                )
                break

            messages = await loop.run_in_executor(
                None, self._reader.read_new_messages
            )

            for raw in messages:
                for event in self._reader.normalize(raw):
                    self._last_activity = time.monotonic()
                    yield event

                if self._reader.is_response_complete(raw):
                    response_complete = True
                    break

            if not response_complete:
                await asyncio.sleep(self.config.jsonl_poll_interval)

        if not response_complete and time.monotonic() >= deadline:
            yield PTYEvent(
                event_type=EventType.SYSTEM_EVENT,
                content=f"Response timed out after {timeout}s",
                is_error=True,
                session_id=self.session_id,
            )

        await asyncio.sleep(self.config.post_response_wait)

    async def send_interrupt(self) -> None:
        if self._process:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._process.send_interrupt)

    def on_permission_request(
        self, handler: Callable[[str, dict], None]
    ) -> None:
        """Register a callback for permission requests from CC.

        handler(session_id, request) is called synchronously from the
        BridgeHub HTTP thread. Use resolve_permission() to respond.
        """
        if not self._bridge:
            raise SessionError(
                "Cannot register permission handler: session was not created "
                "with channels enabled."
            )
        self._bridge.on_permission_request(handler)

    async def resolve_permission(
        self, request_id: str, behavior: str = "allow"
    ) -> bool:
        """Resolve a pending permission request.

        behavior: "allow" or "deny"
        """
        if not self._bridge:
            raise SessionError(
                "Cannot resolve permission: session was not created "
                "with channels enabled."
            )
        if not self.session_id:
            raise SessionError("Cannot resolve permission: session has no ID")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._bridge.resolve_permission,
            self.session_id,
            request_id,
            behavior,
        )

    async def inject(self, content: str, meta: dict | None = None) -> bool:
        """Inject a message into CC's context mid-execution via Channels.

        Requires channels=True when creating the session. The message appears
        as a <channel source="pty-bridge"> tag in CC's context at the next
        tool call boundary.

        Returns True if sent successfully.
        """
        if not self._bridge:
            raise SessionError(
                "Cannot inject: session was not created with channels enabled. "
                "Pass bridge and channel_inject_port to enable."
            )
        if not self.session_id:
            raise SessionError("Cannot inject: session has no ID")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._bridge.inject, self.session_id, content, meta
        )

    async def _auto_resume(self) -> None:
        if self._restart_count >= self.config.max_restart_attempts:
            raise SessionError(
                f"Session {self.session_id} exceeded max restart attempts "
                f"({self.config.max_restart_attempts})"
            )

        logger.warning(
            "Session %s process died, attempting resume (#%d)",
            self.session_id,
            self._restart_count + 1,
        )

        if self._process:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._process.stop)

        self._restart_count += 1
        backoff = self.config.restart_backoff_base ** self._restart_count
        await asyncio.sleep(backoff)

        await self.start()

    def _on_process_death(self, proc: PTYProcess) -> None:
        logger.warning(
            "Session %s process died (pid=%s)", self.session_id, proc.pid
        )

    async def stop(self) -> None:
        if self._bridge and self.session_id:
            self._bridge.unregister_session(self.session_id)
        if self._process:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._process.stop)
        self._started = False
        logger.info("Session %s stopped", self.session_id)
