"""Filesystem paths for app state. FRD B.10."""
from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """Repo root, resolved from this file's location (``<root>/app/paths.py``).

    Used for files the developer edits by hand (``.env``). Runtime state
    (db, logs, pid files, artifacts) still lives under ``state_dir()``
    which is outside the repo so it survives ``git clean`` and keeps
    secrets-free runtime data separate from source.
    """
    return Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    override = os.environ.get("EMRB_STATE_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".claude-equity-momentum"
    base.mkdir(parents=True, exist_ok=True)
    for sub in ("run", "run/commands", "logs", "artifacts"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def env_file() -> Path:
    """``.env`` lives at the project root (``<repo>/.env``), ignored by git.

    Rationale: the user edits this by hand to paste the daily Dhan access
    token. Keeping it next to the repo makes it discoverable in an IDE and
    matches the convention most Python projects follow. ``.gitignore``
    already excludes ``.env``, so there's no accidental-commit risk.
    """
    return project_root() / ".env"


def db_file() -> Path:
    return state_dir() / "state.db"


def pid_file(name: str) -> Path:
    return state_dir() / "run" / f"{name}.pid"


def lock_file(name: str) -> Path:
    """Sentinel file used only to hold an exclusive OS lock. Separate from
    pid_file so the pid file remains freely readable from other processes
    (Windows uses mandatory locking; locking the pid file directly would
    block readers with PermissionError)."""
    return state_dir() / "run" / f"{name}.lock"


def log_file(name: str) -> Path:
    return state_dir() / "logs" / f"{name}.log"


def command_inbox() -> Path:
    return state_dir() / "run" / "commands"


def artifact_file(name: str) -> Path:
    return state_dir() / "artifacts" / name
