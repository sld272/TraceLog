"""SQLite-backed goal lifecycle and prompt formatting."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
import unicodedata
from typing import Any

from core import db

GOAL_HORIZONS = {"short", "long"}
GOAL_STATUSES = {"active", "done", "abandoned", "paused"}
GOAL_SOURCES = {"user", "suggested_accepted"}
EDITABLE_FIELDS = {"title", "detail", "horizon", "status", "focus"}

FOCUS_WINDOW_DAYS = 30
DAY_SECONDS = 86400.0
GOAL_TOOL_ENABLED_ENV = "GOAL_TOOL_ENABLED"


def goal_tool_enabled() -> bool:
    value = os.environ.get(GOAL_TOOL_ENABLED_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def list_goals(*, status: str | None = None, horizon: str | None = None) -> list[dict[str, Any]]:
    """List goals newest-first, optionally filtered by status and horizon."""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        _validate_status(status)
        clauses.append("status = ?")
        params.append(status)
    if horizon is not None:
        _validate_horizon(horizon)
        clauses.append("horizon = ?")
        params.append(horizon)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query_all(
        f"""
        SELECT id, title, detail, horizon, status, source, focus,
               last_progress_at, created_at, updated_at
        FROM goals
        {where}
        ORDER BY
            CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
            focus DESC,
            COALESCE(last_progress_at, updated_at, created_at) DESC,
            id DESC
        """,
        tuple(params),
    )
    return [_row_to_dict(row) for row in rows]


def list_active_long_term() -> list[dict[str, Any]]:
    return list_goals(status="active", horizon="long")


def list_active_short_term() -> list[dict[str, Any]]:
    return list_goals(status="active", horizon="short")


def list_current_focus(*, now: float | None = None) -> list[dict[str, Any]]:
    """Return active short-term goals still inside the 30-day focus window.

    Stale focus flags are cleared lazily here; the goal itself remains active.
    """
    current = db.now_ts() if now is None else float(now)
    cutoff = current - FOCUS_WINDOW_DAYS * DAY_SECONDS
    with db.immediate_transaction() as conn:
        conn.execute(
            """
            UPDATE goals
            SET focus = 0, updated_at = ?
            WHERE status = 'active'
              AND horizon = 'short'
              AND focus = 1
              AND COALESCE(last_progress_at, updated_at, created_at) < ?
            """,
            (current, cutoff),
        )
        rows = conn.execute(
            """
            SELECT id, title, detail, horizon, status, source, focus,
                   last_progress_at, created_at, updated_at
            FROM goals
            WHERE status = 'active' AND horizon = 'short' AND focus = 1
            ORDER BY COALESCE(last_progress_at, updated_at, created_at) DESC, id DESC
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_goal(goal_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    sql = """
        SELECT id, title, detail, horizon, status, source, focus,
               last_progress_at, created_at, updated_at
        FROM goals
        WHERE id = ?
    """
    row = conn.execute(sql, (goal_id,)).fetchone() if conn is not None else db.query_one(sql, (goal_id,))
    return _row_to_dict(row) if row is not None else None


def create_goal(
    title: str,
    detail: str | None,
    horizon: str,
    *,
    source: str = "user",
    focus: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    normalized = _normalize_create(title, detail, horizon, source, focus)

    def _insert(c: sqlite3.Connection) -> dict[str, Any]:
        goal_id = _new_goal_id()
        now = db.now_ts()
        c.execute(
            """
            INSERT INTO goals(
                id, title, detail, horizon, status, source, focus,
                last_progress_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, ?, ?)
            """,
            (
                goal_id,
                normalized["title"],
                normalized["detail"],
                normalized["horizon"],
                normalized["source"],
                normalized["focus"],
                now,
                now,
            ),
        )
        created = get_goal(goal_id, conn=c)
        if created is None:
            raise RuntimeError("goal insert did not return a row")
        return created

    if conn is not None:
        return _insert(conn)
    with db.transaction() as owned:
        return _insert(owned)


def update_goal(goal_id: str, **fields: Any) -> dict[str, Any] | None:
    existing = get_goal(goal_id)
    if existing is None:
        return None
    normalized = _normalize_updates(fields)
    if normalized:
        assignments = [f"{field} = ?" for field in normalized]
        params = list(normalized.values())
        assignments.append("updated_at = ?")
        params.extend([db.now_ts(), goal_id])
        db.execute(
            f"UPDATE goals SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )
    return get_goal(goal_id)


def set_status(goal_id: str, status: str) -> dict[str, Any] | None:
    return update_goal(goal_id, status=status)


def set_focus(goal_id: str, focus: bool) -> dict[str, Any] | None:
    return update_goal(goal_id, focus=focus)


def mark_progress(goal_id: str, *, at: float | None = None) -> dict[str, Any] | None:
    if get_goal(goal_id) is None:
        return None
    now = db.now_ts() if at is None else float(at)
    db.execute(
        """
        UPDATE goals
        SET last_progress_at = ?, focus = CASE WHEN horizon = 'short' THEN 1 ELSE focus END,
            updated_at = ?
        WHERE id = ?
        """,
        (now, now, goal_id),
    )
    return get_goal(goal_id)


def delete_goal(goal_id: str) -> bool | None:
    if get_goal(goal_id) is None:
        return None
    db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    return True


def format_goal_for_context(goal: dict[str, Any], *, include_status: bool = False) -> str:
    horizon = "长期" if goal.get("horizon") == "long" else "短期"
    focus = "，当前关注" if bool(goal.get("focus")) else ""
    status = f"，{goal.get('status')}" if include_status and goal.get("status") else ""
    detail = str(goal.get("detail") or "").strip()
    detail_text = f"：{detail}" if detail else ""
    return f"- [{goal.get('id') or '?'}] {goal.get('title', '')}{detail_text}（{horizon}{focus}{status}）"


def prompt_sections() -> list[str]:
    """Always-on prompt sections shared by public, comment and chat replies."""
    if not goal_tool_enabled():
        return []
    sections: list[str] = []
    long_term = list_active_long_term()
    if long_term:
        sections.append(
            "# 长期目标\n\n"
            + "\n".join(format_goal_for_context(goal) for goal in long_term)
        )
    current = list_current_focus()
    if current:
        sections.append(
            "# 当前状态\n\n[当前关注]\n"
            + "\n".join(format_goal_for_context(goal) for goal in current)
        )
    return sections


def memory_content_duplicates_active_goal(content: str) -> bool:
    """Whether a memory belief would merely restate an active goal title."""
    if not goal_tool_enabled():
        return False
    content_key = _topic_key(content)
    if not content_key:
        return False
    for goal in list_goals(status="active"):
        title_key = _topic_key(goal["title"])
        if not title_key:
            continue
        if title_key in content_key or content_key in title_key:
            return True
    return False


def _normalize_create(
    title: str,
    detail: str | None,
    horizon: str,
    source: str,
    focus: bool,
) -> dict[str, Any]:
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title 不能为空")
    _validate_horizon(horizon)
    if source not in GOAL_SOURCES:
        raise ValueError("source 只支持：user、suggested_accepted")
    if detail is not None and not isinstance(detail, str):
        raise ValueError("detail 必须是字符串或 null")
    return {
        "title": title.strip(),
        "detail": detail.strip() if isinstance(detail, str) and detail.strip() else None,
        "horizon": horizon,
        "source": source,
        "focus": 1 if bool(focus) else 0,
    }


def _normalize_updates(fields: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field, value in fields.items():
        if field not in EDITABLE_FIELDS:
            continue
        if field == "title":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("title 不能为空")
            normalized[field] = value.strip()
        elif field == "detail":
            if value is not None and not isinstance(value, str):
                raise ValueError("detail 必须是字符串或 null")
            normalized[field] = value.strip() if isinstance(value, str) and value.strip() else None
        elif field == "horizon":
            _validate_horizon(value)
            normalized[field] = value
        elif field == "status":
            _validate_status(value)
            normalized[field] = value
        elif field == "focus":
            if not isinstance(value, bool):
                raise ValueError("focus 必须是布尔值")
            normalized[field] = 1 if value else 0
    return normalized


def _validate_horizon(horizon: Any) -> None:
    if horizon not in GOAL_HORIZONS:
        raise ValueError("horizon 只支持：short、long")


def _validate_status(status: Any) -> None:
    if status not in GOAL_STATUSES:
        raise ValueError("status 只支持：active、done、abandoned、paused")


def _new_goal_id() -> str:
    stamp = int(time.time() * 1000).to_bytes(6, "big").hex()
    return f"g_{stamp}{secrets.token_hex(5)}"


def _topic_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"^(用户|我)(正在|计划|决定|希望|想要|想|要|对)?", "", text)
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "detail": row["detail"],
        "horizon": row["horizon"],
        "status": row["status"],
        "source": row["source"],
        "focus": bool(row["focus"]),
        "last_progress_at": row["last_progress_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
