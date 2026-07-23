"""Microsoft Outlook calendar authentication, cache, and write-through API."""

from __future__ import annotations

import asyncio
import threading
from datetime import date as Date, time as Time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from api.deps import run_sync
from core import goal_schedule_service
from core.graph.auth import (
    DEFAULT_GRAPH_CLIENT_ID,
    GraphAuth,
    GraphAuthError,
    GraphNotConfiguredError,
)
from core.graph.client import GraphHTTPError
from core.schedule_service import (
    NoWritableAccountError,
    ScheduleEventNotFoundError,
    ScheduleNotConnectedError,
    ScheduleService,
)

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
    account_id: str | None = Field(default=None, min_length=1, max_length=200)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=200)


class DeleteLocalAccountRequest(BaseModel):
    delete_events: bool


class LocalMigrationRequest(BaseModel):
    decisions: dict[str, Literal["skip", "create"]] = Field(default_factory=dict)


class UpdateEventRequest(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    date: Date | None = None
    start_time: Time | None = None
    end_time: Time | None = None
    all_day: bool | None = None


_AUTH_NOT_STARTED = {"status": "error", "error": "尚未启动 Microsoft 登录"}
_auth_task: asyncio.Task[None] | None = None
_auth_cancel: threading.Event | None = None
_auth_state: dict[str, Any] = dict(_AUTH_NOT_STARTED)
_auth_busy = False
_auth_generation = 0
_auth_resetting = False


@router.get("/status")
async def get_status():
    return await run_sync(ScheduleService().status)


@router.get("/accounts")
async def list_accounts():
    return await run_sync(ScheduleService().list_accounts)


@router.post("/accounts/local")
async def create_local_account():
    try:
        return await run_sync(ScheduleService().create_local_account)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/accounts/local")
async def delete_local_account(request: DeleteLocalAccountRequest):
    try:
        deleted = await run_sync(
            ScheduleService().delete_local_account,
            delete_events=request.delete_events,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "deleted_events": deleted}


@router.post("/accounts/local/migration/preview")
async def preview_local_migration():
    try:
        return await run_sync(ScheduleService().migration_preview)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/accounts/local/migration")
async def migrate_local_events(request: LocalMigrationRequest | None = None):
    try:
        return await run_sync(
            ScheduleService().migrate_local_events,
            request.decisions if request is not None else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/accounts/local/migration/dismiss")
async def dismiss_local_migration():
    try:
        await run_sync(ScheduleService().dismiss_migration_prompt)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/auth/client-id")
async def save_client_id(request: ClientIdRequest):
    auth = GraphAuth()
    try:
        current_client_id = await run_sync(auth.client_id)
        next_client_id = request.client_id.strip()
        if current_client_id != next_client_id:
            _begin_auth_reset()
            try:
                await cancel_auth_login()
                await run_sync(ScheduleService(auth=auth).logout)
                await run_sync(auth.set_client_id, next_client_id)
            finally:
                _end_auth_reset()
        else:
            await run_sync(auth.set_client_id, next_client_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await run_sync(auth.client_id_info)


@router.get("/auth/client-id")
async def get_client_id():
    return await run_sync(GraphAuth().client_id_info)


@router.delete("/auth/client-id")
async def restore_default_client_id():
    auth = GraphAuth()
    current_client_id = await run_sync(auth.client_id)
    custom_client_id = await run_sync(auth.custom_client_id)
    if custom_client_id is None:
        return await run_sync(auth.client_id_info)
    if current_client_id != DEFAULT_GRAPH_CLIENT_ID:
        _begin_auth_reset()
        try:
            await cancel_auth_login()
            await run_sync(ScheduleService(auth=auth).logout)
            await run_sync(auth.clear_client_id)
        finally:
            _end_auth_reset()
    else:
        await run_sync(auth.clear_client_id)
    return await run_sync(auth.client_id_info)


@router.post("/auth/interactive-start")
async def start_interactive_login():
    global _auth_task
    generation, cancel_event = _reserve_auth_flow()
    auth = GraphAuth()
    _auth_task = asyncio.create_task(
        _complete_interactive_login(auth, cancel_event, generation)
    )
    return {"status": "pending"}


@router.post("/auth/device-start")
async def start_device_login():
    global _auth_task
    generation, cancel_event = _reserve_auth_flow()
    auth = GraphAuth()
    try:
        flow = await run_sync(auth.start_device_flow)
    except GraphNotConfiguredError as exc:
        _set_auth_state(generation, {"status": "error", "error": str(exc)})
        _release_auth_flow(generation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GraphAuthError as exc:
        _set_auth_state(generation, {"status": "error", "error": str(exc)})
        _release_auth_flow(generation)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        _set_auth_state(
            generation,
            {"status": "error", "error": "无法启动 Microsoft 设备码登录"},
        )
        _release_auth_flow(generation)
        raise HTTPException(status_code=502, detail="无法启动 Microsoft 设备码登录") from exc
    if not _auth_flow_is_current(generation, cancel_event):
        raise HTTPException(status_code=409, detail="Microsoft 登录已取消")
    _auth_task = asyncio.create_task(
        _complete_device_login(auth, flow, cancel_event, generation)
    )
    return {
        "user_code": flow["user_code"],
        "verification_uri": flow.get("verification_uri") or flow.get("verification_url"),
        "expires_in": flow.get("expires_in"),
    }


@router.get("/auth/status")
@router.get("/auth/device-status")
async def get_auth_login_status():
    return dict(_auth_state)


@router.post("/auth/logout")
async def logout():
    _begin_auth_reset()
    try:
        await cancel_auth_login()
        await run_sync(ScheduleService().logout)
    finally:
        _end_auth_reset()
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
            account_id=request.account_id,
            client_request_id=request.client_request_id,
        )
    except goal_schedule_service.GoalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoWritableAccountError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "no_writable_account"}
        ) from exc
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
    except ScheduleEventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
    generation: int,
) -> None:
    try:
        account = await run_sync(
            auth.complete_device_flow,
            flow,
            exit_condition=cancel_event.is_set,
        )
        if not _auth_flow_is_current(generation, cancel_event):
            return
        await _sync_after_login(auth)
        if _auth_flow_is_current(generation, cancel_event):
            _set_auth_state(generation, {"status": "ok", "account": account})
    except asyncio.CancelledError:
        raise
    except GraphAuthError as exc:
        _set_auth_state(generation, {"status": "error", "error": str(exc)})
    except Exception:
        _set_auth_state(generation, {"status": "error", "error": "设备码登录失败"})
    finally:
        _release_auth_flow(generation)


