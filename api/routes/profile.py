"""User profile memory routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import run_sync
from core import memory_review_service

router = APIRouter(prefix="/profile", tags=["profile"])


class UpdateProfileRequest(BaseModel):
    content: str = Field(min_length=1)


@router.get("")
async def get_profile():
    content = await run_sync(memory_review_service.read_user_memory)
    return {"content": content}


@router.put("")
async def update_profile(request: UpdateProfileRequest):
    try:
        await run_sync(memory_review_service.save_user_memory, request.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "content": await run_sync(memory_review_service.read_user_memory)}


@router.get("/revisions")
async def list_profile_revisions(
    limit: int = Query(default=20, ge=1, le=100),
    source: str | None = None,
):
    return await run_sync(memory_review_service.list_user_revisions, limit, source)


@router.get("/revisions/{revision_id}")
async def get_profile_revision(revision_id: int):
    revision = await run_sync(memory_review_service.get_user_revision, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="profile revision not found")
    return revision
