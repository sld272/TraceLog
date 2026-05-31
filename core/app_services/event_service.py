"""Post event persistence for API polling and SSE streams."""

from __future__ import annotations

import json
from typing import Any

from core import db


def append_post_event(
    post_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    job_id: int | None = None,
) -> int:
    """Append one product-facing event for a post."""
    now = db.now_ts()
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO post_events(post_id, job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, job_id, event_type, json.dumps(payload or {}, ensure_ascii=False), now),
        )
        return db.require_lastrowid(cur, "post event insert")


def list_post_events(post_id: str, *, after_id: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, post_id, job_id, event_type, payload_json, created_at
        FROM post_events
        WHERE post_id = ? AND id > ?
        ORDER BY id ASC
    """
    params: tuple = (post_id, after_id)
    if limit is not None:
        sql += " LIMIT ?"
        params = (post_id, after_id, limit)
    return [_row_to_dict(row) for row in db.query_all(sql, params)]


def latest_event_type(post_id: str) -> str | None:
    row = db.query_one(
        """
        SELECT event_type
        FROM post_events
        WHERE post_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (post_id,),
    )
    return row["event_type"] if row is not None else None


def _row_to_dict(row) -> dict[str, Any]:
    payload_json = row["payload_json"]
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "id": row["id"],
        "post_id": row["post_id"],
        "job_id": row["job_id"],
        "event_type": row["event_type"],
        "payload_json": payload_json,
        "payload": payload,
        "created_at": row["created_at"],
    }
