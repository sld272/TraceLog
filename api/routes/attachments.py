"""Image attachment upload and serving routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.responses import FileResponse

from api.deps import run_sync
from core import attachment_service

router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.post("/upload")
async def upload_attachment(file: UploadFile = File(...)):
    content = await file.read()
    try:
        attachment = await run_sync(
            attachment_service.upload_image,
            content,
            content_type=file.content_type,
            filename=file.filename,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return asdict(attachment)


@router.get("/{attachment_id}")
async def get_attachment(attachment_id: str):
    try:
        attachment = await run_sync(attachment_service.get_attachment, attachment_id)
        path = await run_sync(attachment_service.attachment_path, attachment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type=attachment.mime_type)
