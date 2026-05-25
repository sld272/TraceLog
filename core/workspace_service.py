"""Workspace initialization orchestration."""

from __future__ import annotations

from core import db, profile_service, soul_service


def init_workspace() -> None:
    """Ensure workspace, state.db, user.md, and default SOUL files exist."""
    db.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    profile_service.init_default_profile()
    soul_service.sync_souls()
