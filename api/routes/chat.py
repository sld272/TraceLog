"""Private SOUL chat routes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from api.deps import MODEL_NOT_CONFIGURED_MESSAGE, require_configured_runtime, run_sync
from core import chat_service

router = APIRouter(prefix="/chat", tags=["chat"])


class SendChatMessageRequest(BaseModel):
    content: str = Field(default="", max_length=20_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=9)


class UpdateChatMessageRequest(BaseModel):
    content: str = Field(default="", max_length=20_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=9)


@router.get("/threads/{thread_id}")
async def get_chat_thread(thread_id: int, limit: int = Query(default=30, ge=1, le=100)):
    try:
        thread = await run_sync(chat_service.get_thread, thread_id)
        messages = await run_sync(chat_service.list_thread_messages, thread_id, limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"thread": asdict(thread), "messages": [asdict(message) for message in messages]}


@router.get("/threads/{thread_id}/events")
async def stream_chat_thread_events(
    thread_id: int,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    try:
        await run_sync(chat_service.get_thread, thread_id)
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


@router.get("/{soul_name}/threads")
async def list_chat_threads(soul_name: str, all_souls: bool = False):
    try:
        threads = await run_sync(chat_service.list_chat_threads, None if all_souls else soul_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return [asdict(thread) for thread in threads]


@router.post("/{soul_name}/messages")
async def send_chat_message(soul_name: str, request: SendChatMessageRequest):
    body = request.content.strip()
    if not body and not request.attachment_ids:
        raise HTTPException(status_code=422, detail="content 不能为空")
    runtime = _runtime_or_409()
    try:
        thread = await run_sync(chat_service.get_or_create_thread, soul_name)
        result = await run_sync(chat_service.call_chat_reply, thread.id, body, runtime.client, runtime.model, request.attachment_ids)
        messages = await run_sync(chat_service.list_thread_messages, thread.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "thread": asdict(thread),
        "result": asdict(result),
        "messages": [asdict(message) for message in messages],
    }


@router.patch("/messages/{message_id}")
async def update_chat_message(message_id: int, request: UpdateChatMessageRequest):
    runtime = _runtime_or_409()
    try:
        result = await run_sync(
            chat_service.edit_user_message_and_reply,
            message_id,
            request.content,
            runtime.client,
            runtime.model,
            request.attachment_ids,
        )
    except ValueError as exc:
        status = 404 if str(exc).startswith("私聊消息不存在") else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {
        "thread": asdict(result["thread"]),
        "message": asdict(result["message"]),
        "result": asdict(result["result"]),
        "messages": [asdict(message) for message in result["messages"]],
    }


@router.post("/messages/{message_id}/rerun")
async def rerun_chat_message(message_id: int):
    runtime = _runtime_or_409()
    try:
        result = await run_sync(
            chat_service.rerun_assistant_message,
            message_id,
            runtime.client,
            runtime.model,
        )
    except ValueError as exc:
        status = 404 if str(exc).startswith("私聊消息不存在") else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "thread": asdict(result["thread"]),
        "message": asdict(result["message"]),
        "messages": [asdict(message) for message in result["messages"]],
    }


async def _message_stream(thread_id: int, after_id: int):
    current_id = after_id
    while True:
        messages = await run_sync(chat_service.list_thread_messages_after, thread_id, current_id)
        for message in messages:
            current_id = int(message.id)
            yield _format_message_sse(asdict(message))
        await asyncio.sleep(1.0)


def _format_message_sse(message: dict[str, Any]) -> str:
    return f"id: {message['id']}\nevent: chat_message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"


def _runtime_or_409():
    try:
        return require_configured_runtime()
    except RuntimeError as exc:
        if str(exc) == MODEL_NOT_CONFIGURED_MESSAGE:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
