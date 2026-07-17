"""Pending, accepted, and dismissed goal or schedule suggestions."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any

from core import db, goal_service
from core.graph.client import GraphHTTPError
from core.schedule_service import (
    LOCAL_ACCOUNT_ID,
    LOCAL_TIMEZONE,
    NoWritableAccountError,
    ScheduleService,
)

SUGGESTION_KINDS = {"goal", "schedule"}
SUGGESTION_STATUSES = {"pending", "accepted", "dismissed"}


class SuggestionExpiredError(ValueError):
    """Raised when a schedule suggestion's local-time end has passed."""


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
        conn.execute(
            "SELECT * FROM suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if conn is not None
        else db.query_one(
            "SELECT * FROM suggestions WHERE id = ?",
            (suggestion_id,),
        )
    )
    return (
        _row_to_dict(row)
        if row is not None and row["kind"] in SUGGESTION_KINDS
        else None
    )


def list_pending(kind: str | None = None) -> list[dict[str, Any]]:
    if kind is not None:
        _validate_kind(kind)
    supported_kinds = tuple(sorted(SUGGESTION_KINDS))
    sql = """
        SELECT *
        FROM suggestions
        WHERE status = 'pending' AND kind IN (?, ?)
    """
    params: tuple[Any, ...] = supported_kinds
    if kind is not None:
        sql += " AND kind = ?"
        params = (*params, kind)
    rows = db.query_all(
        sql
        + """
        ORDER BY confidence DESC, created_at ASC, id ASC
        """,
        params,
    )
    return [_row_to_dict(row) for row in rows]


def accept(suggestion_id: str, *, fallback_local: bool = False) -> dict[str, Any]:
    """Accept one pending suggestion and create its target object."""
    row = db.query_one("SELECT kind FROM suggestions WHERE id = ?", (suggestion_id,))
    if row is None or row["kind"] not in SUGGESTION_KINDS:
        raise ValueError("suggestion 不存在")
    kind = str(row["kind"])
    _validate_kind(kind)
    if kind == "schedule":
        return _accept_schedule(suggestion_id, fallback_local=fallback_local)
    return _accept_goal(suggestion_id)


def _accept_goal(suggestion_id: str) -> dict[str, Any]:
    """Create a goal and mark its suggestion in one SQLite transaction."""
    with db.immediate_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if row is None or row["kind"] != "goal":
            raise ValueError("suggestion 不存在")
        if row["status"] != "pending":
            raise ValueError("suggestion 已处理")
        suggestion = _row_to_dict(row)
        payload = suggestion["payload"]
        created = goal_service.create_goal(
            payload["title"],
            payload.get("detail"),
            payload["horizon"],
            source="suggested_accepted",
            focus=bool(payload.get("focus", False)),
            conn=conn,
        )
        decided_at = db.now_ts()
        conn.execute(
            "UPDATE suggestions SET status = 'accepted', decided_at = ? WHERE id = ?",
            (decided_at, suggestion_id),
        )
        updated = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return {"suggestion": _row_to_dict(updated), "created": created}


