"""Persistent state: run artifacts and remembered run options."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "ansicurse"


def runs_dir() -> Path:
    return state_dir() / "runs"


def _options_file() -> Path:
    return state_dir() / "options.json"


def load_options() -> dict[str, dict[str, Any]]:
    """Last-used run options, keyed by absolute playbook path."""
    try:
        data = json.loads(_options_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_options(options: dict[str, dict[str, Any]]) -> None:
    path = _options_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(options, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
