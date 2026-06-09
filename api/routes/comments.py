"""Post comment conversation routes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from api.deps import require_configured_runtime_or_409, run_sync
from core import comment_service

router = APIRouter(prefix="/comments", tags=["comments"])


class SendCommentMessageRequest(BaseModel):
    content: str = Field(default="", max_length=20_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=9)


@router.get("/posts/{post_id}/conversations")
async def list_comment_conversations(post_id: str):
    try:
        conversations = await run_sync(comment_service.list_post_conversations, post_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [asdict(conversation) for conversation in conversations]


@router.get("/posts/{post_id}/souls/{soul_name}")
async def get_comment_conversation(
    post_id: str,
    soul_name: str,
    limit: int = Query(default=30, ge=1, le=100),
):
    try:
        conversation = await run_sync(comment_service.get_conversation, post_id, soul_name)
        messages = await run_sync(comment_service.list_conversation_messages, post_id, soul_name, limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"conversation": asdict(conversation), "messages": [asdict(message) for message in messages]}


@router.get("/posts/{post_id}/souls/{soul_name}/events")
async def stream_comment_conversation_events(
    post_id: str,
    soul_name: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    try:
        await run_sync(comment_service.get_conversation, post_id, soul_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        after_id = int(last_event_id or "0")
    except ValueError:
        after_id = 0
    return StreamingResponse(
        _message_stream(post_id, soul_name, after_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/posts/{post_id}/souls/{soul_name}/messages")
async def send_comment_message(post_id: str, soul_name: str, request: SendCommentMessageRequest):
    body = request.content.strip()
    if not body and not request.attachment_ids:
        raise HTTPException(status_code=422, detail="content 不能为空")
    runtime = require_configured_runtime_or_409()
    try:
        result = await run_sync(comment_service.call_comment_reply, post_id, soul_name, body, runtime.client, runtime.model, request.attachment_ids)
        conversation = await run_sync(comment_service.get_conversation, post_id, soul_name)
        messages = await run_sync(comment_service.list_conversation_messages, post_id, soul_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "conversation": asdict(conversation),
        "result": asdict(result),
        "messages": [asdict(message) for message in messages],
    }


@router.delete("/messages/{comment_id}")
async def delete_comment_message(comment_id: int):
    try:
        return await run_sync(comment_service.delete_message, comment_id)
    except ValueError as exc:
        status = 404 if str(exc).startswith("评论消息不存在") else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/messages/{comment_id}/rerun")
async def rerun_comment_message(comment_id: int):
    runtime = require_configured_runtime_or_409()
    try:
        result = await run_sync(
            comment_service.rerun_latest_assistant_message,
            comment_id,
            runtime.client,
            runtime.model,
        )
    except ValueError as exc:
        status = 404 if str(exc).startswith("评论消息不存在") else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "message": asdict(result["message"]),
        "conversation": asdict(result["conversation"]),
        "messages": [asdict(message) for message in result["messages"]],
    }


async def _message_stream(post_id: str, soul_name: str, after_id: int):
    current_id = after_id
    while True:
        messages = await run_sync(comment_service.list_conversation_messages_after, post_id, soul_name, current_id)
        for message in messages:
            current_id = int(message.id)
            yield _format_message_sse(asdict(message))
        await asyncio.sleep(1.0)


def _format_message_sse(message: dict[str, Any]) -> str:
    return f"id: {message['id']}\nevent: comment_message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
