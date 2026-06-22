"""Unified pending/accepted/dismissed suggestions for todos and goals."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from typing import Any

from core import db, goal_service

SUGGESTION_KINDS = {"todo", "goal"}
SUGGESTION_STATUSES = {"pending", "accepted", "dismissed"}


def create_suggestion(
    kind: str,
    payload: dict[str, Any],
    evidence_ref: str | None,
    confidence: float = 0.6,
) -> dict[str, Any] | None:
    """Create a pending suggestion unless an equivalent row already exists.

    A dismissed equivalent is a permanent tombstone. Pending/accepted matches
    are also reused so retries and reply reruns cannot create duplicates.
    """
    normalized_payload = _normalize_payload(kind, payload)
    if kind == "goal" and goal_service.has_active_goal_title(normalized_payload["title"]):
        return None
    normalized_ref = _normalize_optional_text(evidence_ref)
    normalized_key = normalized_key_for(kind, normalized_payload, normalized_ref)
    normalized_confidence = _coerce_confidence(confidence)
    with db.immediate_transaction() as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM suggestions
            WHERE normalized_key = ?
            ORDER BY
                CASE status WHEN 'dismissed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                created_at DESC
            LIMIT 1
            """,
            (normalized_key,),
        ).fetchone()
        if existing is not None:
            return None if existing["status"] == "dismissed" else _row_to_dict(existing)

        suggestion_id = _new_suggestion_id()
        now = db.now_ts()
        conn.execute(
            """
            INSERT INTO suggestions(
                id, kind, payload_json, evidence_ref, confidence, status,
                normalized_key, created_at, decided_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL)
            """,
            (
                suggestion_id,
                kind,
                json.dumps(normalized_payload, ensure_ascii=False, sort_keys=True),
                normalized_ref,
                normalized_confidence,
                normalized_key,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _row_to_dict(row)


def get_suggestion(suggestion_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    row = (
        conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if conn is not None
        else db.query_one("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))
    )
    return _row_to_dict(row) if row is not None else None


def list_pending(kind: str | None = None) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = ()
    kind_clause = ""
    if kind is not None:
        _validate_kind(kind)
        kind_clause = " AND kind = ?"
        params = (kind,)
    rows = db.query_all(
        f"""
        SELECT *
        FROM suggestions
        WHERE status = 'pending'{kind_clause}
        ORDER BY confidence DESC, created_at ASC, id ASC
        """,
        params,
    )
    return [_row_to_dict(row) for row in rows]


def accept(suggestion_id: str) -> dict[str, Any]:
    """Accept one pending suggestion and atomically create its target object."""
    with db.immediate_transaction() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            raise ValueError("suggestion 不存在")
        if row["status"] != "pending":
            raise ValueError("suggestion 已处理")
        suggestion = _row_to_dict(row)
        if suggestion["kind"] == "goal":
            payload = suggestion["payload"]
            created = goal_service.create_goal(
                payload["title"],
                payload.get("detail"),
                payload["horizon"],
                source="suggested_accepted",
                focus=bool(payload.get("focus", False)),
                conn=conn,
            )
        else:
            created = _apply_todo_suggestion(
                conn,
                suggestion["payload"],
                suggestion.get("evidence_ref"),
            )
        decided_at = db.now_ts()
        conn.execute(
            "UPDATE suggestions SET status = 'accepted', decided_at = ? WHERE id = ?",
            (decided_at, suggestion_id),
        )
        updated = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return {"suggestion": _row_to_dict(updated), "created": created}


def dismiss(suggestion_id: str) -> dict[str, Any]:
    """Dismiss a suggestion; its normalized key remains as a permanent tombstone."""
    with db.immediate_transaction() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            raise ValueError("suggestion 不存在")
        if row["status"] == "accepted":
            raise ValueError("已采纳的 suggestion 不能忽略")
        if row["status"] == "pending":
            conn.execute(
                "UPDATE suggestions SET status = 'dismissed', decided_at = ? WHERE id = ?",
                (db.now_ts(), suggestion_id),
            )
        updated = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _row_to_dict(updated)


def delete_pending_for_evidence(evidence_ref: str) -> int:
    """Delete pending suggestions tied to one source (e.g. a deleted post).

    Pending rows are removed outright rather than tombstoned, so re-creating
    the same source (e.g. reposting) can surface the suggestions again.
    Accepted/dismissed rows are left untouched.
    """
    normalized_ref = _normalize_optional_text(evidence_ref)
    if not normalized_ref:
        return 0
    with db.immediate_transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM suggestions WHERE evidence_ref = ? AND status = 'pending'",
            (normalized_ref,),
        )
        return cursor.rowcount


def normalized_key_for(kind: str, payload: dict[str, Any], evidence_ref: str | None) -> str:
    _validate_kind(kind)
    source_kind = _evidence_source_kind(evidence_ref)
    if kind == "goal":
        material = {
            "kind": kind,
            "title": _normalize_key_text(payload.get("title")),
            "horizon": payload.get("horizon"),
            "source": source_kind,
        }
    else:
        material = {
            "kind": kind,
            "action": payload.get("action", "create"),
            "todo_id": payload.get("todo_id"),
            "task": _normalize_key_text(payload.get("task")),
            "date": payload.get("date"),
            "start_time": payload.get("start_time"),
            "source": source_kind,
        }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    _validate_kind(kind)
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    if kind == "goal":
        title = payload.get("title")
        horizon = payload.get("horizon")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("goal suggestion title 不能为空")
        if horizon not in goal_service.GOAL_HORIZONS:
            raise ValueError("goal suggestion horizon 只支持：short、long")
        detail = payload.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("goal suggestion detail 必须是字符串或 null")
        return {
            "title": title.strip(),
            "detail": detail.strip() if isinstance(detail, str) and detail.strip() else None,
            "horizon": horizon,
            "focus": bool(payload.get("focus", horizon == "short")),
        }

    action = payload.get("action") or "create"
    if action not in {"create", "update", "delete"}:
        raise ValueError("todo suggestion action 只支持：create、update、delete")
    todo_id = payload.get("todo_id")
    if action in {"update", "delete"} and (not isinstance(todo_id, str) or not todo_id.strip()):
        raise ValueError("todo suggestion update/delete 必须提供 todo_id")
    task = payload.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("todo suggestion task 不能为空")
    status = payload.get("status") or "未完成"
    if status not in {"未完成", "已完成"}:
        status = "未完成"
    normalized: dict[str, Any] = {
        "action": action,
        "todo_id": todo_id.strip() if isinstance(todo_id, str) and todo_id.strip() else None,
        "task": task.strip(),
        "status": status,
    }
    for field in ("date", "start_time", "end_time"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"todo suggestion {field} 必须是字符串或 null")
        normalized[field] = value.strip() if isinstance(value, str) and value.strip() else None
    return normalized


def _apply_todo_suggestion(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    evidence_ref: str | None,
) -> dict[str, Any]:
    action = payload.get("action") or "create"
    if action == "update":
        return _update_todo(conn, payload)
    if action == "delete":
        return _delete_todo(conn, payload)

    todo_id = f"manual-{uuid.uuid4().hex[:12]}"
    now = db.now_ts()
    source_post = _source_post_from_evidence(conn, evidence_ref)
    completed_at = now if payload.get("status") == "已完成" else None
    conn.execute(
        """
        INSERT INTO todos(
            id, task, date, start_time, end_time, status,
            source_post, created_at, updated_at, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            todo_id,
            payload["task"],
            payload.get("date"),
            payload.get("start_time"),
            payload.get("end_time"),
            payload.get("status") or "未完成",
            source_post,
            now,
            now,
            completed_at,
        ),
    )
    row = conn.execute(
        """
        SELECT id, task, date, start_time, end_time, status,
               source_post, created_at, updated_at, completed_at
        FROM todos WHERE id = ?
        """,
        (todo_id,),
    ).fetchone()
    return dict(row)


def _update_todo(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    todo_id = payload["todo_id"]
    current = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if current is None:
        raise ValueError("待更新 todo 不存在")
    now = db.now_ts()
    completed_at = current["completed_at"]
    if payload["status"] == "已完成" and completed_at is None:
        completed_at = now
    if payload["status"] != "已完成":
        completed_at = None
    conn.execute(
        """
        UPDATE todos
        SET task = ?, date = ?, start_time = ?, end_time = ?, status = ?,
            updated_at = ?, completed_at = ?
        WHERE id = ?
        """,
        (
            payload["task"],
            payload.get("date"),
            payload.get("start_time"),
            payload.get("end_time"),
            payload["status"],
            now,
            completed_at,
            todo_id,
        ),
    )
    row = conn.execute(
        """
        SELECT id, task, date, start_time, end_time, status,
               source_post, created_at, updated_at, completed_at
        FROM todos WHERE id = ?
        """,
        (todo_id,),
    ).fetchone()
    return dict(row)


def _delete_todo(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    todo_id = payload["todo_id"]
    row = conn.execute(
        """
        SELECT id, task, date, start_time, end_time, status,
               source_post, created_at, updated_at, completed_at
        FROM todos WHERE id = ?
        """,
        (todo_id,),
    ).fetchone()
    if row is None:
        raise ValueError("待删除 todo 不存在")
    deleted = dict(row)
    deleted["deleted"] = True
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    return deleted


def _source_post_from_evidence(conn: sqlite3.Connection, evidence_ref: str | None) -> str | None:
    if not evidence_ref or not evidence_ref.startswith("post:"):
        return None
    post_id = evidence_ref.split(":", 1)[1].strip()
    if not post_id:
        return None
    exists = conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone()
    return post_id if exists is not None else None


def _validate_kind(kind: Any) -> None:
    if kind not in SUGGESTION_KINDS:
        raise ValueError("kind 只支持：todo、goal")


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.6
    return max(0.0, min(1.0, confidence))


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("evidence_ref 必须是字符串或 null")
    return value.strip() or None


def _normalize_key_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _evidence_source_kind(evidence_ref: str | None) -> str:
    if not evidence_ref:
        return "unknown"
    return evidence_ref.split(":", 1)[0].strip().casefold() or "unknown"


def _new_suggestion_id() -> str:
    stamp = int(time.time() * 1000).to_bytes(6, "big").hex()
    return f"s_{stamp}{secrets.token_hex(5)}"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "payload": payload if isinstance(payload, dict) else {},
        "evidence_ref": row["evidence_ref"],
        "confidence": float(row["confidence"]),
        "status": row["status"],
        "normalized_key": row["normalized_key"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }
