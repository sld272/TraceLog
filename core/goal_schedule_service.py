"""Goal-to-schedule links, expectations, and progress."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
import json
import os
import threading
from typing import Any

from core import db, goal_activity_service, goal_service, logging_service
from core.llm import goal_schedule_router
from core.llm.types import LLMClient
from core.system_timezone import SYSTEM_TIMEZONE

LOCAL_TIMEZONE = SYSTEM_TIMEZONE
GOAL_SCHEDULE_AUTOMATION_ENABLED_ENV = "GOAL_SCHEDULE_AUTOMATION_ENABLED"
GOAL_SCHEDULE_MATCH_THRESHOLD = 0.7
_AUTOMATION_LOCK = threading.Lock()


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
        connection.execute(
            """
            INSERT OR IGNORE INTO goal_schedule_assessments(event_id, assessed_at)
            VALUES (?, ?)
            """,
            (event_id, db.now_ts()),
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
        connection_event = conn.execute(
            "SELECT 1 FROM schedule_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        # A repeated unlink can arrive after sync has already removed the event;
        # that missing event_id must not leave a new assessment tombstone.
        if connection_event is not None:
            # Upsert so the user's removal is dated now: a stale timestamp would
            # look older than an existing goal and let the next scan re-link the
            # very pair the user just took apart.
            conn.execute(
                """
                INSERT INTO goal_schedule_assessments(event_id, assessed_at)
                VALUES (?, ?)
                ON CONFLICT(event_id) DO UPDATE SET assessed_at = excluded.assessed_at
                """,
                (event_id, db.now_ts()),
            )
        cursor = conn.execute(
            "DELETE FROM goal_schedule_links WHERE goal_id = ? AND event_id = ?",
            (goal_id, event_id),
        )
        return cursor.rowcount > 0


def automation_enabled() -> bool:
    """Whether background goal/schedule writes are allowed."""
    value = os.environ.get(
        GOAL_SCHEDULE_AUTOMATION_ENABLED_ENV, "1"
    ).strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def run_automation(
    client: LLMClient | None,
    model: str | None,
    *,
    now: float | None = None,
    matcher=None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Batch-match unassessed events, create links, and ledger expired links.

    A row in ``goal_schedule_assessments`` is the durable negative-result
    marker. It is separate from ``goal_schedule_links`` so a no-match decision
    is not mistaken for an event the LLM has never seen.
    """
    current = db.now_ts() if now is None else float(now)
    result: dict[str, Any] = {
        "enabled": automation_enabled() and goal_service.goal_tool_enabled(),
        "dry_run": dry_run,
        "event_count": 0,
        "assessed_event_ids": [],
        "links": [],
        "scheduled": [],
        "matching_status": "skipped",
    }
    if not result["enabled"]:
        return result

    with _AUTOMATION_LOCK:
        if not dry_run:
            _mark_linked_events_assessed(current)
        active_goals = goal_service.list_goals(status="active")
        events = _unassessed_events()
        result["event_count"] = len(events)

        decisions: list[dict[str, Any]] = []
        matching_completed = False
        if active_goals and events:
            if matcher is None:
                if client is None or not model:
                    result["matching_status"] = "model_unavailable"
                else:
                    decisions_or_none = goal_schedule_router.call_goal_schedule_router(
                        client,
                        model,
                        events=events,
                        goals=active_goals,
                    )
                    if decisions_or_none is None:
                        result["matching_status"] = "failed"
                        logging_service.log_event(
                            "goal_schedule_matching_failed",
                            level="WARNING",
                            event_count=len(events),
                            goal_count=len(active_goals),
                        )
                    else:
                        decisions = decisions_or_none
                        matching_completed = True
            else:
                decisions = matcher(events, active_goals)
                matching_completed = True

        normalized = _normalize_decisions(decisions, events, active_goals)
        decided_ids = {decision["event_id"] for decision in normalized}
        if matching_completed:
            result["matching_status"] = "ok"
        missing_ids = [
            str(event["id"])
            for event in events
            if str(event["id"]) not in decided_ids
        ]
        if matching_completed and missing_ids:
            logging_service.log_event(
                "goal_schedule_matching_incomplete",
                level="WARNING",
                missing_event_count=len(missing_ids),
                event_count=len(events),
            )

        result["assessed_event_ids"] = [decision["event_id"] for decision in normalized]
        goal_by_id = {str(goal["id"]): goal for goal in active_goals}
        event_by_id = {str(event["id"]): event for event in events}
        links = [
            {
                "event_id": decision["event_id"],
                "subject": str(event_by_id[decision["event_id"]]["subject"]),
                "goal_id": match["goal_id"],
                "goal_title": str(goal_by_id[match["goal_id"]]["title"]),
                "confidence": match["confidence"],
            }
            for decision in normalized
            for match in decision["matches"]
            if match["confidence"] >= GOAL_SCHEDULE_MATCH_THRESHOLD
        ]
        result["links"] = links

        if not dry_run and normalized:
            with db.transaction() as conn:
                for decision in normalized:
                    # Upsert, not INSERT OR IGNORE: a re-judged event must carry
                    # the new timestamp, otherwise it stays older than the newest
                    # goal forever and every later scan re-judges it again.
                    conn.execute(
                        """
                        INSERT INTO goal_schedule_assessments(event_id, assessed_at)
                        VALUES (?, ?)
                        ON CONFLICT(event_id) DO UPDATE SET assessed_at = excluded.assessed_at
                        """,
                        (decision["event_id"], current),
                    )
                    for match in decision["matches"]:
                        if match["confidence"] >= GOAL_SCHEDULE_MATCH_THRESHOLD:
                            link(match["goal_id"], decision["event_id"], conn=conn)

        due = _expired_linked_events(current)
        if dry_run:
            predicted = {
                (str(item["goal_id"]), str(item["event_id"])): item for item in due
            }
            for item in links:
                event = event_by_id[item["event_id"]]
                if float(event["end_ts"]) < current:
                    predicted[(item["goal_id"], item["event_id"])] = {
                        "goal_id": item["goal_id"],
                        "event_id": item["event_id"],
                        "subject": item["subject"],
                        "series_master_id": event["series_master_id"],
                    }
            result["scheduled"] = list(predicted.values())
        else:
            with db.transaction() as conn:
                for item in due:
                    goal_activity_service.record(
                        str(item["goal_id"]),
                        "scheduled",
                        "schedule",
                        f"schedule:{item['event_id']}",
                        conn=conn,
                    )
            result["scheduled"] = due
        return result


def run_automation_best_effort(
    client: LLMClient | None,
    model: str | None,
) -> dict[str, Any] | None:
    """Run maintenance without allowing it to fail schedule sync."""
    try:
        return run_automation(client, model)
    except Exception as exc:
        logging_service.log_event(
            "goal_schedule_automation_failed",
            level="WARNING",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None


def _mark_linked_events_assessed(assessed_at: float) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO goal_schedule_assessments(event_id, assessed_at)
            SELECT event_id, ?
            FROM goal_schedule_links
            """,
            (assessed_at,),
        )


def _unassessed_events() -> list[dict[str, Any]]:
    """Events still needing a verdict: never judged, or judged before a goal that
    did not exist yet was adopted.

    An assessment is not a permanent tombstone. Adopting goals is the product's
    core loop — every goal in the live workspace arrived that way — and a
    never-revisited verdict would mean a newly adopted goal could never pick up
    the schedule entries that belong to it, leaving its weekly progress stuck at
    zero. That is the same "the system is lying" failure this whole feature
    exists to fix, just relocated. Events that already have a link are left
    alone, so re-judging only ever costs one batch call after a new goal appears.
    """
    rows = db.query_all(
        """
        SELECT event.id, event.subject, event.start_ts, event.end_ts,
               event.series_master_id
        FROM schedule_events AS event
        LEFT JOIN goal_schedule_assessments AS assessment
          ON assessment.event_id = event.id
        WHERE event.is_cancelled = 0
          AND (
              assessment.event_id IS NULL
              OR assessment.assessed_at < (
                  SELECT MAX(goal.created_at)
                  FROM goals AS goal
                  WHERE goal.status = 'active'
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM goal_schedule_links AS link
              WHERE link.event_id = event.id
          )
        ORDER BY event.start_ts, event.end_ts, event.id
        """
    )
    return [dict(row) for row in rows]


def _normalize_decisions(
    decisions: object,
    events: Sequence[Mapping[str, Any]],
    goals: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(decisions, list):
        return []
    known_event_ids = {str(event["id"]) for event in events}
    known_goal_ids = {str(goal["id"]) for goal in goals}
    normalized: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for decision in decisions:
        # A malformed injected/router item such as an invented goal_id stays
        # unassessed, so the real event is retried instead of being frozen wrong.
        if not isinstance(decision, Mapping):
            continue
        event_id = decision.get("event_id")
        matches = decision.get("matches")
        if (
            not isinstance(event_id, str)
            or event_id not in known_event_ids
            or event_id in seen_event_ids
            or not isinstance(matches, list)
        ):
            continue
        normalized_matches: list[dict[str, Any]] = []
        valid = True
        best_by_goal: dict[str, float] = {}
        for match in matches:
            if not isinstance(match, Mapping):
                valid = False
                break
            goal_id = match.get("goal_id")
            confidence = _normalize_confidence(match.get("confidence"))
            if (
                not isinstance(goal_id, str)
                or goal_id not in known_goal_ids
                or confidence is None
            ):
                valid = False
                break
            best_by_goal[goal_id] = max(confidence, best_by_goal.get(goal_id, 0.0))
        if not valid:
            continue
        for goal_id, confidence in best_by_goal.items():
            normalized_matches.append(
                {"goal_id": goal_id, "confidence": confidence}
            )
        seen_event_ids.add(event_id)
        normalized.append({"event_id": event_id, "matches": normalized_matches})
    return normalized


def _normalize_confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return confidence if 0.0 <= confidence <= 1.0 else None


def _expired_linked_events(now: float) -> list[dict[str, Any]]:
    rows = db.query_all(
        """
        SELECT link.goal_id, event.id AS event_id, event.subject,
               event.series_master_id
        FROM goal_schedule_links AS link
        JOIN schedule_events AS event ON event.id = link.event_id
        JOIN goals AS goal ON goal.id = link.goal_id
        WHERE event.is_cancelled = 0
          AND event.end_ts < ?
        ORDER BY event.end_ts, event.id, link.goal_id
        """,
        (now,),
    )
    return [dict(row) for row in rows]


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
