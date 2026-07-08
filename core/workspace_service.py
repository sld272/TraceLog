"""Workspace initialization orchestration."""

from __future__ import annotations

from core import db, soul_service


def init_workspace() -> None:
    """Ensure workspace, state.db, and default SOUL personality files exist."""
    db.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    soul_service.sync_souls()
