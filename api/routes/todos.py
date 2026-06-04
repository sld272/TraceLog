"""Todo routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.deps import run_sync
from core.app_services import todo_editor

router = APIRouter(prefix="/todos", tags=["todos"])


class UpdateTodoRequest(BaseModel):
    task: str | None = None
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    status: str | None = None


class CreateTodoRequest(BaseModel):
    task: str
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    status: str | None = None


@router.get("")
async def list_todos():
    return await run_sync(todo_editor.list_todos)


@router.post("")
async def create_todo(request: CreateTodoRequest):
    try:
        changes: dict[str, Any] = request.model_dump(exclude_unset=True)
        return await run_sync(todo_editor.create_todo, changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{todo_id}")
async def update_todo(todo_id: str, request: UpdateTodoRequest):
    try:
        changes: dict[str, Any] = request.model_dump(exclude_unset=True)
        todo = await run_sync(todo_editor.update_todo, todo_id, changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if todo is None:
        raise HTTPException(status_code=404, detail="todo not found")
    return todo


@router.delete("/{todo_id}")
async def delete_todo(todo_id: str):
    deleted = await run_sync(todo_editor.delete_todo, todo_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="todo not found")
    return {"ok": True}
