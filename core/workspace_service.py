"""Workspace initialization orchestration."""

from __future__ import annotations

from pathlib import Path

from core import db, file_security, soul_service
from core.cli import config as cli_config


def migrate_workspace_permissions() -> None:
    """Best-effort migration of sensitive workspace paths to owner-only access."""
    targets = (
        db.WORKSPACE_DIR,
        db.DB_PATH,
        Path(f"{db.DB_PATH}-wal"),
        Path(f"{db.DB_PATH}-shm"),
        db.WORKSPACE_DIR / "chroma_db",
        Path(cli_config.CONFIG_FILE),
    )
    for path in targets:
        _make_private_best_effort(path)


def init_workspace() -> None:
    """Ensure workspace, state.db, and default SOUL personality files exist."""
    db.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    _make_private_best_effort(db.WORKSPACE_DIR)
    db.init_db()
    soul_service.sync_souls()


def _make_private_best_effort(path: Path) -> None:
    try:
        if path.exists():
            file_security.make_private(path)
    except OSError:
        pass
