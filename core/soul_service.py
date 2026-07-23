"""SOUL loading and management service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core import db
from core.paths import RESOURCE_DIR

SOULS_DIR = db.WORKSPACE_DIR / "souls"
SOUL_TEMPLATES_DIR = RESOURCE_DIR / "resources" / "souls"

DEFAULT_SOULS = {
    "拾迹者": {
        "sort_order": 0,
        "description": "TraceLog 的默认好友，温暖、记得你的来路，帮你看见自己的成长",
    },
    "温柔树洞": {
        "sort_order": 1,
        "description": "只负责好好听你说的安全角落，不评判、不催促、不急着给建议",
    },
    "毒舌好友": {
        "sort_order": 2,
        "description": "直话直说、戳破借口的损友，但只怼逃避、不怼人，底色是真的在乎",
    },
}


@dataclass(frozen=True)
class SoulContext:
    name: str
    description: str | None
    sort_order: int
    soul: str


@dataclass(frozen=True)
class SoulRecord:
    name: str
    file_path: str
    enabled: bool
    sort_order: int
    description: str | None
    created_at: float
    updated_at: float
    soul_exists: bool


def sync_souls() -> None:
    """Sync soul personality files and the souls registry."""
    SOULS_DIR.mkdir(parents=True, exist_ok=True)

    for name, spec in DEFAULT_SOULS.items():
        path = _soul_path(name)
        if not path.exists():
            _write_text_atomic(path, _default_soul_template(name))

    rows = []
    now = db.now_ts()
    for fallback_sort_order, path in enumerate(sorted(SOULS_DIR.glob("*.md"))):
        name = path.stem
        validate_soul_name(name)
        default = DEFAULT_SOULS.get(name, {})
        rows.append(
            (
                name,
                _relative_workspace_path(path),
                default.get("sort_order", fallback_sort_order),
                _read_soul_description(path, default.get("description")),
                now,
                now,
            )
        )

    with db.transaction() as conn:
        conn.executemany(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, description, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                file_path = excluded.file_path,
                description = COALESCE(excluded.description, souls.description),
                updated_at = excluded.updated_at
            """,
            rows,
        )
        existing_names = {row[0] for row in conn.execute("SELECT name FROM souls").fetchall()}
        file_names = {row[0] for row in rows}
        missing_names = existing_names - file_names
        conn.executemany(
            "UPDATE souls SET enabled = 0, updated_at = ? WHERE name = ?",
            [(now, name) for name in missing_names],
        )



def list_souls(enabled_only: bool = False) -> list[SoulRecord]:
    """List SOUL registry records in display order."""
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = db.query_all(
        f"""
        SELECT name, file_path, enabled, sort_order, description, created_at, updated_at
        FROM souls
        {where}
        ORDER BY sort_order, name
        """
    )
    return [_row_to_record(row) for row in rows]


def get_soul(name: str) -> SoulRecord:
    """Return one SOUL registry record."""
    validate_soul_name(name)
    row = db.query_one(
        """
        SELECT name, file_path, enabled, sort_order, description, created_at, updated_at
        FROM souls
        WHERE name = ?
        """,
        (name,),
    )
    if row is None:
        raise ValueError(f"SOUL 不存在：{name}")
    return _row_to_record(row)


def list_enabled_souls() -> list[SoulContext]:
    """Load enabled SOUL personality files in display order."""
    rows = db.query_all(
        """
        SELECT name, file_path, sort_order, description
        FROM souls
        WHERE enabled = 1
        ORDER BY sort_order, name
        """
    )

    souls: list[SoulContext] = []
    for row in rows:
        soul_path = db.WORKSPACE_DIR / row["file_path"]
        soul = _read_optional_text(soul_path)
        if soul is None:
            continue

        souls.append(
            SoulContext(
                name=row["name"],
                description=row["description"],
                sort_order=row["sort_order"],
                soul=soul,
            )
        )
    return souls


def read_soul_content(name: str) -> str:
    """Return the Markdown content of one SOUL personality file."""
    record = get_soul(name)
    content = _read_optional_text(db.WORKSPACE_DIR / record.file_path)
    if content is None:
        raise ValueError(f"SOUL 人格文件不存在：{name}")
    return content


