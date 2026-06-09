"""Manual reflection routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import MODEL_NOT_CONFIGURED_MESSAGE, require_configured_runtime, run_sync
from core import reflector
from core.app_services import job_service

router = APIRouter(prefix="/reflections", tags=["reflections"])


class TriggerGlobalReflectionRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class TriggerSoulReflectionsRequest(BaseModel):
    limit_per_soul: int = Field(default=100, ge=1, le=500)


@router.get("/global/preview")
async def preview_global_reflection(limit: int = Query(default=100, ge=1, le=500)):
    scope = await run_sync(reflector.preview_global_deep_reflection_scope, limit)
    return asdict(scope)


@router.post("/global")
async def trigger_global_reflection(request: TriggerGlobalReflectionRequest):
    _require_runtime_or_409()
    job_id = await run_sync(
        job_service.enqueue,
        job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION,
        {"trigger": "api_manual", "limit": request.limit},
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/souls/preview")
async def preview_soul_reflections(limit_per_soul: int = Query(default=100, ge=1, le=500)):
    scopes = await run_sync(reflector.preview_soul_deep_reflection_scopes, limit_per_soul)
    return [asdict(scope) for scope in scopes]


@router.post("/souls")
async def trigger_soul_reflections(request: TriggerSoulReflectionsRequest):
    _require_runtime_or_409()
    job_id = await run_sync(
        job_service.enqueue,
        job_service.TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS,
        {"trigger": "api_manual", "limit_per_soul": request.limit_per_soul},
    )
    return {"job_id": job_id, "status": "queued"}


def _require_runtime_or_409() -> None:
    try:
        require_configured_runtime()
    except RuntimeError as exc:
        if str(exc) == MODEL_NOT_CONFIGURED_MESSAGE:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
