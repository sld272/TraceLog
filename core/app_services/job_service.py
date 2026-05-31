"""SQLite-backed background job state for API workflows."""

from __future__ import annotations

import json
from typing import Any

from core import db

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TYPE_INDEX_POST_EMBEDDING = "index_post_embedding"
TYPE_GENERATE_POST_REPLIES = "generate_post_replies"
TYPE_RUN_TODO_TOOL = "run_todo_tool"
TYPE_RUN_LIGHT_REFLECTION = "run_light_reflection"
TYPE_MAYBE_TRIGGER_GLOBAL_DEEP_REFLECTION = "maybe_trigger_global_deep_reflection"
TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION = "trigger_global_deep_reflection"
TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS = "trigger_soul_deep_reflections"

VALID_STATUSES = {STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED}
VALID_TYPES = {
    TYPE_INDEX_POST_EMBEDDING,
    TYPE_GENERATE_POST_REPLIES,
    TYPE_RUN_TODO_TOOL,
    TYPE_RUN_LIGHT_REFLECTION,
    TYPE_MAYBE_TRIGGER_GLOBAL_DEEP_REFLECTION,
    TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION,
    TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS,
}


def enqueue(job_type: str, payload: dict[str, Any], *, max_attempts: int = 1) -> int:
    """Create one pending job and return its id."""
    if job_type not in VALID_TYPES:
        raise ValueError(f"unsupported job type: {job_type}")
    now = db.now_ts()
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs(type, status, payload_json, attempts, max_attempts, created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (job_type, STATUS_PENDING, json.dumps(payload, ensure_ascii=False), max_attempts, now, now),
        )
        return db.require_lastrowid(cur, "job insert")


def claim_next_pending() -> dict[str, Any] | None:
    """Atomically claim the oldest pending job."""
    now = db.now_ts()
    with db.immediate_transaction() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (STATUS_PENDING,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, attempts = attempts + 1, started_at = ?, updated_at = ?, error = NULL
            WHERE id = ? AND status = ?
            """,
            (STATUS_RUNNING, now, now, row["id"], STATUS_PENDING),
        )
        claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    return _row_to_dict(claimed) if claimed is not None else None


def mark_succeeded(job_id: int) -> None:
    now = db.now_ts()
    db.execute(
        """
        UPDATE jobs
        SET status = ?, updated_at = ?, finished_at = ?, error = NULL
        WHERE id = ?
        """,
        (STATUS_SUCCEEDED, now, now, job_id),
    )


def mark_failed(job_id: int, error: str) -> None:
    now = db.now_ts()
    db.execute(
        """
        UPDATE jobs
        SET status = ?, updated_at = ?, finished_at = ?, error = ?
        WHERE id = ?
        """,
        (STATUS_FAILED, now, now, error, job_id),
    )


def mark_failed_or_retry(job_id: int, error: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    if int(job["attempts"]) < int(job["max_attempts"]):
        now = db.now_ts()
        db.execute(
            """
            UPDATE jobs
            SET status = ?, updated_at = ?, error = ?, started_at = NULL, finished_at = NULL
            WHERE id = ?
            """,
            (STATUS_PENDING, now, error, job_id),
        )
        return
    mark_failed(job_id, error)


def retry_failed_job(job_id: int) -> int | None:
    """Create a fresh pending copy of a failed job for manual retry."""
    job = get_job(job_id)
    if job is None:
        return None
    if job["status"] != STATUS_FAILED:
        raise ValueError("only failed jobs can be retried")
    return enqueue(job["type"], job.get("payload") or {}, max_attempts=int(job["max_attempts"]))


def cancel_pending_job(job_id: int) -> bool | None:
    """Cancel a pending job. Running jobs are not preempted in the in-process worker."""
    job = get_job(job_id)
    if job is None:
        return None
    if job["status"] != STATUS_PENDING:
        raise ValueError("only pending jobs can be cancelled")
    now = db.now_ts()
    db.execute(
        """
        UPDATE jobs
        SET status = ?, updated_at = ?, finished_at = ?
        WHERE id = ? AND status = ?
        """,
        (STATUS_CANCELLED, now, now, job_id, STATUS_PENDING),
    )
    return True


def reset_running_to_pending() -> int:
    """Return running jobs to pending after an interrupted API worker shutdown."""
    now = db.now_ts()
    with db.transaction() as conn:
        rows = conn.execute("SELECT id FROM jobs WHERE status = ?", (STATUS_RUNNING,)).fetchall()
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, updated_at = ?, started_at = NULL
            WHERE status = ?
            """,
            (STATUS_PENDING, now, STATUS_RUNNING),
        )
    return len(rows)


def get_job(job_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return _row_to_dict(row) if row is not None else None


def list_jobs(
    *,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List jobs for API inspection."""
    clauses = []
    params: list[Any] = []
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"unsupported job status: {status}")
        clauses.append("status = ?")
        params.append(status)
    if job_type is not None:
        if job_type not in VALID_TYPES:
            raise ValueError(f"unsupported job type: {job_type}")
        clauses.append("type = ?")
        params.append(job_type)

    sql = "SELECT * FROM jobs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit), 100)), max(0, int(offset))])
    return [_row_to_dict(row) for row in db.query_all(sql, tuple(params))]


def list_jobs_for_post(post_id: str) -> list[dict[str, Any]]:
    rows = db.query_all("SELECT * FROM jobs ORDER BY id ASC")
    jobs = []
    for row in rows:
        item = _row_to_dict(row)
        payload = item.get("payload") or {}
        if payload.get("post_id") == post_id:
            jobs.append(item)
    return jobs


def _row_to_dict(row) -> dict[str, Any]:
    payload_json = row["payload_json"]
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "status": row["status"],
        "payload_json": payload_json,
        "payload": payload,
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
