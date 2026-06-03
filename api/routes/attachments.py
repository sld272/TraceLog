"""Image attachment upload and serving routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse

from api.deps import run_sync
from core import attachment_service, logging_service

router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.post("/upload")
async def upload_attachment(request: Request):
    try:
        form = await request.form()
    except Exception as exc:
        logging_service.log_event(
            "attachment_upload_failed",
            level="WARNING",
            reason="invalid_multipart_form",
            content_type=request.headers.get("content-type"),
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail="上传请求格式无效") from exc

    file = _first_upload_file(form)
    if file is None:
        logging_service.log_event(
            "attachment_upload_failed",
            level="WARNING",
            reason="missing_file",
            content_type=request.headers.get("content-type"),
            form_fields=list(form.keys()),
        )
        raise HTTPException(status_code=400, detail="没有找到上传图片文件")

    filename = file.filename
    content_type = file.content_type
    content = await file.read()
    await file.close()
    try:
        attachment = await run_sync(
            attachment_service.upload_image,
            content,
            content_type=content_type,
            filename=filename,
        )
    except ValueError as exc:
        logging_service.log_event(
            "attachment_upload_failed",
            level="WARNING",
            reason="validation_error",
            filename=filename,
            content_type=content_type,
            detected_image_format=getattr(exc, "image_format", None),
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        logging_service.log_event(
            "attachment_upload_failed",
            level="ERROR",
            reason="runtime_error",
            filename=filename,
            content_type=content_type,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    logging_service.log_event(
        "attachment_uploaded",
        attachment_id=attachment.id,
        filename=filename,
        content_type=content_type,
        stored_mime_type=attachment.mime_type,
        file_size=attachment.file_size,
        width=attachment.width,
        height=attachment.height,
    )
    return asdict(attachment)


@router.get("/{attachment_id}")
async def get_attachment(attachment_id: str):
    try:
        attachment = await run_sync(attachment_service.get_attachment, attachment_id)
        path = await run_sync(attachment_service.attachment_path, attachment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type=attachment.mime_type)


def _first_upload_file(form) -> UploadFile | None:
    preferred = form.get("file")
    if isinstance(preferred, UploadFile):
        return preferred
    for _, value in form.multi_items():
        if isinstance(value, UploadFile):
            return value
    return None
