"""Post delete helpers for API-facing mutation flows."""

from __future__ import annotations

from dataclasses import dataclass

from core import db, memory_events_service, memory_read, memory_unit_service, record_service
from core.app_services import job_service


@dataclass(frozen=True)
class DeletePostResult:
    post_id: str
    deleted_comments: int
    cancelled_jobs: int


@dataclass(frozen=True)
class EditPostResult:
    post_id: str
    content: str
    updated_at: float


def edit_post(post_id: str, content: str) -> EditPostResult | None:
    row = db.query_one("SELECT id FROM posts WHERE id = ?", (post_id,))
    if row is None:
        return None
    body = content.strip()
    if len(body) > 20_000:
        raise ValueError("content 不能超过 20000 字符")
    if not body and not db.query_one(
        "SELECT 1 FROM post_attachments WHERE post_id = ? LIMIT 1",
        (post_id,),
    ):
        raise ValueError("content 不能为空")
    now = db.now_ts()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE posts SET content = ?, updated_at = ? WHERE id = ?",
            (body, now, post_id),
        )
        event = memory_events_service.record_post_mutation(
            conn,
            post_id=post_id,
            op="edit",
            content=body,
            occurred_at=now,
        )
        memory_unit_service.challenge_units_for_source(conn, event.id)
    if body:
        record_service.index_post_embedding(post_id)
    else:
        record_service.delete_post_embedding(post_id)
    if memory_read.reconcile_write_enabled():
        job_service.enqueue_memory_reconcile_once({"trigger": "post_edit", "post_id": post_id})
    return EditPostResult(post_id=post_id, content=body, updated_at=now)


def delete_post(post_id: str) -> DeletePostResult | None:
    row = db.query_one("SELECT id FROM posts WHERE id = ?", (post_id,))
    if row is None:
        return None

    comment_rows = db.query_all(
        "SELECT id, soul_name, role FROM comments WHERE post_id = ? ORDER BY id",
        (post_id,),
    )
    comment_ids = [int(item["id"]) for item in comment_rows]
    cancelled_jobs = job_service.cancel_pending_jobs_for_post(post_id)

    with db.transaction() as conn:
        post_event = memory_events_service.record_post_mutation(
            conn, post_id=post_id, op="delete", content=None, occurred_at=db.now_ts()
        )
        memory_unit_service.challenge_units_for_source(conn, post_event.id)
        now = db.now_ts()
        for item in comment_rows:
            comment_event = memory_events_service.record_comment_mutation(
                conn,
                comment_id=int(item["id"]),
                post_id=post_id,
                soul_name=str(item["soul_name"]),
                role=str(item["role"]),
                op="delete",
                content=None,
                occurred_at=now,
            )
            memory_unit_service.challenge_units_for_source(conn, comment_event.id)
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    record_service.delete_post_embedding(post_id)
    record_service.delete_post_vision_embedding(post_id)
    for comment_id in comment_ids:
        record_service.delete_comment_embedding(comment_id)
    if memory_read.reconcile_write_enabled():
        job_service.enqueue_memory_reconcile_once({"trigger": "post_delete", "post_id": post_id})

    return DeletePostResult(
        post_id=post_id,
        deleted_comments=len(comment_ids),
        cancelled_jobs=cancelled_jobs,
    )
