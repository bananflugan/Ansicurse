"""Persistent state: run logs and remembered run options, kept in ~/.local/ansicurse."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

KEEP_DAYS = 30
"""Run logs and run folders older than this are deleted when Ansicurse starts."""


def data_dir() -> Path:
    return Path.home() / ".local" / "ansicurse"


def runs_dir() -> Path:
    """ansible-runner's working folders, one per run."""
    return data_dir() / "runs"


def logs_dir() -> Path:
    """One plain-text log per run, named like its folder in runs/."""
    return data_dir() / "logs"


def delete_old_runs(keep_days: int = KEEP_DAYS) -> None:
    """Delete logs and run folders last written more than keep_days ago."""
    cutoff = time.time() - keep_days * 24 * 60 * 60
    for path in _entries(logs_dir()):
        try:
            # Never follow a symlink out of our own folders.
            if path.suffix == ".log" and not path.is_symlink() and path.is_file() and _older(path, cutoff):
                path.unlink()
        except OSError:
            pass  # try again next start
    for path in _entries(runs_dir()):
        try:
            if not path.is_symlink() and path.is_dir() and _older(path, cutoff):
                shutil.rmtree(path)
        except OSError:
            pass


def _entries(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def _older(path: Path, cutoff: float) -> bool:
    return path.stat().st_mtime < cutoff


def _options_file() -> Path:
    return data_dir() / "options.json"


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
