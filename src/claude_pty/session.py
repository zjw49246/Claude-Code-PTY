from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from typing import AsyncIterator, Callable

from .config import PTYConfig
from .events import PTYEvent, EventType
from .pty_process import PTYProcess
from .jsonl_reader import JsonlReader
from .subagents import SubagentTracker
from .bridge import BridgeHub
from .exceptions import SessionError, SteerDeliveryUncertainError

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _PendingSteer:
    """One stdin update awaiting CC's queue-operation acknowledgement."""

    content: str
    process: PTYProcess
    acknowledged: asyncio.Future[bool]
    enqueued: bool = False
    delivery_uncertain: bool = False
    prewrite_message_ids: set[int] = dataclasses.field(default_factory=set)


class _PredicatePromptEchoMatcher:
    """Compatibility adapter for reader doubles with the legacy predicate."""

    def __init__(self, reader, prompt: str):
        self._reader = reader
        self._prompt = prompt

    def observe(self, raw: dict) -> bool:
        return self._reader.is_prompt_echo(raw, self._prompt)


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
        resume_existing: bool = False,
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
        # ``send_prompt`` owns JSONL consumption for a whole foreground turn,
        # while live steering must run concurrently with that consumer.  Keep
        # a separate, short-held state lock and expose only the exact native
        # process whose prompt delivery has been confirmed.
        self._turn_state_lock = asyncio.Lock()
        self._active_turn_owner: object | None = None
        self._active_turn_process: PTYProcess | None = None
        self._pending_steer: _PendingSteer | None = None
        # A queued stdin steer may cross the original turn boundary. Keep the
        # exact process fenced until JSONL proves that the update was absorbed
        # or that its dequeued follow-up reached a terminal record.
        self._unsettled_steer_process: PTYProcess | None = None
        # Only one write may await queue acknowledgement at a time.  The
        # process-level lock prevents byte interleaving; this lock also keeps
        # queue-operation records attributable without synthetic prompt text.
        self._steer_admission_lock = asyncio.Lock()
        # ``steer_active_turn`` may need to inspect unread JSONL while proving
        # that a terminal sentinel is not already durable.  It must not steal
        # those records from the foreground consumer, so inspected batches are
        # handed back through this buffer under ``_reader_lock``.
        self._prefetched_messages: list[dict] = []
        self._bridge = bridge
        self._channel_inject_port = channel_inject_port
        # True when session_id refers to an existing CC session on disk:
        # spawn with --resume instead of --session-id (which would collide).
        self._resume_existing = resume_existing
        self._pending_prompt: str | None = None
        self._rate_limited_turn = False
        # When True, the next _deliver_prompt skips channel inject and goes
        # straight to PTY stdin. Used after cold resume (service restart) to
        # avoid the prompt being wrapped in <channel source="pty-bridge"> tags
        # which Claude misinterprets as "No response requested." because the
        # old JSONL already contains identical channel tags.
        self._force_stdin_next = False
        # Native sub-agent tracking (Agent/Task/Monitor tools in the JSONL)
        self._tracker = SubagentTracker()
        # Serializes JSONL reads between send_prompt and the idle watcher
        self._reader_lock = asyncio.Lock()
        self._idle_watcher_task: asyncio.Task | None = None
        # Host callback for events consumed outside send_prompt (autonomous
        # turns: sub-agent notifications waking the session). Without a
        # consumer those events used to pile up unread and got misattributed
        # to the NEXT prompt (task-87 off-by-one incident).
        self.on_autonomous_event = None  # async (PTYEvent) -> None

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

    @property
    def active_turn_process(self) -> PTYProcess | None:
        """Opaque identity of the exact foreground process that can be steered.

        Callers may capture this object under their own lifecycle lock and pass
        it back to :meth:`steer_active_turn` as ``expected_process``.  The
        method revalidates identity while holding the Session state lock, so
        this property is only a snapshot and never grants authority by itself.
        """

        process = self._active_turn_process
        if (
            process is None
            or process is not self._process
            or not self._send_lock.locked()
            or not process.is_alive
        ):
            return None
        return process

    @property
    def has_pending_subagents(self) -> bool:
        """True while model-spawned sub-agents (Agent/Monitor) are pending.

        Such a session must not be treated as idle/evictable: the main JSONL
        is silent, but a sub-agent is still working and will wake it.
        """
        return self._tracker.has_pending

    @property
    def rate_limited(self) -> bool:
        """True when this session hit a usage/rate limit (PTY banner or
        structured JSONL signal). The host should rotate accounts."""
        proc_flag = bool(self._process and getattr(self._process, "rate_limited", False))
        return proc_flag or self._rate_limited_turn

    def _rate_limit_event(self) -> PTYEvent:
        return PTYEvent(
            event_type=EventType.MESSAGE,
            role="assistant",
            content=(
                "usage limit reached — account hit its rate limit "
                "(detected in PTY session)"
            ),
            is_error=True,
            session_id=self.session_id,
        )

    @staticmethod
    def _structured_rate_limit_is_hard(raw: dict) -> bool:
        """Return whether one structured quota record aborts the turn."""

        if raw.get("error") == "rate_limit":
            return True
        if raw.get("type") != "rate_limit_event":
            return False
        info = raw.get("rate_limit_info")
        if not isinstance(info, dict):
            return True
        if bool(info.get("hard_limit")):
            return True
        status = str(info.get("status") or "").lower()
        return status not in {"allowed", "allowed_warning"}

    @staticmethod
    def _queue_operation_matches_prompt(raw: dict, prompt: str) -> bool:
        """Return whether an enqueue record can be attributed to ``prompt``.

        Queue-operation records are also emitted for native child
        notifications.  Treating any such record as confirmation of a
        channel-delivered prompt suppresses the stdin fallback and can leave
        the consumer waiting for an echo that will never arrive.  Only an
        enqueue whose content contains this exact prompt (plain or inside the
        known channel wrapper) is evidence for the current turn.
        """

        if (
            raw.get("type") != "queue-operation"
            or raw.get("operation") != "enqueue"
        ):
            return False
        content = raw.get("content")
        needle = str(prompt or "").strip()
        if not isinstance(content, str) or not needle:
            return False
        candidate = content.strip()
        if candidate == needle:
            return True
        if not (
            candidate.startswith("<channel")
            and candidate.endswith("</channel>")
        ):
            return False
        opening_end = candidate.find(">")
        closing_start = candidate.rfind("</channel>")
        if opening_end < 0 or closing_start <= opening_end:
            return False
        wrapped = candidate[opening_end + 1 : closing_start].strip()
        return wrapped == needle

    async def start(self, initial_prompt: str | None = None) -> None:
        loop = asyncio.get_running_loop()

        # A new JsonlReader belongs to a new native-process epoch. Do not
        # replay object identities prefetched from the previous reader.
        async with self._turn_state_lock:
            self._prefetched_messages.clear()

        resume_id = (
            self._session_id
            if (self._restart_count > 0 or (self._resume_existing and self._session_id))
            else None
        )

        # Cold resume (service restart → first message): force stdin delivery
        # to avoid channel tags that Claude confuses with old JSONL content.
        if resume_id and self._resume_existing and self._restart_count == 0:
            self._force_stdin_next = True

        self._process = PTYProcess(
            cwd=self._cwd,
            session_id=self._session_id,
            config=self.config,
            on_death=self._on_process_death,
            channel_inject_port=self._channel_inject_port,
            bridge_port=self._bridge.port if self._bridge else None,
        )

        await loop.run_in_executor(None, self._process.spawn, resume_id)

        # NOTE: prompts are never written at spawn time. The TUI is not ready
        # yet and a stdin write here gets silently swallowed (observed in
        # production: cold-resumed turns never started, consumer hung until
        # timeout). Delivery happens in send_prompt via channel injection
        # (with retries) after startup_wait.
        self._pending_prompt = None

        self._session_id = self._process.session_id
        self._tracker.set_jsonl_path(self._process.jsonl_path)
        self._reader = JsonlReader(self._process.jsonl_path, tracker=self._tracker)

        await asyncio.sleep(self.config.startup_wait)
        await loop.run_in_executor(None, self._reader.read_new_messages)

        # CC --resume in interactive mode auto-generates a "Continue from
        # where you left off." turn. Wait for it to finish (turn_duration
        # sentinel) before returning, so send_prompt doesn't race with it.
        if resume_id:
            settle_deadline = time.monotonic() + 30
            settled = False
            while time.monotonic() < settle_deadline and getattr(self._process, "is_alive", False):
                await asyncio.sleep(1.0)
                msgs = await loop.run_in_executor(
                    None, self._reader.read_new_messages
                )
                if not msgs:
                    continue
                has_turn_end = any(
                    m.get("type") == "system"
                    and m.get("subtype") == "turn_duration"
                    for m in msgs
                )
                if has_turn_end:
                    settled = True
                    break
            if not settled and hasattr(self._process, "is_alive") and not self._process.is_alive:
                ec = getattr(self._process, "exit_code", None)
                logger.warning(
                    "Session %s: process died during resume settle (exit_code=%s)",
                    self.session_id, ec,
                )
                raise SessionError(
                    f"Session {self.session_id} process died during resume "
                    f"(exit_code={ec}) — likely auth failure or stale session"
                )

        if self._bridge and self._channel_inject_port:
            self._bridge.register_session(
                self._process.session_id, self._channel_inject_port
            )

        self._started = True
        self._last_activity = time.monotonic()
        if self._idle_watcher_task is None or self._idle_watcher_task.done():
            self._idle_watcher_task = asyncio.create_task(self._idle_watcher())
        logger.info(
            "Session %s started (pid=%s, cwd=%s, channels=%s)",
            self.session_id,
            self._process.pid,
            self._cwd,
            self._process.channels_enabled,
        )

    _SLASH_COMMANDS = frozenset({
        "/help", "/exit", "/clear", "/compact", "/config", "/cost",
        "/doctor", "/init", "/login", "/logout", "/memory", "/mcp",
        "/permissions", "/review", "/status", "/terminal-setup", "/vim",
    })

    def _is_slash_command(self, text: str) -> bool:
        cmd = text.strip().split()[0] if text.strip() else ""
        return cmd in self._SLASH_COMMANDS or (cmd.startswith("/") and len(cmd) > 1)

    async def send_prompt(
        self,
        text: str,
        timeout: float | None = None,
    ) -> AsyncIterator[PTYEvent]:
        if self._is_slash_command(text):
            cmd = text.strip().split()[0]
            yield PTYEvent(
                event_type=EventType.RESULT,
                role="system",
                content=f"Slash command '{cmd}' is not supported in PTY mode. Use $ commands (e.g. $help) for CCM skills.",
                is_error=True,
            )
            return
        async with self._send_lock:
            turn_owner = object()
            async with self._turn_state_lock:
                self._active_turn_owner = turn_owner
                self._active_turn_process = None
            try:
                async for event in self._send_prompt_inner(
                    text, timeout, turn_owner
                ):
                    yield event
            finally:
                await self._deactivate_active_turn(turn_owner)

    async def _activate_active_turn(
        self,
        turn_owner: object,
        process: PTYProcess,
    ) -> None:
        """Publish steerability only for this send_prompt/process epoch."""

        async with self._turn_state_lock:
            if (
                self._active_turn_owner is turn_owner
                and self._process is process
                and process.is_alive
            ):
                self._active_turn_process = process

    async def _deactivate_active_turn(self, turn_owner: object) -> None:
        stop_process: PTYProcess | None = None
        resolve_after_stop: _PendingSteer | None = None
        async with self._turn_state_lock:
            if self._active_turn_owner is turn_owner:
                self._active_turn_process = None
                self._active_turn_owner = None
                pending, self._pending_steer = self._pending_steer, None
                stop_process = self._unsettled_steer_process
                self._unsettled_steer_process = None
                if (
                    pending is not None
                    and not pending.acknowledged.done()
                ):
                    if stop_process is not None:
                        resolve_after_stop = pending
                    else:
                        pending.acknowledged.set_result(False)

        try:
            if stop_process is not None and stop_process.is_alive:
                logger.warning(
                    "Session %s: stopping exact process after an unsettled "
                    "stdin steer lost its foreground consumer",
                    self.session_id,
                )
                await self._settle_blocking_input(stop_process.stop)
        finally:
            if (
                resolve_after_stop is not None
                and not resolve_after_stop.acknowledged.done()
            ):
                resolve_after_stop.acknowledged.set_result(False)

    async def _fail_closed_steer(
        self,
        pending: _PendingSteer,
        *,
        reason: str,
    ) -> bool:
        """Revoke one ambiguous stdin write and stop its exact process."""

        process = pending.process
        async with self._turn_state_lock:
            if self._pending_steer is pending:
                self._pending_steer = None
            if self._unsettled_steer_process is process:
                self._unsettled_steer_process = None
            if self._active_turn_process is process:
                self._active_turn_process = None
                self._active_turn_owner = None
            if not pending.acknowledged.done():
                pending.acknowledged.set_result(False)

        if process.is_alive:
            logger.warning(
                "Session %s: stopping exact process after %s",
                self.session_id,
                reason,
            )
            await self._settle_blocking_input(process.stop)
        return True

    async def _read_with_prefetch_locked(self) -> list[dict]:
        """Read one JSONL batch while state and reader locks are held."""

        messages, self._prefetched_messages = (
            self._prefetched_messages,
            [],
        )
        if self._reader is not None:
            messages.extend(
                await asyncio.to_thread(self._reader.read_new_messages)
            )
        return messages

    def _new_prompt_echo_matcher(self, prompt: str):
        factory = getattr(self._reader, "prompt_echo_matcher", None)
        if callable(factory):
            return factory(prompt)
        return _PredicatePromptEchoMatcher(self._reader, prompt)

    @staticmethod
    async def _settle_blocking_input(call, *args) -> None:
        """Do not release input ownership while a cancelled thread still writes."""

        work = asyncio.create_task(asyncio.to_thread(call, *args))
        delayed_cancel: asyncio.CancelledError | None = None
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError as exc:
                if delayed_cancel is None:
                    delayed_cancel = exc
        work.result()
        if delayed_cancel is not None:
            raise delayed_cancel

    async def steer_active_turn(
        self,
        content: str,
        *,
        expected_process: PTYProcess | None = None,
    ) -> bool:
        """Submit stdin steering to the exact active foreground turn.

        Unlike :meth:`inject`, this does not use Channels.  It succeeds only
        after the owning prompt has been observed in CC's JSONL and before its
        terminal boundary.  ``expected_process`` is an optional ABA fence for
        hosts that reuse one Session id across native process restarts.

        ``True`` proves both the complete bracketed-paste + Enter write and
        CC's matching queue-operation acknowledgement. If the write completes
        but no acknowledgement arrives before the configured deadline,
        :class:`SteerDeliveryUncertainError` is raised: callers must not retry
        automatically, while this Session quarantines further steering until
        the original turn reaches an authoritative boundary. If the write
        races that boundary, the same foreground consumer follows a proven
        dequeued prompt through its next terminal sentinel.
        """

        if (
            not content
            or self._is_slash_command(content)
            or "\x00" in content
            or "\x1b" in content
        ):
            return False
        operation = asyncio.create_task(
            self._steer_active_turn_inner(content, expected_process)
        )
        delayed_cancel: asyncio.CancelledError | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                if delayed_cancel is None:
                    delayed_cancel = exc
        result = operation.result()
        if delayed_cancel is not None:
            raise delayed_cancel
        return result

    async def _steer_active_turn_inner(
        self,
        content: str,
        expected_process: PTYProcess | None,
    ) -> bool:
        async with self._steer_admission_lock:
            loop = asyncio.get_running_loop()
            pending: _PendingSteer | None = None
            async with self._turn_state_lock:
                process = self._active_turn_process
                if (
                    process is None
                    or self._active_turn_owner is None
                    or self._pending_steer is not None
                    or self._unsettled_steer_process is not None
                    or not self._send_lock.locked()
                    or process is not self._process
                    or not process.is_alive
                    or (
                        expected_process is not None
                        and process is not expected_process
                    )
                ):
                    return False
                pending = _PendingSteer(
                    content=content,
                    process=process,
                    acknowledged=loop.create_future(),
                )
                # Register before reading or writing.  A foreground consumer
                # that already read turn_duration must now defer its exit;
                # otherwise this stdin write could create an orphan next turn.
                self._pending_steer = pending

                if self._reader is None:
                    self._pending_steer = None
                    return False
                try:
                    async with self._reader_lock:
                        unread = await asyncio.to_thread(
                            self._reader.read_new_messages
                        )
                        if unread:
                            self._prefetched_messages.extend(unread)
                except Exception:
                    self._pending_steer = None
                    logger.exception(
                        "Session %s: active-turn JSONL preflight failed",
                        self.session_id,
                    )
                    return False

                if any(
                    self._reader.is_response_complete(raw)
                    or raw.get("isApiErrorMessage")
                    or self._structured_rate_limit_is_hard(raw)
                    for raw in unread
                ):
                    self._pending_steer = None
                    self._active_turn_process = None
                    self._active_turn_owner = None
                    return False

                pending.prewrite_message_ids.update(map(id, unread))

                try:
                    # Once a PTY write starts, any ambiguous exit must reap
                    # this exact process before reporting failure. Otherwise
                    # a client retry could execute both the old and new text.
                    self._unsettled_steer_process = process
                    await self._settle_blocking_input(
                        process.send_prompt, content
                    )
                except BaseException as exc:
                    write_error: BaseException | None = exc
                else:
                    write_error = None
                    self._last_activity = time.monotonic()

            if write_error is not None:
                logger.error(
                    "Session %s: active-turn stdin steering failed",
                    self.session_id,
                    exc_info=(
                        type(write_error),
                        write_error,
                        write_error.__traceback__,
                    ),
                )
                await self._fail_closed_steer(
                    pending,
                    reason="an incomplete active-turn stdin write",
                )
                if isinstance(write_error, asyncio.CancelledError):
                    raise write_error
                return False

            try:
                return bool(
                    await asyncio.wait_for(
                        asyncio.shield(pending.acknowledged),
                        timeout=self.config.inject_confirm_timeout,
                    )
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Session %s: stdin steering was not acknowledged within %.1fs",
                    self.session_id,
                    self.config.inject_confirm_timeout,
                )
                # The complete bracketed-paste + Enter write is already an
                # at-most-once side effect. Re-check at the deadline edge, then
                # quarantine this turn instead of killing a healthy Claude
                # process that may still be inside a long tool call.
                async with self._turn_state_lock:
                    if pending.acknowledged.done():
                        return bool(pending.acknowledged.result())
                    if (
                        self._pending_steer is not pending
                        or self._unsettled_steer_process is not pending.process
                    ):
                        return False
                    pending.delivery_uncertain = True
                raise SteerDeliveryUncertainError(
                    "Active-turn stdin write completed but Claude did not "
                    "acknowledge it; delivery is uncertain and must not be "
                    "retried automatically"
                )

    # Channel server boots with CC's MCP startup; retry injection briefly
    # before falling back to PTY stdin.
    _INJECT_ATTEMPTS = 15
    _INJECT_RETRY_INTERVAL = 2.0

    async def _deliver_prompt(self, text: str) -> str:
        """Deliver a prompt to CC: channel injection first, stdin fallback.

        Channel injection (an MCP notification) is the preferred path — it
        bypasses the TUI input layer entirely, so prompt content can never
        interact with keybindings, slash-command completion, or paste
        handling. Verified to wake an idle session into a new turn.

        Returns the delivery method: "channel" or "stdin". A "channel" result
        only means the notification reached the channel server (HTTP 200) —
        CC may still drop it (e.g. while booting), so the caller must confirm
        the turn actually started via JSONL activity.
        """
        loop = asyncio.get_running_loop()

        # Cold-resume stdin override: skip channel inject entirely so the
        # prompt arrives without <channel source="pty-bridge"> tags.
        if self._force_stdin_next:
            self._force_stdin_next = False
            logger.info(
                "Session %s: cold-resume stdin override — delivering via "
                "PTY stdin to avoid channel tag confusion (%d chars)",
                self.session_id, len(text),
            )
            await loop.run_in_executor(None, self._process.send_prompt, text)
            return "stdin"

        if self._bridge and self._channel_inject_port:
            for attempt in range(1, self._INJECT_ATTEMPTS + 1):
                ok = await loop.run_in_executor(
                    None, self._bridge.inject, self.session_id, text, None
                )
                if ok:
                    logger.info(
                        "Session %s: prompt delivered via channel (%d chars)",
                        self.session_id, len(text),
                    )
                    return "channel"
                if attempt < self._INJECT_ATTEMPTS:
                    await asyncio.sleep(self._INJECT_RETRY_INTERVAL)
            logger.warning(
                "Session %s: channel inject failed %d times, "
                "falling back to PTY stdin",
                self.session_id, self._INJECT_ATTEMPTS,
            )

        logger.info(
            "Session %s: sending prompt via PTY stdin (%d chars)",
            self.session_id, len(text),
        )
        await loop.run_in_executor(None, self._process.send_prompt, text)
        return "stdin"

    async def _send_prompt_inner(
        self,
        text: str,
        timeout: float | None,
        turn_owner: object,
    ) -> AsyncIterator[PTYEvent]:
        if not self._started or not self._process:
            raise SessionError("Session not started. Call start() first.")

        if not self._process.is_alive:
            await self._auto_resume(text)

        turn_process = self._process

        timeout = timeout or self.config.response_timeout
        loop = asyncio.get_running_loop()

        # Drain any backlog left by autonomous turns (sub-agent notifications
        # waking the session while no consumer was attached). Yield it flagged
        # orphan so the host can log it WITHOUT mistaking it for this turn's
        # reply, and crucially without counting its stale turn_duration as our
        # completion sentinel (the task-87 off-by-one). With the idle watcher
        # running this is normally empty — it is the last line of defense.
        async with self._turn_state_lock:
            async with self._reader_lock:
                backlog = await self._read_with_prefetch_locked()
        for raw in backlog:
            for event in self._reader.normalize(raw, include_user_text=True):
                event.orphan = True
                yield event

        # Skip sending if prompt was already sent during start() (resume case)
        if self._pending_prompt and self._pending_prompt == text:
            logger.info("Session %s: prompt already sent during start, skipping re-send", self.session_id)
            self._pending_prompt = None
            delivery = "channel"
        else:
            self._pending_prompt = None
            delivery = await self._deliver_prompt(text)
        self._last_activity = time.monotonic()

        deadline = time.monotonic() + timeout
        response_complete = False
        self._rate_limited_turn = False
        api_error_turn = False
        turn_had_messages = False
        # Our turn starts only at the JSONL echo of OUR prompt. Until then,
        # turn_duration sentinels belong to earlier/in-flight turns and must
        # not complete this one. (If CC was mid-turn when the prompt arrived,
        # it queues the prompt and echoes it when the new turn begins.)
        turn_started = False
        prompt_echo_matcher = self._new_prompt_echo_matcher(text)
        steer_followup_prompt: str | None = None
        steer_followup_echo_matcher = None
        steer_followup_started = False
        terminal_deferred = False
        last_subagent_check = 0.0
        # Channel inject "success" is no proof CC consumed the notification
        # (observed in production: inject 13ms after a resume spawn was
        # silently dropped — message blackholed for 30 min). Confirm the turn
        # started via JSONL activity; otherwise re-send once via stdin.
        confirm_deadline = (
            time.monotonic() + self.config.inject_confirm_timeout
            if delivery == "channel"
            else None
        )

        while not response_complete and time.monotonic() < deadline:
            if self._process is not turn_process or not turn_process.is_alive:
                await self._deactivate_active_turn(turn_owner)
                yield PTYEvent(
                    event_type=EventType.SESSION_CRASHED,
                    content=(
                        "Process was replaced during the foreground turn"
                        if self._process is not turn_process
                        else f"Process died (exit_code={turn_process.exit_code})"
                    ),
                    is_error=True,
                    session_id=self.session_id,
                )
                break

            async with self._turn_state_lock:
                async with self._reader_lock:
                    messages = await self._read_with_prefetch_locked()

                # Inspect the complete batch before yielding any event.  A pending
                # stdin steer owns the terminal edge: enqueue+remove means CC
                # absorbed it into this turn; enqueue+dequeue means it became a
                # next turn which this same consumer must continue to collect.
                scan_started = turn_started
                batch_response_complete = False
                api_error_in_batch = False
                rate_limit_in_batch = False
                enqueued_pending_in_batch: _PendingSteer | None = None
                uncertain_terminal_in_batch: _PendingSteer | None = None
                prompt_echo_ids: set[int] = set()
                for raw in messages:
                    if (
                        not scan_started
                        and prompt_echo_matcher.observe(raw)
                    ):
                        scan_started = True
                        prompt_echo_ids.add(id(raw))

                    pending = self._pending_steer
                    if pending is not None:
                        operation = (
                            raw.get("operation")
                            if raw.get("type") == "queue-operation"
                            else None
                        )
                        if (
                            operation == "enqueue"
                            and raw.get("content") == pending.content
                            and id(raw) not in pending.prewrite_message_ids
                        ):
                            pending.enqueued = True
                            enqueued_pending_in_batch = pending
                        elif (
                            pending.enqueued
                            and operation in {"remove", "dequeue"}
                        ):
                            if operation == "dequeue" or terminal_deferred:
                                steer_followup_prompt = pending.content
                                steer_followup_echo_matcher = (
                                    self._new_prompt_echo_matcher(
                                        steer_followup_prompt
                                    )
                                )
                                steer_followup_started = False
                                self._active_turn_process = None
                            elif (
                                self._unsettled_steer_process
                                is pending.process
                            ):
                                # The update was folded into the current turn;
                                # no cross-turn routing remains to guard.
                                self._unsettled_steer_process = None
                            self._pending_steer = None

                    if (
                        steer_followup_prompt is not None
                        and not steer_followup_started
                        and steer_followup_echo_matcher is not None
                        and steer_followup_echo_matcher.observe(raw)
                    ):
                        steer_followup_started = True
                        terminal_deferred = False
                        if (
                            self._active_turn_owner is turn_owner
                            and self._process is turn_process
                            and turn_process.is_alive
                        ):
                            self._active_turn_process = turn_process

                    if raw.get("isApiErrorMessage"):
                        api_error_in_batch = True
                    if self._structured_rate_limit_is_hard(raw):
                        rate_limit_in_batch = True

                    if (
                        scan_started
                        and self._reader.is_response_complete(raw)
                    ):
                        if self._pending_steer is not None:
                            terminal_deferred = True
                            self._active_turn_process = None
                            if (
                                self._pending_steer.delivery_uncertain
                                and not self._pending_steer.enqueued
                            ):
                                uncertain_terminal_in_batch = (
                                    self._pending_steer
                                )
                        elif (
                            steer_followup_prompt is not None
                            and not steer_followup_started
                        ):
                            terminal_deferred = True
                            self._active_turn_process = None
                        else:
                            batch_response_complete = True
                            if (
                                self._unsettled_steer_process
                                is turn_process
                            ):
                                self._unsettled_steer_process = None
                            self._active_turn_process = None
                            self._active_turn_owner = None

                if (
                    uncertain_terminal_in_batch is not None
                    and self._pending_steer is uncertain_terminal_in_batch
                    and not uncertain_terminal_in_batch.enqueued
                    and not api_error_in_batch
                    and not rate_limit_in_batch
                ):
                    # The original turn reached its durable terminal boundary
                    # without ever recording this stdin update. That boundary
                    # safely releases the quarantine: preserve the healthy PTY
                    # and finish the useful response instead of manufacturing
                    # a process crash solely to make delivery failure definite.
                    self._pending_steer = None
                    if (
                        self._unsettled_steer_process
                        is uncertain_terminal_in_batch.process
                    ):
                        self._unsettled_steer_process = None
                    if not uncertain_terminal_in_batch.acknowledged.done():
                        uncertain_terminal_in_batch.acknowledged.set_result(
                            False
                        )
                    terminal_deferred = False
                    batch_response_complete = True
                    self._active_turn_process = None
                    self._active_turn_owner = None
                    logger.warning(
                        "Session %s: preserving process after an uncertain "
                        "stdin steer reached the original turn boundary "
                        "without acknowledgement",
                        self.session_id,
                    )

                if api_error_in_batch or rate_limit_in_batch:
                    # A provider error later in this same durable batch wins
                    # over enqueue: CC accepted bytes but did not establish a
                    # usable model turn. If cross-turn routing is still live,
                    # reattach the receipt so deactivation stops the exact
                    # process before waking the API with False.
                    failed_pending = (
                        self._pending_steer or enqueued_pending_in_batch
                    )
                    if (
                        failed_pending is not None
                        and not failed_pending.acknowledged.done()
                    ):
                        if self._unsettled_steer_process is not None:
                            self._pending_steer = failed_pending
                        else:
                            failed_pending.acknowledged.set_result(False)
                    self._active_turn_process = None
                elif (
                    enqueued_pending_in_batch is not None
                    and not enqueued_pending_in_batch.acknowledged.done()
                ):
                    # Exact enqueue(content), with no provider failure in the
                    # same batch, is CC's durable acceptance acknowledgement.
                    enqueued_pending_in_batch.acknowledged.set_result(True)

            if messages:
                turn_had_messages = True
                self._last_activity = time.monotonic()
                # Any activity (even another turn's) extends the inactivity
                # deadline: a turn chaining long sub-agent calls must not be
                # cut at an absolute 30min mark — that re-creates the
                # unread-backlog misalignment.
                deadline = time.monotonic() + timeout
            elif (
                time.monotonic() - last_subagent_check
                >= self.config.subagent_check_interval
            ):
                last_subagent_check = time.monotonic()
                if self._tracker.transcripts_grew():
                    # Main JSONL silent but a sync sub-agent's transcript is
                    # growing — the turn is alive, keep waiting.
                    self._last_activity = time.monotonic()
                    deadline = time.monotonic() + timeout
                    # Read transcript updates and emit progress events
                    for update in self._tracker.read_transcript_updates():
                        yield PTYEvent(
                            event_type=EventType.SUBAGENT_PROGRESS,
                            subagent={
                                "tool_use_id": update["tool_use_id"],
                                "summary": update["summary"],
                            },
                            session_id=self._session_id,
                        )

            for raw in messages:
                if self._structured_rate_limit_is_hard(raw):
                    self._rate_limited_turn = True
                if raw.get("isApiErrorMessage"):
                    api_error_turn = True
                if not turn_started:
                    if id(raw) in prompt_echo_ids:
                        turn_started = True
                        confirm_deadline = None  # delivery confirmed
                        if not (
                            batch_response_complete
                            or api_error_in_batch
                            or rate_limit_in_batch
                            or terminal_deferred
                        ):
                            await self._activate_active_turn(
                                turn_owner, turn_process
                            )
                    elif self._queue_operation_matches_prompt(raw, text):
                        # An exact enqueue for this prompt means CC accepted
                        # it behind an in-flight turn.  Unrelated queue
                        # operations (especially child notifications) must
                        # leave the channel confirmation deadline intact.
                        confirm_deadline = None
                for event in self._reader.normalize(
                    raw, include_user_text=not turn_started
                ):
                    if not turn_started:
                        # Tail of a previous/in-flight turn, not our reply
                        event.orphan = True
                    yield event

            if batch_response_complete:
                response_complete = True

            # A timed-out/failed steer can clear the pending receipt after the
            # original terminal was already consumed.  Finish that exact turn
            # once no managed follow-up remains; never leave the poller hung.
            if terminal_deferred and steer_followup_prompt is None:
                async with self._turn_state_lock:
                    if (
                        self._pending_steer is None
                        and self._unsettled_steer_process is None
                    ):
                        response_complete = True
                        self._active_turn_process = None
                        self._active_turn_owner = None

            # An API error aborts the turn server-side: CC writes the error
            # message but never a turn_duration sentinel. End the turn as an
            # error instead of hanging until response_timeout.
            if not response_complete and api_error_turn:
                await self._deactivate_active_turn(turn_owner)
                yield PTYEvent(
                    event_type=EventType.SYSTEM_EVENT,
                    content=(
                        "api_error: turn aborted by API error "
                        "(no turn_duration sentinel follows)"
                    ),
                    is_error=True,
                    session_id=self.session_id,
                )
                break

            if (
                confirm_deadline is not None
                and time.monotonic() > confirm_deadline
            ):
                confirm_deadline = None  # fall back at most once
                logger.warning(
                    "Session %s: no JSONL activity %.0fs after channel "
                    "inject, re-sending prompt via PTY stdin",
                    self.session_id, self.config.inject_confirm_timeout,
                )
                await loop.run_in_executor(
                    None, turn_process.send_prompt, text
                )

            # Structured JSONL signal — always trusted: end the turn so the
            # host can rotate accounts instead of waiting out the timeout.
            if not response_complete and self._rate_limited_turn:
                await self._deactivate_active_turn(turn_owner)
                yield self._rate_limit_event()
                break

            # PTY banner scan (drain loop)。横幅标记也会出现在 TUI 渲染的
            # 对话正文里（tool result 引用本仓库源码、会话讨论 limit ——
            # CCM task 81/82 三账号连环误冻事故），所以单凭横幅不可信：
            # - turn 已有 JSONL 消息在流动 → 误报，清 flag 继续；
            # - turn 零 JSONL 输出（真撞限的签名：API 直接拒绝，什么都
            #   不写）→ 再静默 rate_limit_confirm_quiet 秒才确认。
            if (
                not response_complete
                and self._process
                and getattr(self._process, "rate_limited", False)
            ):
                if turn_had_messages:
                    logger.warning(
                        "Session %s: rate-limit banner matched rendered "
                        "conversation content (turn has JSONL activity) — "
                        "ignoring as false positive",
                        self.session_id,
                    )
                    self._process.clear_rate_limited()
                elif (
                    time.monotonic() - self._last_activity
                    >= self.config.rate_limit_confirm_quiet
                ):
                    await self._deactivate_active_turn(turn_owner)
                    yield self._rate_limit_event()
                    break

            if not response_complete:
                await asyncio.sleep(self.config.jsonl_poll_interval)

        # Turn 正常完成 = 没撞限。误报横幅可能在 turn 末尾才被 drain loop
        # 置位（message loop 内 break，banner 分支没机会跑）——残留 flag 会
        # 毒化下一 turn（开局零 JSONL，静默够久就被误判真撞限）。
        if (
            response_complete
            and self._process
            and getattr(self._process, "rate_limited", False)
        ):
            logger.warning(
                "Session %s: rate-limit banner flag set but turn completed "
                "normally — clearing as false positive",
                self.session_id,
            )
            self._process.clear_rate_limited()

        if not response_complete and time.monotonic() >= deadline:
            await self._deactivate_active_turn(turn_owner)
            yield PTYEvent(
                event_type=EventType.SYSTEM_EVENT,
                content=f"Response timed out after {timeout}s",
                is_error=True,
                session_id=self.session_id,
            )

        await asyncio.sleep(self.config.post_response_wait)

    async def _idle_watcher(self) -> None:
        """Consume JSONL written outside any send_prompt turn.

        The harness wakes a session on its own when background sub-agents
        (Monitor, background tasks) emit notifications: CC runs full turns
        with no consumer attached. Without this watcher those events pile up
        unread and the next send_prompt mistakes them for its own reply
        (task-87 off-by-one). Events are forwarded to on_autonomous_event
        flagged autonomous=True; reads also keep idle_seconds honest.
        """
        while True:
            try:
                await asyncio.sleep(self.config.idle_poll_interval)
                if (
                    not self._started
                    or self._reader is None
                    or self._send_lock.locked()  # send_prompt owns the reader
                ):
                    continue
                async with self._turn_state_lock:
                    async with self._reader_lock:
                        if self._send_lock.locked():
                            continue
                        messages = await self._read_with_prefetch_locked()
                if not messages:
                    if self._tracker.transcripts_grew():
                        self._last_activity = time.monotonic()
                    continue
                self._last_activity = time.monotonic()
                cb = self.on_autonomous_event
                for raw in messages:
                    for event in self._reader.normalize(
                        raw, include_user_text=True
                    ):
                        event.autonomous = True
                        if cb is not None:
                            try:
                                await cb(event)
                            except Exception:
                                logger.exception(
                                    "Session %s: autonomous event callback "
                                    "failed",
                                    self.session_id,
                                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Session %s: idle watcher iteration failed",
                    self.session_id,
                )

    async def send_interrupt(self) -> None:
        async with self._turn_state_lock:
            turn_owner = self._active_turn_owner
        if turn_owner is not None:
            await self._deactivate_active_turn(turn_owner)
        process = self._process
        if process and process.is_alive:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, process.send_interrupt)

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

    async def migrate_session(self, new_config_dir: str) -> None:
        """Migrate session JSONL to a new config_dir via hardlink, then restart."""
        old_jsonl = self.jsonl_path
        if not old_jsonl or not os.path.exists(old_jsonl):
            raise SessionError(f"No JSONL file to migrate: {old_jsonl}")

        old_config = self.config.config_dir or os.path.expanduser("~/.claude")
        rel = os.path.relpath(old_jsonl, old_config)
        new_jsonl = os.path.join(new_config_dir, rel)

        os.makedirs(os.path.dirname(new_jsonl), exist_ok=True)
        if not os.path.exists(new_jsonl):
            os.link(old_jsonl, new_jsonl)

        saved_session_id = self._session_id
        await self.stop()
        self.config = dataclasses.replace(self.config, config_dir=new_config_dir)
        self._session_id = saved_session_id
        self._restart_count += 1
        await self.start()

    async def _auto_resume(self, prompt: str | None = None) -> None:
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

        await self.start(initial_prompt=prompt)

    def _on_process_death(self, proc: PTYProcess) -> None:
        logger.warning(
            "Session %s process died (pid=%s)", self.session_id, proc.pid
        )

    async def stop(self) -> None:
        if self._idle_watcher_task is not None:
            self._idle_watcher_task.cancel()
            self._idle_watcher_task = None
        if self._bridge and self.session_id:
            self._bridge.unregister_session(self.session_id)
        async with self._turn_state_lock:
            self._active_turn_process = None
            self._active_turn_owner = None
            pending, self._pending_steer = self._pending_steer, None
            self._unsettled_steer_process = None
            self._prefetched_messages.clear()
        process = self._process
        try:
            if process:
                await self._settle_blocking_input(process.stop)
        finally:
            # Never report an injection failure until its exact process has
            # been reaped (or the stop attempt itself has failed visibly).
            if pending is not None and not pending.acknowledged.done():
                pending.acknowledged.set_result(False)
            self._started = False
        logger.info("Session %s stopped", self.session_id)
