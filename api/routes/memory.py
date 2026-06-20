"""Memory workbench routes: portrait (view) -> unit -> evidence drill-down,
plus user edits of units (the v2 control surface that replaces editing the
legacy markdown files).

A user edit is an ordinary, fully-reconcilable change that only raises
confidence and marks the old evidence links for AI re-link; editing therefore
enqueues a reconcile job so the background re-link pass runs.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import run_sync
from core import memory_read, memory_unit_service as mus, memory_view_service as mvs
from core.app_services import job_service

router = APIRouter(prefix="/memory", tags=["memory"])


class UpdateUnitRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    type: str | None = None
    tier: Literal["core", "contextual", "episodic"] | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)


class PromptPolicyRequest(BaseModel):
    prompt_policy: Literal["allow", "no_prompt"]


class ProfilePolicyRequest(BaseModel):
    profile_policy: Literal["auto", "force_include", "force_exclude"]


class ResynthesizeViewRequest(BaseModel):
    owner_scope: str
    visibility_scope: str
    view_type: Literal["user_md", "soul_private_memory"]


def _unit_detail_or_404(unit_id: str):
    detail = memory_read.unit_detail(unit_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="unit not found")
    return asdict(detail)


@router.get("/units")
async def list_units(
    owner_scope: str | None = Query(default=None),
    visibility_scope: str | None = Query(default=None),
    status: str | None = Query(default="active", description="'all' returns every status"),
    type: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    status_filter = None if status == "all" else status
    rows = await run_sync(
        mus.list_units,
        owner_scope,
        visibility_scope,
        status=status_filter,
        tier=None,
        prompt_policy=None,
        in_md_slice=None,
        limit=limit,
    )
    units = [dict(row) for row in rows]
    if type is not None:
        units = [u for u in units if u.get("type") == type]
    return {"units": units}


@router.get("/units/{unit_id}")
async def get_unit(unit_id: str):
    return await run_sync(_unit_detail_or_404, unit_id)


@router.patch("/units/{unit_id}")
async def update_unit(unit_id: str, request: UpdateUnitRequest):
    if await run_sync(mus.get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    try:
        await run_sync(
            mus.update_unit,
            unit_id,
            content=request.content,
            confidence=request.confidence,
            type=request.type,
            tier=request.tier,
            importance=request.importance,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # the edit marked old links review_pending; trigger the background AI re-link.
    await run_sync(
        job_service.enqueue_memory_reconcile_once,
        {"trigger": "unit_edit", "unit_id": unit_id},
    )
    return await run_sync(_unit_detail_or_404, unit_id)


@router.delete("/units/{unit_id}")
async def retract_unit(
    unit_id: str,
    reason: Literal["false", "outdated"] = Query(default="false"),
):
    """User deletes a belief as wrong ('false') or outdated. To merely hide a
    still-true memory ('不要提到'), use the prompt-policy endpoint instead."""
    if await run_sync(mus.get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    try:
        await run_sync(
            mus.retract_unit, unit_id, by="user", reason=reason, actor="user"
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/units/{unit_id}/prompt-policy")
async def set_prompt_policy(unit_id: str, request: PromptPolicyRequest):
    if await run_sync(mus.get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    await run_sync(
        mus.set_prompt_policy, unit_id, prompt_policy=request.prompt_policy, actor="user"
    )
    return await run_sync(_unit_detail_or_404, unit_id)


@router.post("/units/{unit_id}/profile-policy")
async def set_profile_policy(unit_id: str, request: ProfilePolicyRequest):
    if await run_sync(mus.get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    await run_sync(
        mus.set_profile_policy, unit_id, profile_policy=request.profile_policy, actor="user"
    )
    return await run_sync(_unit_detail_or_404, unit_id)


@router.get("/views")
async def list_views():
    rows = await run_sync(mvs.list_views)
    return {"views": [dict(row) for row in rows]}


@router.post("/views/resynthesize")
async def resynthesize_view(request: ResynthesizeViewRequest):
    """Deterministically re-render a portrait view from its current core units
    (no LLM) and mark it fresh — the workbench's manual 'refresh portrait'."""
    try:
        view = await run_sync(
            mvs.synthesize_view,
            request.owner_scope,
            request.visibility_scope,
            request.view_type,
        )
    except Exception as exc:  # boundary / unknown scope
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return asdict(view)
