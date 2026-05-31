"""Job inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.deps import run_sync
from core.app_services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(
    status: str | None = None,
    job_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    try:
        return await run_sync(job_service.list_jobs, status=status, job_type=job_type, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{job_id}")
async def get_job(job_id: int):
    job = await run_sync(job_service.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    try:
        new_job_id = await run_sync(job_service.retry_failed_job, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if new_job_id is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": new_job_id, "status": "queued"}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int):
    try:
        cancelled = await run_sync(job_service.cancel_pending_job, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if cancelled is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "status": job_service.STATUS_CANCELLED}
