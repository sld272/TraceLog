"""Post comment thread routes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from api.deps import get_runtime, run_sync
from core import comment_service

router = APIRouter(prefix="/comments", tags=["comments"])


class SendCommentMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


@router.get("/posts/{post_id}/threads")
async def list_comment_threads(post_id: str):
    try:
        threads = await run_sync(comment_service.list_post_threads, post_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [asdict(thread) for thread in threads]


@router.get("/threads/{thread_id}")
async def get_comment_thread(thread_id: int, limit: int = Query(default=30, ge=1, le=100)):
    try:
        thread = await run_sync(comment_service.get_thread, thread_id)
        messages = await run_sync(comment_service.list_thread_messages, thread_id, limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"thread": asdict(thread), "messages": [asdict(message) for message in messages]}


@router.get("/threads/{thread_id}/events")
async def stream_comment_thread_events(
    thread_id: int,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    try:
        await run_sync(comment_service.get_thread, thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        after_id = int(last_event_id or "0")
    except ValueError:
        after_id = 0
    return StreamingResponse(
        _message_stream(thread_id, after_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{post_id}/{soul_name}/messages")
async def send_comment_message(post_id: str, soul_name: str, request: SendCommentMessageRequest):
    body = request.content.strip()
    if not body:
        raise HTTPException(status_code=422, detail="content 不能为空")
    runtime = get_runtime()
    try:
        thread = await run_sync(comment_service.get_or_create_thread, post_id, soul_name)
        result = await run_sync(comment_service.call_comment_reply, thread.id, body, runtime.client, runtime.model)
        messages = await run_sync(comment_service.list_thread_messages, thread.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "thread": asdict(thread),
        "result": asdict(result),
        "messages": [asdict(message) for message in messages],
    }


async def _message_stream(thread_id: int, after_id: int):
    current_id = after_id
    while True:
        messages = await run_sync(comment_service.list_thread_messages_after, thread_id, current_id)
        for message in messages:
            current_id = int(message.id)
            yield _format_message_sse(asdict(message))
        await asyncio.sleep(1.0)


def _format_message_sse(message: dict[str, Any]) -> str:
    return f"id: {message['id']}\nevent: comment_message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