def _accept_schedule(
    suggestion_id: str,
    *,
    fallback_local: bool,
) -> dict[str, Any]:
    """Create an event outside SQLite transactions, then finalize the row."""
    row = db.query_one("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))
    if row is None or row["kind"] != "schedule":
        raise ValueError("suggestion 不存在")
    if row["status"] != "pending":
        raise ValueError("suggestion 已处理")
    suggestion = _row_to_dict(row)
    payload = suggestion["payload"]
    if _schedule_suggestion_expired(payload, now=_now_local()):
        raise SuggestionExpiredError("suggestion_expired")

    service = ScheduleService()
    create_kwargs = _schedule_create_kwargs(payload, suggestion_id=suggestion_id)
    try:
        created = service.create_event(**create_kwargs)
    except NoWritableAccountError:
        if not fallback_local:
            raise
        try:
            service.create_local_account()
        except ValueError:
            if not any(
                account.get("id") == LOCAL_ACCOUNT_ID
                for account in service.list_accounts()
            ):
                raise
        local_create_kwargs = {**create_kwargs, "account_id": LOCAL_ACCOUNT_ID}
        created = service.create_event(**local_create_kwargs)
    except GraphHTTPError as exc:
        if exc.status_code != 409:
            raise
        created = _recover_graph_retry_event(service, payload)

    with db.immediate_transaction() as conn:
        current = conn.execute(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        if current is None:
            raise ValueError("suggestion 不存在")
        if current["status"] != "pending":
            raise ValueError("suggestion 已处理")
        conn.execute(
            "UPDATE suggestions SET status = 'accepted', decided_at = ? WHERE id = ?",
            (db.now_ts(), suggestion_id),
        )
        updated = conn.execute(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
    return {"suggestion": _row_to_dict(updated), "created": created}


def dismiss(suggestion_id: str) -> dict[str, Any]:
    """Dismiss a suggestion; its normalized key remains as a permanent tombstone."""
    with db.immediate_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if row is None or row["kind"] not in SUGGESTION_KINDS:
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
        # Keep this material byte-for-byte compatible with existing rows.
        material = {
            "kind": kind,
            "title": _normalize_key_text(payload.get("title")),
            "horizon": payload.get("horizon"),
            "source": source_kind,
        }
    else:
        material = {
            "kind": kind,
            "subject": _normalize_key_text(payload.get("subject")),
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
    if kind == "schedule":
        return _normalize_schedule_payload(payload)
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


def _validate_kind(kind: Any) -> None:
    if kind not in SUGGESTION_KINDS:
        raise ValueError("kind 只支持：goal、schedule")


def _normalize_schedule_payload(payload: dict[str, Any]) -> dict[str, Any]:
    subject = payload.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("schedule suggestion subject 不能为空")
    clean_subject = subject.strip()
    if len(clean_subject) > 500:
        raise ValueError("schedule suggestion subject 不能超过 500 个字符")
    event_date = _parse_schedule_date(payload.get("date"))
    all_day = payload.get("all_day", False)
    if not isinstance(all_day, bool):
        raise ValueError("schedule suggestion all_day 必须是布尔值")
    start_time = _parse_schedule_time(payload.get("start_time"), field="start_time")
    end_time = _parse_schedule_time(payload.get("end_time"), field="end_time")
    if all_day:
        start_time = None
        end_time = None
    else:
        effective_start = start_time or datetime_time(hour=9)
        if end_time is not None and end_time <= effective_start:
            raise ValueError("schedule suggestion end_time 必须晚于 start_time")
    goal_id = payload.get("goal_id")
    if goal_id is not None:
        if not isinstance(goal_id, str):
            raise ValueError("schedule suggestion goal_id 必须是字符串或 null")
        goal_id = goal_id.strip() or None
    return {
        "subject": clean_subject,
        "date": event_date.isoformat(),
        "start_time": start_time.strftime("%H:%M") if start_time is not None else None,
        "end_time": end_time.strftime("%H:%M") if end_time is not None else None,
        "all_day": all_day,
        "goal_id": goal_id,
    }


def _parse_schedule_date(value: Any) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("schedule suggestion date 必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("schedule suggestion date 无效") from exc


def _parse_schedule_time(value: Any, *, field: str) -> datetime_time | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ValueError(f"schedule suggestion {field} 必须是 HH:MM 或 null")
    return datetime_time.fromisoformat(value)


def _schedule_create_kwargs(
    payload: dict[str, Any],
    *,
    suggestion_id: str,
) -> dict[str, Any]:
    return {
        "subject": payload["subject"],
        "event_date": date.fromisoformat(payload["date"]),
        "start_time": _time_from_normalized(payload.get("start_time")),
        "end_time": _time_from_normalized(payload.get("end_time")),
        "all_day": bool(payload.get("all_day", False)),
        "goal_id": payload.get("goal_id"),
        "account_id": None,
        "client_request_id": suggestion_id,
    }


def _time_from_normalized(value: Any) -> datetime_time | None:
    return datetime_time.fromisoformat(value) if isinstance(value, str) else None


def _schedule_suggestion_expired(payload: dict[str, Any], *, now: datetime) -> bool:
    event_date = date.fromisoformat(payload["date"])
    if payload.get("all_day"):
        expires_at = datetime.combine(event_date, datetime_time.max, LOCAL_TIMEZONE)
    else:
        start_time = _time_from_normalized(payload.get("start_time")) or datetime_time(hour=9)
        end_time = _time_from_normalized(payload.get("end_time"))
        expires_at = (
            datetime.combine(event_date, end_time, LOCAL_TIMEZONE)
            if end_time is not None
            else datetime.combine(event_date, start_time, LOCAL_TIMEZONE) + timedelta(hours=1)
        )
    local_now = now.replace(tzinfo=LOCAL_TIMEZONE) if now.tzinfo is None else now.astimezone(LOCAL_TIMEZONE)
    return expires_at < local_now


def _recover_graph_retry_event(
    service: ScheduleService,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Best-effort cache recovery after Graph confirms a transaction duplicate."""
    try:
        service.sync()
        event_date = date.fromisoformat(payload["date"])
        expected_start = _schedule_start_datetime(payload).timestamp()
        events = service.list_events(event_date, event_date).get("events", [])
        return next(
            (
                event
                for event in events
                if event.get("subject") == payload["subject"]
                and abs(float(event.get("start_ts")) - expected_start) < 0.5
            ),
            None,
        )
    except Exception:
        return None


def _schedule_start_datetime(payload: dict[str, Any]) -> datetime:
    event_date = date.fromisoformat(payload["date"])
    start_time = (
        datetime_time.min
        if payload.get("all_day")
        else _time_from_normalized(payload.get("start_time")) or datetime_time(hour=9)
    )
    return datetime.combine(event_date, start_time, LOCAL_TIMEZONE)


def _now_local() -> datetime:
    return datetime.now(LOCAL_TIMEZONE)


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
