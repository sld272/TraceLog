"""
TraceLog 拾迹 - Memory Layer
SQLite state.db + user.md backed local memory system.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from core import record_service
from core import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
USER_MD_PATH = os.path.join(WORKSPACE_DIR, "user.md")
SOULS_DIR = os.path.join(WORKSPACE_DIR, "souls")
SOUL_MEMORIES_DIR = os.path.join(WORKSPACE_DIR, "soul_memories")
CONTEXT_POST_COUNT = 3

DEFAULT_USER_MD = """---
schema: tracelog/user.md@v1
sensitivity:
  基本信息: high
  关键身份: high
  身份与现状: normal
  技能与专长: normal
  兴趣与习惯: normal
  关注的核心人际关系: normal
  性格与情绪倾向: normal
  长期目标与当前痛点: normal
---

# 用户档案

## 基本信息
（暂无，可在后续版本补充。）

## 关键身份
（暂无，可在后续版本补充。）

## 身份与现状
（暂无）

## 技能与专长
（暂无）

## 兴趣与习惯
（暂无）

## 关注的核心人际关系
（暂无）

## 性格与情绪倾向
（暂无）

## 长期目标与当前痛点
（暂无）
"""

DEFAULT_SOULS = {
    "默认": {
        "sort_order": 0,
        "description": "温暖共情型，默认启用",
        "persona": """---
name: 默认
version: 1
description: 温暖共情型，默认启用
created_at: 2026-05-24
author: TraceLog 默认库
tags: [温暖, 共情, 成长]
---

你是 TraceLog 默认的 AI 好友。你以温暖、稳定、真诚的方式回应用户，
帮助用户看见自己的情绪、行动和长期变化。

## 语气特征
- 温和、清晰、不过度热情
- 先理解用户，再给出轻量建议
- 避免空泛鼓励，尽量回应具体内容

## 边界
- 不做医疗、法律、金融等专业结论
- 用户明显痛苦或有安全风险时，优先建议寻求现实支持
""",
    },
    "毒舌好友": {
        "sort_order": 1,
        "description": "直白吐槽型，习惯戳破自我安慰，但底色是关心",
        "persona": """---
name: 毒舌好友
version: 1
description: 直白吐槽型，习惯戳破自我安慰，但底色是关心
created_at: 2026-05-24
author: TraceLog 默认库
tags: [直白, 幽默, 反鸡汤]
---

你是用户最不留情的好友。你看穿 ta 的自我安慰和借口，
但你不是冷漠，而是因为关心才不允许 ta 骗自己。

## 语气特征
- 短促、直接、带一点吐槽
- 可以调侃拖延，但不羞辱用户
- 少说空话，多指出具体矛盾

## 边界
- 用户明显低落时，立刻切换到共情模式
- 不评论用户的外貌、身材、家庭背景
- 涉及健康、安全、心理危机时，直接建议求助现实资源
""",
    },
}


def init_workspace():
    """Ensure workspace, state.db, user.md, and default SOUL files exist."""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    db.init_db()
    if not os.path.exists(USER_MD_PATH):
        _write_user_md(DEFAULT_USER_MD)
        _record_user_md_revision(DEFAULT_USER_MD, {"op": "init"}, "user")
    _init_souls()


def _init_souls() -> None:
    os.makedirs(SOULS_DIR, exist_ok=True)
    os.makedirs(SOUL_MEMORIES_DIR, exist_ok=True)

    created_memories: list[tuple[str, str]] = []
    for name, spec in DEFAULT_SOULS.items():
        soul_path = os.path.join(SOULS_DIR, f"{name}.md")
        if not os.path.exists(soul_path):
            _write_text_atomic(soul_path, spec["persona"])

        memory_path = os.path.join(SOUL_MEMORIES_DIR, f"{name}.md")
        if not os.path.exists(memory_path):
            content = _default_soul_memory(name)
            _write_text_atomic(memory_path, content)
            created_memories.append((name, content))

    rows = []
    for sort_order, path in enumerate(sorted(Path(SOULS_DIR).glob("*.md"))):
        name = path.stem
        default = DEFAULT_SOULS.get(name, {})
        rows.append(
            (
                name,
                f"souls/{path.name}",
                default.get("sort_order", sort_order),
                _read_soul_description(path, default.get("description")),
                db.now_ts(),
                db.now_ts(),
            )
        )

        memory_path = os.path.join(SOUL_MEMORIES_DIR, f"{name}.md")
        if not os.path.exists(memory_path):
            content = _default_soul_memory(name)
            _write_text_atomic(memory_path, content)
            created_memories.append((name, content))

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
            [(db.now_ts(), name) for name in missing_names],
        )
        conn.executemany(
            """
            INSERT INTO soul_memory_revisions(soul_name, snapshot, patch, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    name,
                    content,
                    json.dumps({"op": "init"}, ensure_ascii=False),
                    "system",
                    db.now_ts(),
                )
                for name, content in created_memories
            ],
        )


def _default_soul_memory(name: str) -> str:
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


