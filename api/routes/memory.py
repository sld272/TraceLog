"""Memory workbench routes: status -> reconcile run -> view -> unit -> evidence.

A user edit is an ordinary, fully-reconcilable change that only raises
confidence and marks the old evidence links for AI re-link; editing therefore
enqueues a reconcile job so the background re-link pass runs.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import require_configured_runtime_or_409, run_sync
from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_unit_service as mus,
    memory_view_service as mvs,
    soul_relationship_memory as srm,
)
from core.app_services import job_service

router = APIRouter(prefix="/memory", tags=["memory"])


class CreateUnitRequest(BaseModel):
    owner_scope: str = "global"
    visibility_scope: str = "public"
    type: str
    content: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    tier: Literal["core", "contextual", "episodic"] = "contextual"
    importance: float = Field(default=0.8, ge=0.0, le=1.0)


class UpdateUnitRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    type: str | None = None
    tier: Literal["core", "contextual", "episodic"] | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)


class PromptPolicyRequest(BaseModel):
    prompt_policy: Literal["allow", "no_prompt"]


class PortraitPolicyRequest(BaseModel):
    portrait_policy: Literal["auto", "force_include", "force_exclude"]


class ResynthesizeViewRequest(BaseModel):
    owner_scope: str
    visibility_scope: str
    view_type: Literal["user_portrait", "soul_relationship_memory"]


def _memory_status() -> dict:
    buckets = mes.buckets_with_pending_events()
    pending_events = db.query_one(
        """
        SELECT COUNT(*) AS count
        FROM memory_ingest_events event
        LEFT JOIN memory_reconcile_cursors cursor
          ON cursor.owner_scope = event.owner_scope
         AND cursor.visibility_scope = event.visibility_scope
        WHERE event.id > COALESCE(cursor.last_event_id, 0)
        """
    )
    stale_views = db.query_one(
        "SELECT COUNT(*) AS count FROM memory_views WHERE status != 'fresh'"
    )
    pending_reviews = db.query_one(
        "SELECT COUNT(*) AS count FROM memory_unit_reconcile_queue WHERE status = 'pending'"
    )
    active_jobs = job_service.list_jobs(
        job_type=job_service.TYPE_RUN_MEMORY_RECONCILE,
        limit=20,
    )
    active_jobs = [
        job for job in active_jobs
        if job["status"] in {job_service.STATUS_PENDING, job_service.STATUS_RUNNING}
    ]
    return {
        "pending_event_count": int(pending_events["count"]) if pending_events else 0,
        "pending_buckets": [
            {"owner_scope": owner, "visibility_scope": visibility}
            for owner, visibility in buckets
        ],
        "pending_review_count": int(pending_reviews["count"]) if pending_reviews else 0,
        "pending_relink_count": len(mus.list_pending_relinks()),
        "stale_view_count": int(stale_views["count"]) if stale_views else 0,
        "active_jobs": active_jobs,
    }


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
        type=type,
        tier=None,
        prompt_policy=None,
        in_portrait=None,
        limit=limit,
    )
    return {"units": [dict(row) for row in rows]}


@router.post("/units")
async def create_unit(request: CreateUnitRequest):
    try:
        unit_id = await run_sync(
            mus.add_unit,
            owner_scope=request.owner_scope,
            visibility_scope=request.visibility_scope,
            source_channel="user",
            type=request.type,
            content=request.content,
            confidence=request.confidence,
            tier=request.tier,
            importance=request.importance,
            source="user_authored",
            actor="user",
        )
    except (ValueError, mus.BoundaryError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await run_sync(_unit_detail_or_404, unit_id)


@router.get("/units/{unit_id}")
async def get_unit(unit_id: str):
    return await run_sync(_unit_detail_or_404, unit_id)


@router.get("/status")
async def get_memory_status():
    return await run_sync(_memory_status)


@router.post("/reconcile")
async def trigger_reconcile():
    require_configured_runtime_or_409()
    job_id = await run_sync(
        job_service.enqueue_memory_reconcile_once,
        {"trigger": "memory_workbench"},
    )
    if job_id is None:
        jobs = await run_sync(
            job_service.list_jobs,
            status=job_service.STATUS_PENDING,
            job_type=job_service.TYPE_RUN_MEMORY_RECONCILE,
            limit=1,
        )
        job_id = int(jobs[0]["id"]) if jobs else None
    return {"job_id": job_id, "status": "queued" if job_id is not None else "running"}


@router.get("/operations")
async def list_operations(
    unit_id: str | None = Query(default=None),
    reconcile_run_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    rows = await run_sync(
        mus.list_unit_ops,
        unit_id=unit_id,
        reconcile_run_id=reconcile_run_id,
        limit=limit,
    )
    operations = []
    for row in rows:
        item = dict(row)
        for key in ("before_json", "after_json"):
            try:
                item[key.removesuffix("_json")] = (
                    json.loads(item[key]) if item.get(key) else None
                )
            except (TypeError, ValueError):
                item[key.removesuffix("_json")] = None
            item.pop(key, None)
        operations.append(item)
    return {"operations": operations}


@router.get("/reconcile-runs")
async def list_reconcile_runs(limit: int = Query(default=50, ge=1, le=500)):
    rows = await run_sync(
        db.query_all,
        "SELECT * FROM memory_reconcile_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return {"runs": [dict(row) for row in rows]}


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
    reason: Literal["false", "outdated"] = Query(default="outdated"),
):
    """User forgets a belief as outdated (default: reversible, may re-form on
    new evidence) or wrong ('false': never regenerate). Defaults to 'outdated'
    because the miscall costs are asymmetric — a wrong 'false' silently and
    permanently suppresses the claim. To merely hide a still-true memory
    ('不要提起'), use the prompt-policy endpoint instead."""
    if await run_sync(mus.get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    try:
        await run_sync(
            mus.retract_unit, unit_id, by="user", reason=reason, actor="user"
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # the background pass backfills the tombstone's normalized claim so the
    # suppression is paraphrase-proof from the next reconcile on.
    await run_sync(
        job_service.enqueue_memory_reconcile_once,
        {"trigger": "unit_retract", "unit_id": unit_id},
    )
    return {"ok": True}


@router.post("/units/{unit_id}/prompt-policy")
async def set_prompt_policy(unit_id: str, request: PromptPolicyRequest):
    if await run_sync(mus.get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    await run_sync(
        mus.set_prompt_policy, unit_id, prompt_policy=request.prompt_policy, actor="user"
    )
    return await run_sync(_unit_detail_or_404, unit_id)


@router.post("/units/{unit_id}/portrait-policy")
async def set_portrait_policy(unit_id: str, request: PortraitPolicyRequest):
    if await run_sync(mus.get_unit, unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    await run_sync(
        mus.set_portrait_policy,
        unit_id,
        portrait_policy=request.portrait_policy,
        actor="user",
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
    if request.view_type == mvs.VIEW_SOUL_RELATIONSHIP:
        soul_name = (
            request.owner_scope[len("soul:"):]
            if request.owner_scope.startswith("soul:")
            else ""
        )
        if not soul_name or request.visibility_scope != srm.VIEW_VISIBILITY:
            raise HTTPException(
                status_code=422,
                detail="SOUL 关系视图必须使用 soul:<name>/relationship 寻址",
            )
        view = await run_sync(srm.refresh_relationship_memory, soul_name)
        return asdict(view)

    try:
        mus.validate_boundary(request.owner_scope, request.visibility_scope)
    except mus.BoundaryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    expected = mvs.view_type_for_bucket(request.owner_scope, request.visibility_scope)
    if expected is None:
        raise HTTPException(
            status_code=422,
            detail=f"该 bucket 没有画像视图：{request.owner_scope}/{request.visibility_scope}",
        )
    if request.view_type != expected:
        raise HTTPException(
            status_code=422,
            detail=(
                f"view_type 与 bucket 不匹配：{request.owner_scope}/"
                f"{request.visibility_scope} 应为 {expected}，收到 {request.view_type}"
            ),
        )
    view = await run_sync(
        mvs.synthesize_view,
        request.owner_scope,
        request.visibility_scope,
        request.view_type,
    )
    return asdict(view)
