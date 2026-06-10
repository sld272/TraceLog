"""Feedback routes for retrieval evidence quality."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.deps import run_sync
from core import evidence_feedback_service

router = APIRouter(prefix="/feedback", tags=["feedback"])


class EvidenceFeedbackRequest(BaseModel):
    channel: Literal["chat", "comment", "public_post"]
    message_id: int = Field(gt=0)
    doc_id: str = Field(min_length=1, max_length=512)
    verdict: Literal["irrelevant"] = "irrelevant"


@router.post("/evidence")
async def record_evidence_feedback(request: EvidenceFeedbackRequest):
    try:
        feedback = await run_sync(
            evidence_feedback_service.record_feedback,
            channel=request.channel,
            message_id=request.message_id,
            doc_id=request.doc_id,
            verdict=request.verdict,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return asdict(feedback)
