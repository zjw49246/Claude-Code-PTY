from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import PTYConfig

_CLEAN_PATTERNS = ("CLAUDE", "CLAUDECODE", "AI_AGENT")

_FORCE_SET = {
    "TERM": "xterm-256color",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
}


def claude_json_path(config: PTYConfig) -> str:
    """Path of the ``.claude.json`` the spawned CC will actually read.

    Must stay in lockstep with ``build_clean_env``: CLAUDE_CONFIG_DIR is only
    exported for a non-default config_dir (or an explicit env override), and
    without it CC reads ``~/.claude.json`` in $HOME — not
    ``~/.claude/.claude.json``. Pre-trust writes that pick the wrong file are
    silently ignored by CC, so the trust/MCP dialogs come back and wedge
    headless startups (CCM task 752).
    """
    override = (config.env_overrides or {}).get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.join(override, ".claude.json")
    if config.config_dir:
        default_dir = os.path.expanduser("~/.claude")
        if os.path.realpath(config.config_dir) != os.path.realpath(default_dir):
            return os.path.join(config.config_dir, ".claude.json")
    return os.path.expanduser("~/.claude.json")


def build_clean_env(config: PTYConfig) -> dict[str, str]:
    env = os.environ.copy()

    for key in list(env):
        upper = key.upper()
        if any(p in upper for p in _CLEAN_PATTERNS):
            del env[key]

    env.update(_FORCE_SET)
    env["DISABLE_AUTO_COMPACT"] = "true"

    if config.config_dir:
        default_dir = os.path.expanduser("~/.claude")
        if os.path.realpath(config.config_dir) != os.path.realpath(default_dir):
            env["CLAUDE_CONFIG_DIR"] = config.config_dir

    if config.env_overrides:
        env.update(config.env_overrides)

    return env
