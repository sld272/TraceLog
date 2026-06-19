"""Goaltool CRUD routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import run_sync
from core import goal_service

router = APIRouter(prefix="/goals", tags=["goals"])


class CreateGoalRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    detail: str | None = Field(default=None, max_length=4000)
    horizon: Literal["short", "long"]
    focus: bool = False


class UpdateGoalRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    detail: str | None = Field(default=None, max_length=4000)
    horizon: Literal["short", "long"] | None = None
    status: Literal["active", "done", "abandoned", "paused"] | None = None
    focus: bool | None = None


@router.get("")
async def list_goals(
    status: Literal["active", "done", "abandoned", "paused"] | None = Query(default=None),
    horizon: Literal["short", "long"] | None = Query(default=None),
):
    return await run_sync(goal_service.list_goals, status=status, horizon=horizon)


@router.post("")
async def create_goal(request: CreateGoalRequest):
    try:
        return await run_sync(
            goal_service.create_goal,
            request.title,
            request.detail,
            request.horizon,
            focus=request.focus,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{goal_id}")
async def update_goal(goal_id: str, request: UpdateGoalRequest):
    try:
        goal = await run_sync(
            goal_service.update_goal,
            goal_id,
            **request.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return goal


@router.post("/{goal_id}/progress")
async def mark_goal_progress(goal_id: str):
    goal = await run_sync(goal_service.mark_progress, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return goal


@router.delete("/{goal_id}")
async def delete_goal(goal_id: str):
    deleted = await run_sync(goal_service.delete_goal, goal_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return {"ok": True}
