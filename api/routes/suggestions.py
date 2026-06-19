"""Unified suggestion review routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from api.deps import run_sync
from core import suggestion_service

router = APIRouter(prefix="/suggestions", tags=["suggestions"])


@router.get("")
async def list_pending_suggestions(
    kind: Literal["todo", "goal"] | None = Query(default=None),
):
    return await run_sync(suggestion_service.list_pending, kind)


@router.post("/{suggestion_id}/accept")
async def accept_suggestion(suggestion_id: str):
    try:
        return await run_sync(suggestion_service.accept, suggestion_id)
    except ValueError as exc:
        status = 404 if str(exc) == "suggestion 不存在" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: str):
    try:
        return await run_sync(suggestion_service.dismiss, suggestion_id)
    except ValueError as exc:
        status = 404 if str(exc) == "suggestion 不存在" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
