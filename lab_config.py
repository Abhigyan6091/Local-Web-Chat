"""
lab_config.py — credentials and lab-specific settings
=====================================================
Keeps secrets out of the source tree. Values are read from the environment, or
from a git-ignored `.lab.env` file beside this module, so nothing sensitive is
committed.

    cp .lab.env.example .lab.env     # then edit it

Recognised names: LAB_SSH_HOST, LAB_SSH_USER, LAB_SSH_PASSWORD,
LAB_DB_PASSWORD, LAB_CHAT_SECRET.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_FILE = Path(__file__).parent / ".lab.env"


def _load_env_file() -> None:
    if not _ENV_FILE.exists():
        return
    for raw in _ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _require(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .lab.env.example to .lab.env and fill it in, "
            f"or export {name} in your shell."
        )
    return value


SSH_HOST = _require("LAB_SSH_HOST", "10.1.75.79")
SSH_USER = _require("LAB_SSH_USER", "student")
SSH_PASSWORD = _require("LAB_SSH_PASSWORD")
DB_PASSWORD = _require("LAB_DB_PASSWORD")
CHAT_SECRET = _require("LAB_CHAT_SECRET")
