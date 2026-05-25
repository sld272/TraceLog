"""SOUL-specific memory file service."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from core import db

SOUL_MEMORIES_DIR = db.WORKSPACE_DIR / "soul_memories"


def default_soul_memory(name: str) -> str:
    now = datetime.now().astimezone().isoformat()
    return f"""---
schema: tracelog/soul_memory.md@v1
soul: {name}
updated_at: {now}
---

# {name}的相处记忆

## 对用户的理解
（暂无）

## 我们之间的互动约定
（暂无）

## 私聊沉淀
（暂无）
"""


def soul_memory_path(name: str) -> Path:
    _validate_memory_name(name)
    return SOUL_MEMORIES_DIR / f"{name}.md"


def read_soul_memory(name: str) -> str:
    path = soul_memory_path(name)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_soul_memory(
    name: str,
    content: str,
    source: str = "user",
    patch: dict | None = None,
) -> None:
    """Overwrite one SOUL memory file and record a revision."""
    _ensure_soul_exists(name)
    if not isinstance(content, str):
        raise ValueError("SOUL 记忆内容必须是字符串")

    SOUL_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(soul_memory_path(name), content)
    db.execute(
        """
        INSERT INTO soul_memory_revisions(soul_name, snapshot, patch, source, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            name,
            content,
            json.dumps(patch or {"op": "overwrite_soul_memory"}, ensure_ascii=False),
            source,
            db.now_ts(),
        ),
    )


def _ensure_soul_exists(name: str) -> None:
    _validate_memory_name(name)
    row = db.query_one("SELECT 1 FROM souls WHERE name = ?", (name,))
    if row is None:
        raise ValueError(f"SOUL 不存在：{name}")


def _validate_memory_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("SOUL 名称不能为空")
    if name != name.strip():
        raise ValueError("SOUL 名称不能包含首尾空白")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("SOUL 名称不能包含路径分隔符或路径穿越")
    if any(ord(char) < 32 for char in name):
        raise ValueError("SOUL 名称不能包含控制字符")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
