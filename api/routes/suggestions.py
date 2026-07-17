"""Unified suggestion review routes."""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.deps import run_sync
from core import suggestion_service
from core.graph.client import GraphHTTPError
from core.schedule_service import NoWritableAccountError

router = APIRouter(prefix="/suggestions", tags=["suggestions"])


class AcceptSuggestionRequest(BaseModel):
    fallback_local: bool = False


@router.get("")
async def list_pending_suggestions(
    kind: Literal["goal", "schedule"] | None = Query(default=None),
):
    return await run_sync(suggestion_service.list_pending, kind)


@router.post("/{suggestion_id}/accept")
async def accept_suggestion(
    suggestion_id: str,
    request: AcceptSuggestionRequest | None = None,
):
    try:
        return await run_sync(
            suggestion_service.accept,
            suggestion_id,
            fallback_local=request.fallback_local if request is not None else False,
        )
    except suggestion_service.SuggestionExpiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "suggestion_expired"}
        ) from exc
    except NoWritableAccountError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "no_writable_account"}
        ) from exc
    except ValueError as exc:
        status = 404 if str(exc) == "suggestion 不存在" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except (GraphHTTPError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: str):
    try:
        return await run_sync(suggestion_service.dismiss, suggestion_id)
    except ValueError as exc:
        status = 404 if str(exc) == "suggestion 不存在" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
