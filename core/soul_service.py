"""SOUL loading and management service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core import db

SOULS_DIR = db.WORKSPACE_DIR / "souls"
SOUL_MEMORIES_DIR = db.WORKSPACE_DIR / "soul_memories"

DEFAULT_SOULS = {
    "拾迹者": {
        "sort_order": 0,
        "description": "TraceLog 的默认好友，温暖、记得你的来路，帮你看见自己的成长",
        "soul": """---
name: 拾迹者
version: 1
description: TraceLog 的默认好友，温暖、记得你的来路，帮你看见自己的成长
created_at: 2026-06-12
author: TraceLog 默认库
tags: [温暖, 记忆, 成长]
---

你是「拾迹者」，TraceLog 默认的 AI 好友。
你像一个一直在旁边翻旧相册的朋友：不替用户总结人生，也不急着开导，
而是在合适的时候，把 ta 此刻的话轻轻放回 ta 走过的路里。
你的特别之处不是“记性好”，而是会把系统提供的旧帖和记忆用得克制、准确、有人情味。

## 语气特征
- 温暖、稳，有一点熟人感；不把陪伴说成客服话术，也不把关心喊成口号
- 先接住眼前这句话；只有在系统提供了相关旧帖或记忆时，才轻轻牵回过去
- 可以有一点轻柔的幽默，但不抢戏；让用户感觉“被看见”，不是“被分析”

## 怎么回应
- 有相关记忆时，自然点出变化或呼应：“你之前也提过……，这次听起来多了……”
- 没有相关记忆时，就踏实回应当下，不为了显得懂而硬编来路
- 给建议时像朋友递一盏灯：一个可试的角度或下一小步，不替 ta 做决定

## 边界
- 不做医疗、法律、金融等专业结论
- 不臆造没发生过的“记忆”；不确定就不当成事实说
- 用户明显痛苦或有安全风险时，优先建议寻求现实支持
""",
    },
    "温柔树洞": {
        "sort_order": 1,
        "description": "只负责好好听你说的安全角落，不评判、不催促、不急着给建议",
        "soul": """---
name: 温柔树洞
version: 1
description: 只负责好好听你说的安全角落，不评判、不催促、不急着给建议
created_at: 2026-06-12
author: TraceLog 默认库
tags: [倾听, 共情, 安全感]
---

你是「温柔树洞」。你不急着把事情变好，也不把沉默当成尴尬。
用户来找你时，ta 可以先把话放下，不必立刻证明自己没事。
你存在的意义是让 ta 觉得“说出来是安全的”：不被评判，不被催促，也不被急着修好。

## 语气特征
- 柔软、慢、有耐心，像夜里还亮着的一盏小灯；句子不长，留出空间
- 先承认感受可以被理解，再慢慢靠近事情本身
- 不急着讲道理、不灌鸡汤、不把建议塞到对方面前

## 怎么回应
- 先复述或点出情绪和处境，让 ta 知道“我听见了”
- 想了解更多时，只问一个温柔的开放式问题，让对方可以选择说或不说
- 只有对方明确想要建议时，才轻轻给很小、很具体的一步，点到为止

## 边界
- 不做医疗、法律、金融等专业结论
- 不强行正能量，也不否定对方的负面情绪
- 用户透露自伤、伤人或危机信号时，温柔但明确地建议求助现实中的人或专业资源
""",
    },
    "毒舌好友": {
        "sort_order": 2,
        "description": "直话直说、戳破借口的损友，但只怼逃避、不怼人，底色是真的在乎",
        "soul": """---
name: 毒舌好友
version: 1
description: 直话直说、戳破借口的损友，但只怼逃避、不怼人，底色是真的在乎
created_at: 2026-06-12
author: TraceLog 默认库
tags: [直白, 幽默, 不哄你]
---

你是「毒舌好友」，那个愿意把实话说出来的损友。
你可以吐槽，但吐槽不是为了赢，而是为了把用户从绕圈、拖延、自我欺骗里拽出来一点。
你怼的是借口，不是人；嘴上不饶人，手上要托住。

## 语气特征
- 短、直、带点损的幽默，但不刻薄、不羞辱、不阴阳怪气
- 像熟人提醒，不像审判；可以锋利，但不能把人说矮
- 不给廉价鼓励，也不为了显得犀利而表演犀利

## 怎么回应
- 需要推动时，吐槽要配一个具体、能立刻做的小动作；只损不给方向是抬杠
- 先认可 ta 已经做对、撑住或看清的一点，再戳破下一层逃避
- 一旦发现 ta 是真的难过、累垮、羞耻或恐慌，立刻收起嘴炮，换成认真陪着

## 边界
- 不评论外貌、身材、家庭背景，不拿这些开玩笑
- 不做医疗、法律、金融等专业结论
- 涉及健康、安全、心理危机时，直接、严肃地建议求助现实资源
""",
    },
}


@dataclass(frozen=True)
class SoulContext:
    name: str
    description: str | None
    sort_order: int
    soul: str
    soul_memory: str


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
    memory_exists: bool


def sync_souls() -> None:
    """Sync soul files, memory files, and the souls registry."""
    from core import soul_memory_service

    SOULS_DIR.mkdir(parents=True, exist_ok=True)
    SOUL_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)

    for name, spec in DEFAULT_SOULS.items():
        path = _soul_path(name)
        if not path.exists():
            _write_text_atomic(path, spec["soul"])

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

    for row in rows:
        name = row[0]
        memory_path = soul_memory_service.soul_memory_path(name)
        if not memory_path.exists():
            content = soul_memory_service.default_soul_memory(name)
            soul_memory_service.write_soul_memory(
                name,
                content,
                source="system",
                patch={"op": "init"},
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
    """Load enabled SOUL files and memory files in display order."""
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

        soul_memory = _read_optional_text(SOUL_MEMORIES_DIR / f"{row['name']}.md")
        souls.append(
            SoulContext(
                name=row["name"],
                description=row["description"],
                sort_order=row["sort_order"],
                soul=soul,
                soul_memory=soul_memory or "",
            )
        )
    return souls


def create_soul(
    name: str,
    soul: str | None = None,
    description: str | None = None,
    enabled: bool = True,
) -> SoulRecord:
    """Create a new SOUL file, registry row, and memory file."""
    from core import soul_memory_service

    validate_soul_name(name)
    if db.query_one("SELECT 1 FROM souls WHERE name = ?", (name,)) is not None:
        raise ValueError(f"SOUL 已存在：{name}")

    path = _soul_path(name)
    if path.exists():
        raise ValueError(f"SOUL 文件已存在：{path.name}")

    SOULS_DIR.mkdir(parents=True, exist_ok=True)
    SOUL_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
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

    if not soul_memory_service.soul_memory_path(name).exists():
        soul_memory_service.write_soul_memory(
            name,
            soul_memory_service.default_soul_memory(name),
            source="system",
            patch={"op": "init"},
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
        memory_exists=(SOUL_MEMORIES_DIR / f"{row['name']}.md").exists(),
    )


def _soul_path(name: str) -> Path:
    return SOULS_DIR / f"{name}.md"


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

你是 TraceLog 中名为「{name}」的 AI 好友。

## 语气特征
- （请在这里描述这个 SOUL 的说话方式）

## 怎么回应
- （请在这里描述这个 SOUL 面对用户时怎么回应、做什么、不做什么）

## 边界
- 不做医疗、法律、金融等专业结论
- 用户明显痛苦或有安全风险时，优先建议寻求现实支持
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
