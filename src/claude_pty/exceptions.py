class ClaudePTYError(Exception):
    """Base exception for claude-pty."""


class PTYSpawnError(ClaudePTYError):
    """Failed to spawn PTY process."""


class PTYDeadError(ClaudePTYError):
    """Operation attempted on a dead PTY process."""


class SessionError(ClaudePTYError):
    """Session-level error."""


class PoolExhaustedError(ClaudePTYError):
    """All sessions are active, cannot create new one."""
