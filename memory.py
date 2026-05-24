"""
TraceLog 拾迹 - Memory Layer
SQLite state.db + user.md backed local memory system.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from core import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
USER_MD_PATH = os.path.join(WORKSPACE_DIR, "user.md")
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


def init_workspace():
    """Ensure workspace, state.db, and user.md exist."""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    db.init_db()
    if not os.path.exists(USER_MD_PATH):
        _write_user_md(DEFAULT_USER_MD)
        _record_user_md_revision(DEFAULT_USER_MD, {"op": "init"}, "user")


# 帖子

def _next_post_id() -> str:
    """Generate the next post id in YYYYMMDD-NNN format from SQLite rows."""
    today = datetime.now().astimezone().strftime("%Y%m%d")
    row = db.query_one(
        """
        SELECT id
        FROM posts
        WHERE id LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{today}-%",),
    )
    if row is None:
        return f"{today}-001"
    try:
        seq = int(str(row["id"]).split("-")[1]) + 1
    except (IndexError, ValueError):
        seq = 1
    return f"{today}-{seq:03d}"


def save_post(user_input: str) -> str:
    """Save a post to state.db and return its post_id."""
    now = datetime.now().astimezone()
    iso_time = now.isoformat()
    now_unix = now.timestamp()
    post_id = _next_post_id()
    db.execute(
        """
        INSERT INTO posts(id, ts, content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (post_id, iso_time, user_input, now_unix, now_unix),
    )
    return post_id


def _format_post(row) -> str:
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
    parts = [_format_post(row).strip() for row in reversed(rows)]
    return "\n\n---\n\n".join(parts)


def recent_post_ids(count: int = CONTEXT_POST_COUNT) -> set[str]:
    rows = db.query_all(
        """
        SELECT id
        FROM posts
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (count,),
    )
    return {row["id"] for row in rows}


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
    tmp = USER_MD_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, USER_MD_PATH)


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


# 上下文组装

def read_posts_by_ids(post_ids: list[str]) -> str:
    """Read posts by id from SQLite, skipping missing ids."""
    parts = []
    for pid in post_ids:
        row = db.query_one(
            "SELECT id, ts, content FROM posts WHERE id = ?",
            (pid,),
        )
        if row is not None:
            parts.append(_format_post(row).strip())
    return "\n\n---\n\n".join(parts)


def build_context(relevant_post_ids: list[str] | None = None) -> str:
    """Build profile + recent posts + relevant posts + active todos context."""
    sections = []

    profile = read_profile().strip()
    if profile and profile != DEFAULT_USER_MD.strip():
        sections.append(profile)

    recent_ids = recent_post_ids()
    posts = read_recent_posts()
    if posts:
        sections.append(f"# 近期帖子\n\n{posts}")

    if relevant_post_ids:
        deduped = [pid for pid in relevant_post_ids if pid not in recent_ids]
        if deduped:
            relevant_posts = read_posts_by_ids(deduped)
            if relevant_posts:
                sections.append(f"# 相关帖子\n\n{relevant_posts}")

    pending = [t for t in load_todos() if t.get("status") != "已完成"]
    if pending:
        lines = [_format_todo_for_context(t) for t in pending]
        sections.append("# 待办事项\n\n" + "\n".join(lines))

    return "\n\n---\n\n".join(sections)


def _format_todo_for_context(todo: dict) -> str:
    date_str = todo.get("date") or "待定"
    start = todo.get("start_time")
    end = todo.get("end_time")
    if start and end:
        time_str = f" {start}~{end}"
    elif start:
        time_str = f" {start}"
    else:
        time_str = ""
    return f"- [{todo.get('id', '?')}] {todo['task']}（{date_str}{time_str}）"
