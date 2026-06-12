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
    # Inactivity timeout for a turn: extended whenever JSONL activity (or a
    # sub-agent transcript) advances — NOT an absolute cap, so turns chaining
    # long sub-agent calls aren't cut mid-flight (which would strand unread
    # events and misalign the next turn's reply).
    response_timeout: float = 7200.0
    jsonl_poll_interval: float = 0.3
    # Idle watcher: poll interval for consuming autonomous turns (harness
    # wakes the session on background sub-agent notifications) between
    # send_prompt calls.
    idle_poll_interval: float = 1.0
    # How often to stat sub-agent transcripts as an activity signal while the
    # main JSONL is silent.
    subagent_check_interval: float = 5.0
    # Channel inject has no delivery ACK from CC (HTTP 200 only means the
    # notification reached the channel server). If no JSONL activity appears
    # within this window, the prompt is re-sent via PTY stdin.
    inject_confirm_timeout: float = 15.0
    # PTY banner scan can match rate-limit phrases rendered from conversation
    # content (tool results quoting this repo's source, sessions discussing
    # limits — CCM task 81/82 false-freeze incident). A banner on a turn with
    # zero JSONL output is only confirmed after this many seconds of continued
    # JSONL silence; a real refusal produces no JSONL at all.
    rate_limit_confirm_quiet: float = 15.0

    max_sessions: int = 20
    idle_timeout: float = 300.0

    max_restart_attempts: int = 3
    restart_backoff_base: float = 2.0

    config_dir: str | None = None

    env_overrides: dict[str, str] | None = None
    disallowed_tools: list[str] | None = None
    mcp_config_path: str | None = None
