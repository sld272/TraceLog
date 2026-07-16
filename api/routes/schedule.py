"""Microsoft Outlook calendar authentication, cache, and write-through API."""

from __future__ import annotations

import asyncio
import threading
from datetime import date as Date, time as Time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from api.deps import run_sync
from core import goal_schedule_service
from core.graph.auth import GraphAuth, GraphAuthError, GraphNotConfiguredError
from core.graph.client import GraphHTTPError
from core.schedule_service import ScheduleNotConnectedError, ScheduleService

router = APIRouter(prefix="/schedule", tags=["schedule"])


class ClientIdRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=200)


class CreateEventRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    date: Date
    start_time: Time | None = None
    end_time: Time | None = None
    all_day: bool = False
    goal_id: str | None = None


class UpdateEventRequest(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    date: Date | None = None
    start_time: Time | None = None
    end_time: Time | None = None
    all_day: bool | None = None


_device_task: asyncio.Task[None] | None = None
_device_cancel: threading.Event | None = None
_device_state: dict[str, Any] = {"status": "error", "error": "尚未启动设备码登录"}


@router.get("/status")
async def get_status():
    return await run_sync(ScheduleService().status)


@router.post("/auth/client-id")
async def save_client_id(request: ClientIdRequest):
    auth = GraphAuth()
    try:
        current_client_id = await run_sync(auth.client_id)
        if current_client_id is not None and current_client_id != request.client_id.strip():
            await cancel_device_login()
            await run_sync(ScheduleService(auth=auth).logout)
        await run_sync(auth.set_client_id, request.client_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await run_sync(auth.client_id_info)


@router.get("/auth/client-id")
async def get_client_id():
    return await run_sync(GraphAuth().client_id_info)


@router.post("/auth/device-start")
async def start_device_login():
    global _device_cancel, _device_task, _device_state
    if _device_task is not None and not _device_task.done():
        raise HTTPException(status_code=409, detail="设备码登录正在进行中")
    auth = GraphAuth()
    try:
        flow = await run_sync(auth.start_device_flow)
    except GraphNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GraphAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _device_state = {"status": "pending"}
    _device_cancel = threading.Event()
    _device_task = asyncio.create_task(_complete_device_login(auth, flow, _device_cancel))
    return {
        "user_code": flow["user_code"],
        "verification_uri": flow.get("verification_uri") or flow.get("verification_url"),
        "expires_in": flow.get("expires_in"),
    }


@router.get("/auth/device-status")
async def get_device_login_status():
    return dict(_device_state)


@router.post("/auth/logout")
async def logout():
    await cancel_device_login()
    await run_sync(ScheduleService().logout)
    return {"ok": True}


@router.get("/events")
async def list_events(start: Date, end: Date, response: Response):
    try:
        result = await run_sync(ScheduleService().list_events, start, end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.headers["X-Schedule-Configured"] = str(result["configured"]).lower()
    response.headers["X-Schedule-Connected"] = str(result["connected"]).lower()
    return result["events"]


@router.post("/events")
async def create_event(request: CreateEventRequest):
    try:
        return await run_sync(
            ScheduleService().create_event,
            subject=request.subject,
            event_date=request.date,
            start_time=request.start_time,
            end_time=request.end_time,
            all_day=request.all_day,
            goal_id=request.goal_id,
        )
    except goal_schedule_service.GoalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ScheduleNotConnectedError, GraphNotConfiguredError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (GraphHTTPError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.patch("/events/{event_id}")
async def update_event(event_id: str, request: UpdateEventRequest):
    changes = request.model_dump(exclude_unset=True)
    try:
        return await run_sync(ScheduleService().update_event, event_id, changes)
    except ScheduleNotConnectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (GraphHTTPError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/events/{event_id}")
async def delete_event(event_id: str):
    try:
        await run_sync(ScheduleService().delete_event, event_id)
    except ScheduleNotConnectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (GraphHTTPError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/sync")
async def sync_schedule():
    try:
        return await run_sync(ScheduleService().sync)
    except (GraphHTTPError, httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _complete_device_login(
    auth: GraphAuth,
    flow: dict[str, Any],
    cancel_event: threading.Event,
) -> None:
    global _device_state
    try:
        account = await run_sync(
            auth.complete_device_flow,
            flow,
            exit_condition=cancel_event.is_set,
        )
        _device_state = {"status": "ok", "account": account}
        try:
            await run_sync(ScheduleService(auth=auth).sync)
        except (GraphHTTPError, httpx.HTTPError, ValueError):
            pass
    except asyncio.CancelledError:
        raise
    except GraphAuthError as exc:
        _device_state = {"status": "error", "error": str(exc)}
    except Exception:
        _device_state = {"status": "error", "error": "设备码登录失败"}


async def cancel_device_login() -> None:
    global _device_cancel, _device_task, _device_state
    task = _device_task
    _device_task = None
    cancel_event = _device_cancel
    _device_cancel = None
    if cancel_event is not None:
        cancel_event.set()
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _device_state = {"status": "error", "error": "尚未启动设备码登录"}
