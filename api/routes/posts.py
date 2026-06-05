"""Public post routes and SSE event stream."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from api.deps import get_runtime, run_sync
from core import attachment_service, db, vectorstore
from core.app_services import event_service, job_service, post_mutation, public_post_pipeline

router = APIRouter(tags=["posts"])


class CreatePostRequest(BaseModel):
    content: str = Field(default="", max_length=20_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=9)


@router.get("/health")
async def health():
    runtime = get_runtime()
    db_status = await run_sync(_check_db)
    return {
        "ok": db_status == "ok",
        "db": db_status,
        "vectorstore_initialized": vectorstore.is_initialized() or runtime.vectorstore_initialized,
    }


@router.post("/posts")
async def create_post(request: CreatePostRequest):
    content = request.content.strip()
    if not content and not request.attachment_ids:
        raise HTTPException(status_code=422, detail="content 不能为空")
    try:
        created = await run_sync(public_post_pipeline.create_post, content, request.attachment_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"post_id": created.post_id, "status": "queued", "job_ids": created.job_ids}


@router.get("/posts")
async def list_posts(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return await run_sync(_list_posts, limit, offset)


@router.get("/posts/{post_id}")
async def get_post(post_id: str):
    post = await run_sync(_get_post_detail, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return post


@router.delete("/posts/{post_id}")
async def delete_post(post_id: str):
    result = await run_sync(post_mutation.delete_post, post_id)
    if result is None:
        raise HTTPException(status_code=404, detail="post not found")
    return {"ok": True, **asdict(result)}


@router.get("/posts/{post_id}/events")
async def stream_post_events(
    post_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    exists = await run_sync(_post_exists, post_id)
    if not exists:
        raise HTTPException(status_code=404, detail="post not found")
    try:
        after_id = int(last_event_id or "0")
    except ValueError:
        after_id = 0
    return StreamingResponse(
        _event_stream(post_id, after_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _check_db() -> str:
    row = db.query_one("SELECT 1 AS ok")
    return "ok" if row is not None and row["ok"] == 1 else "error"


def _post_exists(post_id: str) -> bool:
    return db.query_one("SELECT 1 FROM posts WHERE id = ?", (post_id,)) is not None


def _list_posts(limit: int, offset: int) -> list[dict[str, Any]]:
    rows = db.query_all(
        """
        SELECT posts.id, posts.ts, posts.content, posts.importance,
               COUNT(comments.id) AS comment_count
        FROM posts
        LEFT JOIN comments ON comments.post_id = posts.id AND comments.seq = 0
        GROUP BY posts.id
        ORDER BY julianday(posts.ts) DESC, posts.id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    return [
        {
            "post_id": row["id"],
            "ts": row["ts"],
            "content": row["content"],
            "importance": row["importance"],
            "comment_count": row["comment_count"],
            "latest_event_type": event_service.latest_event_type(row["id"]),
            "attachments": [asdict(attachment) for attachment in attachment_service.list_post_attachments(row["id"])],
        }
        for row in rows
    ]


def _get_post_detail(post_id: str) -> dict[str, Any] | None:
    post = db.query_one(
        "SELECT id, ts, content, importance, created_at, updated_at FROM posts WHERE id = ?",
        (post_id,),
    )
    if post is None:
        return None
    comments = [
        _comment_row_to_dict(row)
        for row in db.query_all(
            """
            SELECT id, post_id, soul_name, role, content, seq, metadata, created_at, edited_at, rerun_at
            FROM comments
            WHERE post_id = ? AND seq = 0
            ORDER BY created_at ASC, id ASC
            """,
            (post_id,),
        )
    ]
    return {
        "post": {
            "post_id": post["id"],
            "ts": post["ts"],
            "content": post["content"],
            "importance": post["importance"],
            "created_at": post["created_at"],
            "updated_at": post["updated_at"],
            "attachments": [asdict(attachment) for attachment in attachment_service.list_post_attachments(post_id)],
        },
        "comments": comments,
        "jobs": job_service.list_jobs_for_post(post_id),
        "events": event_service.list_post_events(post_id),
    }


def _comment_row_to_dict(row) -> dict[str, Any]:
    item = dict(row)
    item["attachments"] = [asdict(attachment) for attachment in attachment_service.list_comment_attachments(int(row["id"]))]
    return item


SSE_TIMEOUT_SECONDS = 120


async def _event_stream(post_id: str, after_id: int):
    current_id = after_id
    elapsed = 0.0
    while elapsed < SSE_TIMEOUT_SECONDS:
        events = await run_sync(event_service.list_post_events, post_id, after_id=current_id)
        for event in events:
            current_id = int(event["id"])
            yield _format_sse(event)
            if event["event_type"] == "pipeline_done":
                return
        await asyncio.sleep(1.0)
        elapsed += 1.0


def _format_sse(event: dict[str, Any]) -> str:
    data = {
        "id": event["id"],
        "post_id": event["post_id"],
        "job_id": event["job_id"],
        "event_type": event["event_type"],
        "payload": event["payload"],
        "created_at": event["created_at"],
    }
    return f"id: {event['id']}\nevent: {event['event_type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
