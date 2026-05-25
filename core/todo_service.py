"""Optional TodoTool for extracting tasks from public posts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from core import db, memory, router, tool_config_service

if TYPE_CHECKING:
    from openai import OpenAI


@dataclass(frozen=True)
class TodoToolResult:
    post_id: str
    applied: bool
    upserted: int
    deleted: int
    skipped: bool
    error: str | None = None


def run_for_post(post_id: str, client: "OpenAI", model: str) -> TodoToolResult:
    """Run TodoTool for one public post when the todo tool is enabled."""
    if not tool_config_service.is_tool_enabled("todo"):
        return TodoToolResult(post_id=post_id, applied=False, upserted=0, deleted=0, skipped=True)

    post = _get_post(post_id)
    if post is None:
        raise ValueError(f"post 不存在：{post_id}")

    data = router.call_todo_tool(
        client=client,
        model=model,
        post=_format_post_for_tool(post),
        active_todos=_format_active_todos(),
    )
    if data is None:
        return TodoToolResult(
            post_id=post_id,
            applied=False,
            upserted=0,
            deleted=0,
            skipped=False,
            error="TodoTool returned invalid JSON",
        )

    upserted, deleted = apply_post_todos(
        post_id,
        data.get("todos_to_upsert", []),
        data.get("todos_to_delete", []),
    )
    return TodoToolResult(
        post_id=post_id,
        applied=bool(upserted or deleted),
        upserted=upserted,
        deleted=deleted,
        skipped=False,
    )


def run_for_post_safely(post_id: str, client: "OpenAI", model: str) -> TodoToolResult:
    """Run TodoTool without interrupting the post/reply/reflection flow."""
    try:
        return run_for_post(post_id, client, model)
    except Exception as exc:
        return TodoToolResult(
            post_id=post_id,
            applied=False,
            upserted=0,
            deleted=0,
            skipped=False,
            error=str(exc),
        )


def apply_post_todos(post_id: str, to_upsert: list, to_delete: list) -> tuple[int, int]:
    """Apply TodoTool output to SQLite todos and record source_post."""
    if _get_post(post_id) is None:
        raise ValueError(f"post 不存在：{post_id}")

    upserts = _merge_upserts(to_upsert)
    deletes = _merge_deletes(to_delete)
    if not upserts and not deletes:
        return 0, 0

    now = db.now_ts()
    existing_rows = {
        row["id"]: row
        for row in db.query_all(
            """
            SELECT id, task, date, start_time, end_time, status, created_at, completed_at
            FROM todos
            """
        )
    }
    existing_keys = {
        (row["task"], row["date"], row["start_time"])
        for row in existing_rows.values()
    }

    upserted = 0
    deleted = 0
    with db.transaction() as conn:
        for item in deletes:
            todo_id = item["id"]
            if todo_id in existing_rows:
                conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
                deleted += 1

        for item in upserts:
            normalized = _normalize_todo(item)
            if normalized is None:
                continue
            todo_id = normalized.get("id")
            if todo_id and todo_id in existing_rows:
                old = existing_rows[todo_id]
                completed_at = old["completed_at"]
                if normalized["status"] == "已完成" and completed_at is None:
                    completed_at = now
                if normalized["status"] != "已完成":
                    completed_at = None
                conn.execute(
                    """
                    UPDATE todos
                    SET task = ?, date = ?, start_time = ?, end_time = ?, status = ?,
                        source_post = ?, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized["task"],
                        normalized.get("date"),
                        normalized.get("start_time"),
                        normalized.get("end_time"),
                        normalized["status"],
                        post_id,
                        now,
                        completed_at,
                        todo_id,
                    ),
                )
                upserted += 1
                continue

            key = (normalized["task"], normalized.get("date"), normalized.get("start_time"))
            if key in existing_keys:
                continue
            existing_keys.add(key)
            new_id = _next_todo_id()
            completed_at = now if normalized["status"] == "已完成" else None
            conn.execute(
                """
                INSERT INTO todos(
                    id, task, date, start_time, end_time, status,
                    source_post, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    normalized["task"],
                    normalized.get("date"),
                    normalized.get("start_time"),
                    normalized.get("end_time"),
                    normalized["status"],
                    post_id,
                    now,
                    now,
                    completed_at,
                ),
            )
            upserted += 1

    return upserted, deleted


def _get_post(post_id: str):
    return db.query_one(
        """
        SELECT id, ts, content
        FROM posts
        WHERE id = ?
        """,
        (post_id,),
    )


def _format_post_for_tool(row) -> str:
    return (
        "---\n"
        f"id: \"{row['id']}\"\n"
        f"date: \"{row['ts']}\"\n"
        "type: \"post\"\n"
        "---\n\n"
        f"{row['content']}"
    )


def _format_active_todos() -> str:
    pending = [todo for todo in memory.load_todos() if todo.get("status") != "已完成"]
    if not pending:
        return "（暂无）"
    lines = []
    for todo in pending:
        date = todo.get("date") or "无日期"
        start_time = todo.get("start_time") or ""
        time_part = f" {start_time}" if start_time else ""
        lines.append(f"- [{todo.get('id')}] {todo.get('task')}（{date}{time_part}，{todo.get('status')}）")
    return "\n".join(lines)


def _merge_upserts(items: list) -> list[dict]:
    upserts: list[dict] = []
    seen_keys: set[tuple] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            continue
        key = (task.strip(), item.get("date"), item.get("start_time"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        upserts.append(
            {
                "id": item.get("id"),
                "task": task.strip(),
                "date": item.get("date"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "status": item.get("status", "未完成"),
            }
        )
    return upserts


def _merge_deletes(items: list) -> list[dict]:
    existing_ids = {todo["id"] for todo in memory.load_todos()}
    deletes: list[dict] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        todo_id = item.get("id")
        if not todo_id:
            continue
        todo_id = str(todo_id)
        if todo_id in seen_ids or todo_id not in existing_ids:
            continue
        seen_ids.add(todo_id)
        deletes.append({"id": todo_id})
    return deletes


def _normalize_todo(item: dict) -> dict | None:
    task = item.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    status = item.get("status") or "未完成"
    if status not in ("未完成", "已完成"):
        status = "未完成"
    return {
        "id": item.get("id"),
        "task": task.strip(),
        "date": item.get("date"),
        "start_time": item.get("start_time"),
        "end_time": item.get("end_time"),
        "status": status,
    }


def _next_todo_id() -> str:
    today = datetime.now().astimezone().strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{today}-{short_uuid}"