def create_soul(
    name: str,
    soul: str | None = None,
    description: str | None = None,
    enabled: bool = True,
) -> SoulRecord:
    """Create a new SOUL personality file and registry row."""
    validate_soul_name(name)
    if db.query_one("SELECT 1 FROM souls WHERE name = ?", (name,)) is not None:
        raise ValueError(f"SOUL 已存在：{name}")

    path = _soul_path(name)
    if path.exists():
        raise ValueError(f"SOUL 文件已存在：{path.name}")

    SOULS_DIR.mkdir(parents=True, exist_ok=True)
    body = soul if soul is not None else _new_soul(name, description)
    _write_text_atomic(path, body)

    now = db.now_ts()
    sort_order = _next_sort_order()
    effective_description = _read_soul_description(path, description)
    db.execute(
        """
        INSERT INTO souls(name, file_path, enabled, sort_order, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            _relative_workspace_path(path),
            1 if enabled else 0,
            sort_order,
            effective_description,
            now,
            now,
        ),
    )

    return get_soul(name)


def update_soul(
    name: str,
    soul: str | None = None,
    description: str | None = None,
) -> SoulRecord:
    """Update a SOUL file and/or registry description."""
    record = get_soul(name)
    path = db.WORKSPACE_DIR / record.file_path
    if soul is not None:
        _write_text_atomic(path, soul)
    effective_description = (
        _read_soul_description(path, description) if soul is not None else description
    )
    if effective_description is not None:
        db.execute(
            """
            UPDATE souls
            SET description = ?, updated_at = ?
            WHERE name = ?
            """,
            (effective_description, db.now_ts(), name),
        )
    elif soul is not None:
        db.execute("UPDATE souls SET updated_at = ? WHERE name = ?", (db.now_ts(), name))
    return get_soul(name)


def enable_soul(name: str) -> SoulRecord:
    """Enable a SOUL for future public post replies."""
    record = get_soul(name)
    if not record.soul_exists:
        raise ValueError(f"SOUL 人格文件不存在，无法启用：{name}")
    db.execute(
        "UPDATE souls SET enabled = 1, updated_at = ? WHERE name = ?",
        (db.now_ts(), name),
    )
    return get_soul(name)


def disable_soul(name: str) -> SoulRecord:
    """Disable a SOUL for future public post replies."""
    get_soul(name)
    db.execute(
        "UPDATE souls SET enabled = 0, updated_at = ? WHERE name = ?",
        (db.now_ts(), name),
    )
    return get_soul(name)


def reorder_souls(names: list[str]) -> list[SoulRecord]:
    """Move the named SOULs to the front in the given order."""
    normalized_names = []
    seen = set()
    for name in names:
        validate_soul_name(name)
        if name in seen:
            raise ValueError(f"SOUL 排序列表重复：{name}")
        seen.add(name)
        normalized_names.append(name)

    existing = list_souls()
    existing_names = {record.name for record in existing}
    missing = [name for name in normalized_names if name not in existing_names]
    if missing:
        raise ValueError(f"SOUL 不存在：{', '.join(missing)}")

    remaining = [record.name for record in existing if record.name not in seen]
    ordered_names = normalized_names + remaining
    now = db.now_ts()
    with db.transaction() as conn:
        conn.executemany(
            "UPDATE souls SET sort_order = ?, updated_at = ? WHERE name = ?",
            [(index, now, name) for index, name in enumerate(ordered_names)],
        )
    return list_souls()


def validate_soul_name(name: str) -> None:
    """Validate a SOUL name that maps directly to <name>.md."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("SOUL 名称不能为空")
    if name != name.strip():
        raise ValueError("SOUL 名称不能包含首尾空白")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("SOUL 名称不能包含路径分隔符或路径穿越")
    if any(ord(char) < 32 for char in name):
        raise ValueError("SOUL 名称不能包含控制字符")


def _row_to_record(row) -> SoulRecord:
    return SoulRecord(
        name=row["name"],
        file_path=row["file_path"],
        enabled=bool(row["enabled"]),
        sort_order=row["sort_order"],
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        soul_exists=(db.WORKSPACE_DIR / row["file_path"]).exists(),
    )


def _soul_path(name: str) -> Path:
    return SOULS_DIR / f"{name}.md"


def _default_soul_template(name: str) -> str:
    return (SOUL_TEMPLATES_DIR / f"{name}.md").read_text(encoding="utf-8")


def _relative_workspace_path(path: Path) -> str:
    return path.relative_to(db.WORKSPACE_DIR).as_posix()


def _next_sort_order() -> int:
    row = db.query_one("SELECT MAX(sort_order) AS max_sort_order FROM souls")
    if row is None or row["max_sort_order"] is None:
        return 0
    return int(row["max_sort_order"]) + 1


def _new_soul(name: str, description: str | None) -> str:
    now = datetime.now().astimezone().date().isoformat()
    clean_description = description or "用户自定义 SOUL"
    return f"""---
name: {name}
version: 1
description: {clean_description}
created_at: {now}
author: TraceLog 用户自定义
tags: []
---

（用两三句话以第三人称介绍「{name}」的整体形象与性格；人称约定：用名字或“她/他”指代人格，「你」一律指用户）

## 语气特征
- （描述「{name}」的说话方式：句子长短、口头禅、情绪表达的浓淡等）

## 怎么回应
- （描述你处于不同情境——闲聊、求助、分享喜悦、情绪低落——时，「{name}」怎么回应）

## 边界
- 不做医疗、法律、金融等专业结论
- 你明显痛苦或有安全风险时，优先建议寻求现实支持
"""


def _read_soul_description(path: Path, fallback: str | None) -> str | None:
    text = _read_optional_text(path)
    if text is None:
        return fallback
    in_frontmatter = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if in_frontmatter and stripped.startswith("description:"):
            return stripped.split(":", 1)[1].strip().strip('"')
    return fallback


def _read_optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
