"""SOUL management routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_configured_runtime_or_409, run_sync
from core import soul_service
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
    # 两者同时非空时走修订模式：按反馈修订 current_soul，不触发网络搜索
    current_soul: str | None = None
    feedback: str | None = None


class UpdateSoulRequest(BaseModel):
    soul: str | None = None
    description: str | None = None
    enabled: bool | None = None
    order: list[str] | None = None


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
    runtime = require_configured_runtime_or_409()
    current_soul = (request.current_soul or "").strip()
    feedback = (request.feedback or "").strip()
    if current_soul and feedback:
        result = await run_sync(
            soul_router.revise_soul,
            name=request.name,
            current_soul=current_soul,
            feedback=feedback,
            client=runtime.client,
            model=runtime.model,
        )
    else:
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


@router.get("/{name}/content")
async def get_soul_content(name: str):
    try:
        content = await run_sync(soul_service.read_soul_content, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"name": name, "soul": content}


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


def _record(record: Any) -> dict[str, Any]:
    return asdict(record)
