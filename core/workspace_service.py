"""Workspace initialization orchestration."""

from __future__ import annotations

import os
from pathlib import Path

from core import db, soul_service
from core.cli import config as cli_config


def migrate_workspace_permissions() -> None:
    """Best-effort migration of sensitive workspace paths to owner-only modes."""
    targets = (
        (db.WORKSPACE_DIR, 0o700),
        (db.DB_PATH, 0o600),
        (Path(f"{db.DB_PATH}-wal"), 0o600),
        (Path(f"{db.DB_PATH}-shm"), 0o600),
        (db.WORKSPACE_DIR / "chroma_db", 0o700),
        (Path(cli_config.CONFIG_FILE), 0o600),
    )
    for path, mode in targets:
        try:
            if path.exists():
                os.chmod(path, mode)
        except OSError:
            pass


def init_workspace() -> None:
    """Ensure workspace, state.db, and default SOUL personality files exist."""
    db.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    soul_service.sync_souls()
