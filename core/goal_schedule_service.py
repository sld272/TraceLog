"""Goal-to-schedule links, expectations, and progress."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
import json
from typing import Any

from core import db
from core.system_timezone import SYSTEM_TIMEZONE

LOCAL_TIMEZONE = SYSTEM_TIMEZONE


class GoalNotFoundError(LookupError):
    """Raised when a goal link operation targets an unknown goal."""


class ScheduleEventNotFoundError(LookupError):
    """Raised when a goal link operation targets an unknown cached event."""


def link(goal_id: str, event_id: str, *, conn: Any | None = None) -> dict[str, Any]:
    """Idempotently link an existing goal and cached schedule event."""
    def _insert(connection: Any) -> dict[str, Any]:
        if connection.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone() is None:
            raise GoalNotFoundError("goal not found")
        if connection.execute("SELECT 1 FROM schedule_events WHERE id = ?", (event_id,)).fetchone() is None:
            raise ScheduleEventNotFoundError("schedule event not found")
        connection.execute(
            """
            INSERT OR IGNORE INTO goal_schedule_links(goal_id, event_id, created_at)
            VALUES (?, ?, ?)
            """,
            (goal_id, event_id, db.now_ts()),
        )
        row = connection.execute(
            """
            SELECT goal_id, event_id, created_at
            FROM goal_schedule_links
            WHERE goal_id = ? AND event_id = ?
            """,
            (goal_id, event_id),
        ).fetchone()
        return dict(row)

    if conn is not None:
        return _insert(conn)
    with db.transaction() as owned:
        return _insert(owned)


def unlink(goal_id: str, event_id: str) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM goal_schedule_links WHERE goal_id = ? AND event_id = ?",
            (goal_id, event_id),
        )
        return cursor.rowcount > 0


def links_for_goal(goal_id: str) -> list[dict[str, Any]]:
    rows = db.query_all(
        """
        SELECT e.*, account.provider AS provider
        FROM goal_schedule_links AS link
        JOIN schedule_events AS e ON e.id = link.event_id
        LEFT JOIN calendar_accounts AS account ON account.id = e.account_id
        WHERE link.goal_id = ? AND e.is_cancelled = 0
        ORDER BY e.start_ts, e.end_ts, e.id
        """,
        (goal_id,),
    )
    events = [_event_from_row(row) for row in rows]
    links = links_for_events([str(event["id"]) for event in events])
    for event in events:
        event["goal_links"] = links.get(str(event["id"]), [])
    return events


def links_for_events(event_ids: Sequence[str]) -> dict[str, list[dict[str, str]]]:
    unique_ids = list(dict.fromkeys(str(event_id) for event_id in event_ids))
    result: dict[str, list[dict[str, str]]] = {event_id: [] for event_id in unique_ids}
    if not unique_ids:
        return result
    placeholders = ", ".join("?" for _ in unique_ids)
    rows = db.query_all(
        f"""
        SELECT link.event_id, goal.id AS goal_id, goal.title AS goal_title
        FROM goal_schedule_links AS link
        JOIN goals AS goal ON goal.id = link.goal_id
        WHERE link.event_id IN ({placeholders})
        ORDER BY link.created_at, goal.id
        """,
        tuple(unique_ids),
    )
    for row in rows:
        result[str(row["event_id"])].append(
            {"goal_id": str(row["goal_id"]), "goal_title": str(row["goal_title"])}
        )
    return result


def update_expectation(
    goal_id: str,
    expectation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Replace a goal's weekly schedule expectation, or clear it with ``None``."""
    normalized = _normalize_expectation(expectation)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) if normalized else None
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone() is None:
            raise GoalNotFoundError("goal not found")
        conn.execute(
            "UPDATE goals SET schedule_expectation = ?, updated_at = ? WHERE id = ?",
            (encoded, db.now_ts(), goal_id),
        )
    return normalized


def weekly_progress(
    goal_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Count linked events in the current system-local Monday-based week."""
    goal = db.query_one(
        "SELECT schedule_expectation FROM goals WHERE id = ?",
        (goal_id,),
    )
    if goal is None:
        raise GoalNotFoundError("goal not found")
    local_now = now or datetime.now(LOCAL_TIMEZONE)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=LOCAL_TIMEZONE)
    else:
        local_now = local_now.astimezone(LOCAL_TIMEZONE)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    week_start = datetime.combine(monday, time.min, LOCAL_TIMEZONE)
    week_end = week_start + timedelta(days=7)
    row = db.query_one(
        """
        SELECT COUNT(*) AS event_count
        FROM goal_schedule_links AS link
        JOIN schedule_events AS event ON event.id = link.event_id
        WHERE link.goal_id = ?
          AND event.is_cancelled = 0
          AND event.start_ts >= ?
          AND event.start_ts < ?
        """,
        (goal_id, week_start.timestamp(), week_end.timestamp()),
    )
    current = int(row["event_count"]) if row is not None else 0
    expectation = _decode_expectation(goal["schedule_expectation"])
    target = expectation["target"] if expectation is not None else None
    return {
        "goal_id": goal_id,
        "week_start": monday.isoformat(),
        "week_end": (monday + timedelta(days=6)).isoformat(),
        "current": current,
        "target": target,
        "text": f"{current}/{target}" if target is not None else None,
        "expectation": expectation,
    }


def _event_from_row(row: Any) -> dict[str, Any]:
    event = dict(row)
    account_id = str(event.get("account_id") or "outlook")
    event["account_id"] = account_id
    event["provider"] = str(event.get("provider") or account_id)
    event["all_day"] = bool(event["all_day"])
    event["is_cancelled"] = bool(event["is_cancelled"])
    event["goal_link"] = None
    event["goal_links"] = []
    return event


def _normalize_expectation(expectation: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if expectation is None:
        return None
    period = expectation.get("period")
    target = expectation.get("target")
    label = expectation.get("label")
    if period != "week":
        raise ValueError("period 只支持 week")
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise ValueError("target 必须是正整数")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label 不能为空")
    return {"period": "week", "target": target, "label": label.strip()}


def _decode_expectation(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    try:
        return _normalize_expectation(decoded)
    except ValueError:
        return None
