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


def build_clean_env(config: PTYConfig) -> dict[str, str]:
    env = os.environ.copy()

    for key in list(env):
        upper = key.upper()
        if any(p in upper for p in _CLEAN_PATTERNS):
            del env[key]

    env.update(_FORCE_SET)

    if config.config_dir:
        env["CLAUDE_CONFIG_DIR"] = config.config_dir

    return env
