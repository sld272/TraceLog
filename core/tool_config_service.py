"""Optional tool configuration backed by workspace meta."""

from __future__ import annotations

from core import db

SUPPORTED_TOOLS: frozenset[str] = frozenset()
TOOL_META_PREFIX = "tool_enabled:"


def is_tool_enabled(name: str) -> bool:
    """Return whether an optional tool is enabled. Unknown tools are disabled."""
    if name not in SUPPORTED_TOOLS:
        return False
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (_tool_key(name),))
    if row is None:
        return True
    return str(row["value"]).lower() in {"1", "true", "yes", "on", "enabled"}


def set_tool_enabled(name: str, enabled: bool) -> None:
    """Persist an optional tool toggle."""
    if name not in SUPPORTED_TOOLS:
        raise ValueError(f"未知工具：{name}")
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (_tool_key(name), "1" if enabled else "0"),
    )


def _tool_key(name: str) -> str:
    return f"{TOOL_META_PREFIX}{name}"
