"""Resolve TraceLog's packaged resources and writable user data paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "TraceLog"
DATA_DIR_ENV = "TRACELOG_DATA_DIR"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """Return whether the current process is a PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Return the read-only root containing resources shipped with the app."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return PROJECT_ROOT


def data_dir() -> Path:
    """Return the root for config, database, attachments, logs, and token cache."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if not is_frozen():
        return PROJECT_ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / APP_NAME
    raise RuntimeError(f"TraceLog desktop is not packaged for platform {sys.platform!r}")


RESOURCE_DIR = resource_dir()
DATA_DIR = data_dir()
WORKSPACE_DIR = DATA_DIR / "workspace"
CONFIG_FILE = DATA_DIR / "config.json"
SCHEMA_FILE = RESOURCE_DIR / "schema.sql"
FRONTEND_DIST_DIR = RESOURCE_DIR / "frontend" / "dist"
