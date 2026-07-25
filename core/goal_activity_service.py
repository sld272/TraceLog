"""Goal activity ledger persistence."""

from __future__ import annotations

import sqlite3
from typing import Any

from core import db, goal_service

ACTIVITY_KINDS = ("commitment", "progress", "blocked", "milestone", "scheduled")
ACTIVITY_SOURCES = {"auto", "manual", "schedule"}
ACTIVITY_STATUSES = {"active", "rejected"}
PROGRESS_KINDS = {"progress", "milestone"}

_ACTIVITY_SELECT = """
    SELECT
        a.id, a.goal_id, a.kind, a.source, a.evidence_ref, a.evidence_span,
        a.confidence, a.status, a.created_at, a.decided_at,
        CASE
            WHEN a.evidence_ref LIKE 'post:%' THEN substr(a.evidence_ref, 6)
            WHEN a.evidence_ref LIKE 'comment:%' THEN c.post_id
            ELSE NULL
        END AS post_id
    FROM goal_activities AS a
    LEFT JOIN comments AS c
      ON a.evidence_ref LIKE 'comment:%'
     AND c.id = CAST(substr(a.evidence_ref, 9) AS INTEGER)
"""


def record(
    goal_id: str,
    kind: str,
    source: str,
    evidence_ref: str,
    *,
    evidence_span: str | None = None,
    confidence: float | None = None,
    created_at: float | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Append one idempotent activity, or return the existing tombstone."""
    normalized = _normalize_record(
        goal_id=goal_id,
        kind=kind,
        source=source,
        evidence_ref=evidence_ref,
        evidence_span=evidence_span,
        confidence=confidence,
        created_at=created_at,
    )

    def _write(c: sqlite3.Connection) -> dict[str, Any] | None:
        if goal_service.get_goal(normalized["goal_id"], conn=c) is None:
            return None
        cursor = c.execute(
            """
            INSERT OR IGNORE INTO goal_activities(
                goal_id, kind, source, evidence_ref, evidence_span,
                confidence, status, created_at, decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL)
            """,
            (
                normalized["goal_id"],
                normalized["kind"],
                normalized["source"],
                normalized["evidence_ref"],
                normalized["evidence_span"],
                normalized["confidence"],
                normalized["created_at"],
            ),
        )
        if cursor.rowcount > 0 and normalized["kind"] in PROGRESS_KINDS:
            c.execute(
                """
                UPDATE goals
                SET last_progress_at = CASE
                    WHEN last_progress_at IS NULL OR last_progress_at < ? THEN ?
                    ELSE last_progress_at
                END
                WHERE id = ?
                """,
                (
                    normalized["created_at"],
                    normalized["created_at"],
                    normalized["goal_id"],
                ),
            )
        row = c.execute(
            _ACTIVITY_SELECT
            + """
            WHERE a.goal_id = ? AND a.evidence_ref = ?
            """,
            (normalized["goal_id"], normalized["evidence_ref"]),
        ).fetchone()
        if row is None:
            raise RuntimeError("goal activity write failed")
        return _row_to_dict(row)

    if conn is not None:
        return _write(conn)
    with db.transaction() as owned:
        return _write(owned)


def reject(
    activity_id: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Reject an activity without deleting its idempotency tombstone."""
    normalized_id = int(activity_id)

    def _reject(c: sqlite3.Connection) -> dict[str, Any] | None:
        current = c.execute(
            "SELECT goal_id, kind, status FROM goal_activities WHERE id = ?",
            (normalized_id,),
        ).fetchone()
        if current is None:
            return None
        if current["status"] == "active":
            c.execute(
                """
                UPDATE goal_activities
                SET status = 'rejected', decided_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (db.now_ts(), normalized_id),
            )
            if current["kind"] in PROGRESS_KINDS:
                c.execute(
                    """
                    UPDATE goals
                    SET last_progress_at = (
                        SELECT MAX(created_at)
                        FROM goal_activities
                        WHERE goal_id = ?
                          AND status = 'active'
                          AND kind IN ('progress', 'milestone')
                    )
                    WHERE id = ?
                    """,
                    (current["goal_id"], current["goal_id"]),
                )
        row = c.execute(
            _ACTIVITY_SELECT + " WHERE a.id = ?",
            (normalized_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    if conn is not None:
        return _reject(conn)
    with db.transaction() as owned:
        return _reject(owned)


def list_for_goal(
    goal_id: str,
    *,
    status: str = "active",
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """List one goal's activities newest-first; active is the default view."""
    normalized_goal_id = _require_text(goal_id, "goal_id")
    if status not in ACTIVITY_STATUSES:
        raise ValueError("status 只支持：active、rejected")
    sql = (
        _ACTIVITY_SELECT
        + """
        WHERE a.goal_id = ? AND a.status = ?
        ORDER BY a.created_at DESC, a.id DESC
        """
    )
    rows = (
        conn.execute(sql, (normalized_goal_id, status)).fetchall()
        if conn is not None
        else db.query_all(sql, (normalized_goal_id, status))
    )
    return [_row_to_dict(row) for row in rows]


def stats_for_goal(
    goal_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Summarize active activity counts and latest progress timestamps.

    Careful: the returned ``last_progress_at`` counts ``progress`` only, so it
    is NOT the same value as ``goals.last_progress_at`` — the column tracks the
    latest forward activity of either kind (``progress`` or ``milestone``). Here
    the two kinds stay split because the UI shows them side by side; read the
    goal column when you want "latest forward activity".
    """
    normalized_goal_id = _require_text(goal_id, "goal_id")
    sql = """
        SELECT
            SUM(CASE WHEN kind = 'commitment' THEN 1 ELSE 0 END) AS commitment_count,
            SUM(CASE WHEN kind = 'progress' THEN 1 ELSE 0 END) AS progress_count,
            SUM(CASE WHEN kind = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
            SUM(CASE WHEN kind = 'milestone' THEN 1 ELSE 0 END) AS milestone_count,
            SUM(CASE WHEN kind = 'scheduled' THEN 1 ELSE 0 END) AS scheduled_count,
            MAX(CASE WHEN kind = 'progress' THEN created_at END) AS last_progress_at,
            MAX(CASE WHEN kind = 'milestone' THEN created_at END) AS last_milestone_at
        FROM goal_activities
        WHERE goal_id = ? AND status = 'active'
    """
    row = (
        conn.execute(sql, (normalized_goal_id,)).fetchone()
        if conn is not None
        else db.query_one(sql, (normalized_goal_id,))
    )
    if row is None:
        raise RuntimeError("goal activity stats query failed")
    return {
        "counts": {
            kind: int(row[f"{kind}_count"] or 0)
            for kind in ACTIVITY_KINDS
        },
        "last_progress_at": row["last_progress_at"],
        "last_milestone_at": row["last_milestone_at"],
    }


def _normalize_record(
    *,
    goal_id: str,
    kind: str,
    source: str,
    evidence_ref: str,
    evidence_span: str | None,
    confidence: float | None,
    created_at: float | None,
) -> dict[str, Any]:
    normalized_goal_id = _require_text(goal_id, "goal_id")
    if kind not in ACTIVITY_KINDS:
        raise ValueError("kind 只支持：commitment、progress、blocked、milestone、scheduled")
    if source not in ACTIVITY_SOURCES:
        raise ValueError("source 只支持：auto、manual、schedule")
    normalized_ref = _require_text(evidence_ref, "evidence_ref")
    if evidence_span is not None and not isinstance(evidence_span, str):
        raise ValueError("evidence_span 必须是字符串或 null")
    normalized_span = (
        evidence_span.strip()
        if isinstance(evidence_span, str) and evidence_span.strip()
        else None
    )
    if source == "auto" and normalized_span is None:
        raise ValueError("auto 动态必须包含 evidence_span")
    if source in {"manual", "schedule"} and confidence is not None:
        raise ValueError("manual、schedule 动态的 confidence 必须为 null")
    normalized_confidence = None if confidence is None else float(confidence)
    if normalized_confidence is not None and not 0.0 <= normalized_confidence <= 1.0:
        raise ValueError("confidence 必须在 0 到 1 之间")
    return {
        "goal_id": normalized_goal_id,
        "kind": kind,
        "source": source,
        "evidence_ref": normalized_ref,
        "evidence_span": normalized_span,
        "confidence": normalized_confidence,
        "created_at": db.now_ts() if created_at is None else float(created_at),
    }


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    return value.strip()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "goal_id": row["goal_id"],
        "kind": row["kind"],
        "source": row["source"],
        "evidence_ref": row["evidence_ref"],
        "evidence_span": row["evidence_span"],
        "confidence": row["confidence"],
        "status": row["status"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "post_id": row["post_id"],
    }
