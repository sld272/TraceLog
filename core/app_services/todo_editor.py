"""Small API-facing helpers for manual todo inspection and edits."""

from __future__ import annotations

from typing import Any

from core import db, todo_service

TODO_STATUSES = {"未完成", "已完成"}
EDITABLE_FIELDS = {"task", "date", "start_time", "end_time", "status"}


def list_todos() -> list[dict[str, Any]]:
    return todo_service.load_todos()


def update_todo(todo_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Patch one todo and return its current display row."""
    row = db.query_one("SELECT * FROM todos WHERE id = ?", (todo_id,))
    if row is None:
        return None

    normalized = _normalize_changes(changes)
    if normalized:
        now = db.now_ts()
        completed_at = row["completed_at"]
        if normalized.get("status") == "已完成" and completed_at is None:
            completed_at = now
        if normalized.get("status") == "未完成":
            completed_at = None

        fields = [f"{field} = ?" for field in normalized]
        params = list(normalized.values())
        fields.extend(["updated_at = ?", "completed_at = ?"])
        params.extend([now, completed_at, todo_id])
        db.execute(
            f"UPDATE todos SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )

    return get_todo(todo_id)


def get_todo(todo_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        """
        SELECT id, task, date, start_time, end_time, status
        FROM todos
        WHERE id = ?
        """,
        (todo_id,),
    )
    return dict(row) if row is not None else None


def _normalize_changes(changes: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field, value in changes.items():
        if field not in EDITABLE_FIELDS:
            continue
        if field == "task":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("task 不能为空")
            normalized[field] = value.strip()
            continue
        if field == "status":
            if value not in TODO_STATUSES:
                raise ValueError("status 只支持：未完成、已完成")
            normalized[field] = value
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} 必须是字符串或 null")
        normalized[field] = value.strip() if isinstance(value, str) and value.strip() else None
    return normalized
