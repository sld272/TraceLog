"""SOUL management routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api import deps
from api.deps import run_sync
from core import memory_review_service, soul_service
from core.llm import soul_router

router = APIRouter(prefix="/souls", tags=["souls"])


class CreateSoulRequest(BaseModel):
    name: str = Field(min_length=1)
    soul: str | None = None
    description: str | None = None
    enabled: bool = True


class GenerateSoulRequest(BaseModel):
    name: str = Field(min_length=1)
    inspiration: str = Field(min_length=1)


class UpdateSoulRequest(BaseModel):
    soul: str | None = None
    description: str | None = None
    enabled: bool | None = None
    order: list[str] | None = None


class UpdateSoulMemoryRequest(BaseModel):
    content: str = Field(min_length=1)


@router.get("")
async def list_souls(enabled_only: bool = False):
    records = await run_sync(soul_service.list_souls, enabled_only)
    return [_record(record) for record in records]


@router.post("")
async def create_soul(request: CreateSoulRequest):
    try:
        record = await run_sync(
            soul_service.create_soul,
            request.name,
            request.soul,
            request.description,
            request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _record(record)


@router.post("/generate-soul")
async def generate_soul(request: GenerateSoulRequest):
    runtime = deps.get_runtime()
    result = await run_sync(
        soul_router.generate_soul,
        name=request.name,
        inspiration=request.inspiration,
        client=runtime.client,
        model=runtime.model,
    )
    if result is None:
        raise HTTPException(status_code=502, detail="SOUL Markdown 生成失败")
    return result


@router.patch("/{name}")
async def update_soul(name: str, request: UpdateSoulRequest):
    try:
        if request.order is not None:
            records = await run_sync(soul_service.reorder_souls, request.order)
            return {"souls": [_record(record) for record in records]}
        if request.soul is not None or request.description is not None:
            await run_sync(soul_service.update_soul, name, request.soul, request.description)
        if request.enabled is True:
            record = await run_sync(soul_service.enable_soul, name)
        elif request.enabled is False:
            record = await run_sync(soul_service.disable_soul, name)
        else:
            record = await run_sync(soul_service.get_soul, name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _record(record)


@router.get("/{name}/memory")
async def get_soul_memory(name: str):
    try:
        content = await run_sync(memory_review_service.read_soul_memory, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"soul_name": name, "content": content}


@router.put("/{name}/memory")
async def update_soul_memory(name: str, request: UpdateSoulMemoryRequest):
    try:
        await run_sync(memory_review_service.save_soul_memory, name, request.content)
        content = await run_sync(memory_review_service.read_soul_memory, name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"soul_name": name, "content": content}


@router.get("/{name}/memory/revisions")
async def list_soul_memory_revisions(name: str, limit: int = 20):
    return await run_sync(memory_review_service.list_soul_revisions, name, limit)


@router.get("/{name}/memory/revisions/{revision_id}")
async def get_soul_memory_revision(name: str, revision_id: int):
    revision = await run_sync(memory_review_service.get_soul_revision, revision_id)
    if revision is None or revision.get("target_name") != name:
        raise HTTPException(status_code=404, detail="soul memory revision not found")
    return revision


def _record(record: Any) -> dict[str, Any]:
    return asdict(record)