async def _complete_interactive_login(
    auth: GraphAuth,
    cancel_event: threading.Event,
    generation: int,
) -> None:
    try:
        account = await run_sync(
            auth.complete_interactive_flow,
            exit_condition=cancel_event.is_set,
        )
        if not _auth_flow_is_current(generation, cancel_event):
            return
        await _sync_after_login(auth)
        if _auth_flow_is_current(generation, cancel_event):
            _set_auth_state(generation, {"status": "ok", "account": account})
    except asyncio.CancelledError:
        raise
    except GraphAuthError as exc:
        _set_auth_state(
            generation,
            {"status": "error", "error": str(exc), "fallback": "device_code"},
        )
    except Exception:
        _set_auth_state(
            generation,
            {
                "status": "error",
                "error": "Microsoft 浏览器登录失败",
                "fallback": "device_code",
            },
        )
    finally:
        _release_auth_flow(generation)


async def _sync_after_login(auth: GraphAuth) -> None:
    try:
        await run_sync(ScheduleService(auth=auth).sync)
    except Exception:
        pass


def _reserve_auth_flow() -> tuple[int, threading.Event]:
    global _auth_busy, _auth_cancel, _auth_generation, _auth_state
    if _auth_busy or _auth_resetting:
        raise HTTPException(status_code=409, detail="Microsoft 登录正在进行中")
    _auth_busy = True
    _auth_generation += 1
    _auth_cancel = threading.Event()
    _auth_state = {"status": "pending"}
    return _auth_generation, _auth_cancel


def _auth_flow_is_current(generation: int, cancel_event: threading.Event) -> bool:
    return generation == _auth_generation and not cancel_event.is_set()


def _set_auth_state(generation: int, state: dict[str, Any]) -> None:
    global _auth_state
    if generation == _auth_generation:
        _auth_state = state


def _release_auth_flow(generation: int) -> None:
    global _auth_busy, _auth_cancel, _auth_task
    if generation != _auth_generation:
        return
    _auth_busy = False
    _auth_cancel = None
    _auth_task = None


def _begin_auth_reset() -> None:
    global _auth_resetting
    if _auth_resetting:
        raise HTTPException(status_code=409, detail="Microsoft 登录状态正在更新")
    _auth_resetting = True


def _end_auth_reset() -> None:
    global _auth_resetting
    _auth_resetting = False


async def cancel_auth_login() -> None:
    global _auth_busy, _auth_cancel, _auth_generation, _auth_state, _auth_task
    task = _auth_task
    cancel_event = _auth_cancel
    _auth_generation += 1
    _auth_task = None
    _auth_cancel = None
    _auth_busy = False
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
    _auth_state = dict(_AUTH_NOT_STARTED)


async def cancel_device_login() -> None:
    """Compatibility wrapper for shutdown callers introduced with device flow."""
    await cancel_auth_login()
