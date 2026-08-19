class ClaudePTYError(Exception):
    """Base exception for claude-pty."""


class PTYSpawnError(ClaudePTYError):
    """Failed to spawn PTY process."""


class PTYDeadError(ClaudePTYError):
    """Operation attempted on a dead PTY process."""


class SessionError(ClaudePTYError):
    """Session-level error."""


class SteerDeliveryUncertainError(SessionError):
    """A complete stdin steer write lacks Claude's acceptance record.

    The caller must not retry automatically: Claude may still absorb the
    already-written input.  The Session keeps the exact turn quarantined until
    a matching queue record or its terminal boundary settles ownership.
    """


class PoolExhaustedError(ClaudePTYError):
    """All sessions are active, cannot create new one."""
