"""Todo merging helpers for multi-SOUL replies."""

from __future__ import annotations

from typing import TYPE_CHECKING

import memory

if TYPE_CHECKING:
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
