from dataclasses import dataclass


@dataclass
class PTYConfig:
    claude_binary: str = "claude"
    dangerously_skip_permissions: bool = True
    default_model: str | None = None
    default_effort: str | None = None

    terminal_rows: int = 50
    terminal_cols: int = 200
    drain_interval: float = 0.05
    drain_read_size: int = 65536

    startup_wait: float = 8.0
    post_response_wait: float = 3.0
    response_timeout: float = 1800.0
    jsonl_poll_interval: float = 0.3

    max_sessions: int = 20
    idle_timeout: float = 300.0

    max_restart_attempts: int = 3
    restart_backoff_base: float = 2.0

    config_dir: str | None = None

    env_overrides: dict[str, str] | None = None
    disallowed_tools: list[str] | None = None
    mcp_config_path: str | None = None
