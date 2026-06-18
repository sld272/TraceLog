"""Post delete helpers for API-facing mutation flows."""

from __future__ import annotations

from dataclasses import dataclass

from core import db, memory_events_service, record_service
from core.app_services import job_service


@dataclass(frozen=True)
class DeletePostResult:
    post_id: str
    deleted_comments: int
    cancelled_jobs: int


def delete_post(post_id: str) -> DeletePostResult | None:
    row = db.query_one("SELECT id FROM posts WHERE id = ?", (post_id,))
    if row is None:
        return None

    comment_rows = db.query_all("SELECT id FROM comments WHERE post_id = ?", (post_id,))
    comment_ids = [int(item["id"]) for item in comment_rows]
    cancelled_jobs = job_service.cancel_pending_jobs_for_post(post_id)

    with db.transaction() as conn:
        memory_events_service.record_post_mutation(
            conn, post_id=post_id, op="delete", content=None, occurred_at=db.now_ts()
        )
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    record_service.delete_post_embedding(post_id)
    record_service.delete_post_vision_embedding(post_id)
    for comment_id in comment_ids:
        record_service.delete_comment_embedding(comment_id)

    return DeletePostResult(
        post_id=post_id,
        deleted_comments=len(comment_ids),
        cancelled_jobs=cancelled_jobs,
    )
