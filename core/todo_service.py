"""Todo merging helpers for multi-SOUL replies."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import memory
from core import db

if TYPE_CHECKING:
    from core.chat_service import ChatReplyResult
    from core.comment_service import CommentReplyResult
    from core.reply_service import SoulReplyResult


def merge_reply_todos(results: list["SoulReplyResult"]) -> tuple[list[dict], list[dict]]:
    """Merge todo changes from successful SOUL replies."""
    upserts: list[dict] = []
    seen_upsert_keys: set[tuple] = set()
    delete_ids: set[str] = set()
    deletes: list[dict] = []

    for result in sorted(results, key=lambda item: (item.sort_order, item.soul_name)):
        if not result.ok:
            continue

        for item in result.todos_to_upsert:
            if not isinstance(item, dict):
                continue
            task = item.get("task")
            if not isinstance(task, str) or not task.strip():
                continue
            key = (task.strip(), item.get("date"), item.get("start_time"))
            if key in seen_upsert_keys:
                continue
            seen_upsert_keys.add(key)
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

        for item in result.todos_to_delete:
            if not isinstance(item, dict):
                continue
            todo_id = item.get("id")
            if not todo_id:
                continue
            todo_id = str(todo_id)
            if todo_id in delete_ids:
                continue
            delete_ids.add(todo_id)
            deletes.append({"id": todo_id})

    return upserts, deletes


def apply_reply_todos(existing_todos: list, results: list["SoulReplyResult"]) -> list:
    """Apply merged todo changes and return the refreshed todo list."""
    to_upsert, to_delete = merge_reply_todos(results)
    if not to_upsert and not to_delete:
        return existing_todos
    todos = memory.upsert_todos(existing_todos, to_upsert, to_delete)
    memory.save_todos(todos)
    return todos


def apply_chat_todos(result: "ChatReplyResult", assistant_message_id: int) -> list[dict]:
    """Apply todo changes from a private chat reply."""
    return _apply_message_todos(result, "source_chat_message", assistant_message_id)


def apply_comment_todos(result: "CommentReplyResult", assistant_message_id: int) -> list[dict]:
    """Apply todo changes from a post comment thread reply."""
    return _apply_message_todos(result, "source_comment_message", assistant_message_id)


def _apply_message_todos(
    result: "ChatReplyResult | CommentReplyResult",
    source_column: str,
    source_message_id: int,
) -> list[dict]:
    if source_column not in {"source_chat_message", "source_comment_message"}:
        raise ValueError("未知待办消息来源列")
    if not result.ok:
        return memory.load_todos()

    to_upsert, to_delete = _merge_chat_todos(result)
    if not to_upsert and not to_delete:
        return memory.load_todos()

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

    with db.transaction() as conn:
        for item in to_delete:
            todo_id = item["id"]
            if todo_id in existing_rows:
                conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))

        for item in to_upsert:
            normalized = _normalize_chat_todo(item)
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
                        {source_column} = ?, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """.format(source_column=source_column),
                    (
                        normalized["task"],
                        normalized.get("date"),
                        normalized.get("start_time"),
                        normalized.get("end_time"),
                        normalized["status"],
                        source_message_id,
                        now,
                        completed_at,
                        todo_id,
                    ),
                )
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
                    {source_column}, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """.format(source_column=source_column),
                (
                    new_id,
                    normalized["task"],
                    normalized.get("date"),
                    normalized.get("start_time"),
                    normalized.get("end_time"),
                    normalized["status"],
                    source_message_id,
                    now,
                    now,
                    completed_at,
                ),
            )

    return memory.load_todos()


def _merge_chat_todos(result: "ChatReplyResult") -> tuple[list[dict], list[dict]]:
    upserts: list[dict] = []
    seen_upsert_keys: set[tuple] = set()
    deletes: list[dict] = []
    delete_ids: set[str] = set()

    for item in result.todos_to_upsert:
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            continue
        key = (task.strip(), item.get("date"), item.get("start_time"))
        if key in seen_upsert_keys:
            continue
        seen_upsert_keys.add(key)
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

    existing_ids = {todo["id"] for todo in memory.load_todos()}
    for item in result.todos_to_delete:
        if not isinstance(item, dict):
            continue
        todo_id = item.get("id")
        if not todo_id:
            continue
        todo_id = str(todo_id)
        if todo_id in delete_ids or todo_id not in existing_ids:
            continue
        delete_ids.add(todo_id)
        deletes.append({"id": todo_id})
    return upserts, deletes


def _normalize_chat_todo(item: dict) -> dict | None:
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