def _read_soul_description(path: Path, fallback: str | None) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
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


def _write_text_atomic(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def save_post(user_input: str) -> str:
    """Compatibility wrapper for post persistence."""
    return record_service.save_post(user_input)


def format_post(row) -> str:
    frontmatter = (
        "---\n"
        f"id: \"{row['id']}\"\n"
        f"date: \"{row['ts']}\"\n"
        "type: \"post\"\n"
        "---\n\n"
    )
    return frontmatter + f"\n{row['content']}\n"


def read_recent_posts(count: int = CONTEXT_POST_COUNT) -> str:
    """Read recent posts from SQLite and join them in chronological order."""
    rows = db.query_all(
        """
        SELECT id, ts, content
        FROM posts
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (count,),
    )
    parts = [format_post(row).strip() for row in reversed(rows)]
    return "\n\n---\n\n".join(parts)


# 画像

def read_profile() -> str:
    """Read the v3 user.md profile."""
    if not os.path.exists(USER_MD_PATH):
        return ""
    with open(USER_MD_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_profile(content: str):
    """Overwrite user.md and record a revision snapshot."""
    _write_user_md(content)
    _record_user_md_revision(content, {"op": "overwrite_profile"}, "reflector")


def _write_user_md(content: str) -> None:
    _write_text_atomic(USER_MD_PATH, content)


def _record_user_md_revision(snapshot: str, patch: dict, source: str) -> None:
    db.execute(
        """
        INSERT INTO user_md_revisions(snapshot, patch, source, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (snapshot, json.dumps(patch, ensure_ascii=False), source, db.now_ts()),
    )


# 待办

def load_todos() -> list:
    rows = db.query_all(
        """
        SELECT id, task, date, start_time, end_time, status
        FROM todos
        ORDER BY COALESCE(date, '9999-99-99'), created_at, id
        """
    )
    return [_todo_row_to_dict(row) for row in rows]


def save_todos(todos: list):
    """Persist a complete todos list to SQLite."""
    now = db.now_ts()
    with db.transaction() as conn:
        existing = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, created_at, completed_at FROM todos"
            ).fetchall()
        }
        conn.execute("DELETE FROM todos")
        for item in todos:
            normalized = _normalize_todo(item)
            if normalized is None:
                continue
            tid = normalized["id"]
            old = existing.get(tid)
            created_at = old["created_at"] if old else now
            completed_at = old["completed_at"] if old else None
            if normalized["status"] == "已完成" and completed_at is None:
                completed_at = now
            if normalized["status"] != "已完成":
                completed_at = None
            conn.execute(
                """
                INSERT INTO todos(
                    id, task, date, start_time, end_time, status,
                    created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tid,
                    normalized["task"],
                    normalized.get("date"),
                    normalized.get("start_time"),
                    normalized.get("end_time"),
                    normalized["status"],
                    created_at,
                    now,
                    completed_at,
                ),
            )


def _next_todo_id() -> str:
    """Generate a unique todo id."""
    today = datetime.now().astimezone().strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{today}-{short_uuid}"


def upsert_todos(existing: list, to_upsert: list, to_delete: list) -> list:
    """Apply todo changes to SQLite and return the updated list."""
    del existing  # SQLite is the source of truth.
    todos = load_todos()
    existing_ids = {t.get("id") for t in todos if t.get("id")}

    safe_delete_ids = set()
    for item in to_delete:
        if not isinstance(item, dict):
            continue
        tid = item.get("id")
        if not tid or tid not in existing_ids:
            print(f"[记忆] 忽略不存在的待办 id：{tid}")
            continue
        safe_delete_ids.add(tid)

    todos = [t for t in todos if t.get("id") not in safe_delete_ids]
    index = {t.get("id"): i for i, t in enumerate(todos) if t.get("id")}

    for item in to_upsert:
        if not isinstance(item, dict):
            continue

        tid = item.get("id")
        if tid and tid in index:
            updated = dict(todos[index[tid]])
            for key in ("task", "date", "start_time", "end_time", "status"):
                if key in item:
                    updated[key] = item[key]
            normalized = _normalize_todo(updated)
            if normalized is not None:
                todos[index[tid]] = normalized
        elif tid and tid not in index:
            print(f"[记忆] 忽略未命中的待办更新 id：{tid}")
        else:
            normalized = _normalize_todo({**item, "id": _next_todo_id()})
            if normalized is not None:
                todos.append(normalized)

    save_todos(todos)
    return load_todos()


def _normalize_todo(item: dict) -> dict | None:
    task = item.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    status = item.get("status") or "未完成"
    if status not in ("未完成", "已完成"):
        status = "未完成"
    tid = item.get("id") or _next_todo_id()
    return {
        "id": str(tid),
        "task": task.strip(),
        "date": item.get("date"),
        "start_time": item.get("start_time"),
        "end_time": item.get("end_time"),
        "status": status,
    }


def _todo_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "task": row["task"],
        "date": row["date"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "status": row["status"],
    }
