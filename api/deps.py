"""API runtime dependencies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, TypeVar

from openai import OpenAI
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from core import db, logging_service, memory_events_service, memory_unit_service, record_service, schedule_service, vector_index_service, vectorstore, workspace_service
from core.app_services import job_service
from core.app_services.api_runtime import ApiRuntime, JobWorker
from core.cli.config import CONFIG_FILE, normalize_vision_config, normalize_web_search_config
from core.llm import secondary_model
from core.logging_service import normalize_config as normalize_logging_settings

T = TypeVar("T")

_runtime: ApiRuntime | None = None
_schedule_sync_task: asyncio.Task[None] | None = None
MODEL_NOT_CONFIGURED_MESSAGE = "请先在设置页完成模型配置"
SCHEDULE_SYNC_INTERVAL_SECONDS = 15 * 60


async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run synchronous core work outside the event loop."""
    return await run_in_threadpool(func, *args, **kwargs)


def get_runtime() -> ApiRuntime:
    if _runtime is None:
        raise RuntimeError("API runtime is not initialized")
    return _runtime


def require_configured_runtime() -> ApiRuntime:
    runtime = get_runtime()
    if not runtime.configured or runtime.client is None or runtime.model is None:
        raise RuntimeError(MODEL_NOT_CONFIGURED_MESSAGE)
    return runtime


def require_configured_runtime_or_409() -> ApiRuntime:
    try:
        return require_configured_runtime()
    except RuntimeError as exc:
        if str(exc) == MODEL_NOT_CONFIGURED_MESSAGE:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise


async def init_runtime() -> ApiRuntime:
    """Initialize workspace, vectorstore, LLM client, and the API worker."""
    global _runtime
    config = _load_api_config(strict=False)
    logging_service.init_logging(config.get("logging"))
    workspace_service.migrate_workspace_permissions()
    workspace_service.init_workspace()
    _start_schedule_sync_task()

    if not _is_model_configured(config):
        _runtime = ApiRuntime(
            config=config,
            client=None,
            model=None,
            worker=None,
            vectorstore_initialized=False,
            configured=False,
        )
        return _runtime

    _runtime = _start_runtime(_build_configured_runtime(config))
    return _runtime


async def reload_runtime() -> ApiRuntime:
    """Reload runtime after settings are saved, keeping the previous runtime on failure."""
    global _runtime
    previous_runtime = _runtime
    config = _load_api_config(strict=False)
    logging_service.update_config(config.get("logging"))
    workspace_service.migrate_workspace_permissions()
    workspace_service.init_workspace()
    next_runtime = (
        _unconfigured_runtime(config)
        if not _is_model_configured(config)
        else _build_configured_runtime(config)
    )
    if previous_runtime is not None and previous_runtime.worker is not None:
        await previous_runtime.worker.stop()
    _runtime = next_runtime
    _start_runtime(_runtime)
    return _runtime


def _unconfigured_runtime(config: dict) -> ApiRuntime:
    secondary_model.reset()
    return ApiRuntime(
        config=config,
        client=None,
        model=None,
        worker=None,
        vectorstore_initialized=False,
        configured=False,
    )


def _build_configured_runtime(config: dict) -> ApiRuntime:
    vectorstore_initialized = False
    try:
        init_result = vectorstore.init_vectorstore(
            config["api_key"],
            config["base_url"],
            config["embedding_model"],
            config.get("embedding_base_url"),
            config.get("embedding_api_key"),
        )
        vectorstore_initialized = True
        vector_index_service.ensure_collection(
            collection_name=init_result.collection_name,
            embedding_config_hash=vectorstore.current_embedding_config_hash() or "",
            embedding_model=config["embedding_model"],
            embedding_base_url=config.get("embedding_base_url") or config["base_url"],
        )
        record_service.reindex_all_vector_docs()
        record_service.retry_pending_vector_docs()
    except vectorstore.VectorStoreInitError as exc:
        logging_service.log_event("vectorstore_init_failed", level="ERROR", error=str(exc))

    client = OpenAI(api_key=config["api_key"], base_url=config.get("base_url", "https://api.openai.com/v1"))
    secondary_model.install_from_config(
        config,
        main_client=client,
        client_factory=lambda api_key, base_url: OpenAI(api_key=api_key, base_url=base_url),
    )
    worker = JobWorker(client, config["model"], concurrency=_job_worker_concurrency(config))
    runtime = ApiRuntime(
        config=config,
        client=client,
        model=config["model"],
        worker=worker,
        vectorstore_initialized=vectorstore_initialized,
        configured=True,
    )
    return runtime


def _start_runtime(runtime: ApiRuntime) -> ApiRuntime:
    if runtime.worker is not None:
        _enqueue_startup_retries()
        runtime.worker.start()
    return runtime


def _start_configured_runtime(config: dict) -> ApiRuntime:
    """Build and start a configured runtime.

    Kept as a small compatibility wrapper for tests and ad-hoc callers; reload
    uses the build/start split so the old worker can stop before the new one
    resets interrupted jobs.
    """
    return _start_runtime(_build_configured_runtime(config))


async def shutdown_runtime() -> None:
    global _runtime, _schedule_sync_task
    task = _schedule_sync_task
    _schedule_sync_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    from api.routes import schedule as schedule_routes

    await schedule_routes.cancel_device_login()
    if _runtime is not None and _runtime.worker is not None:
        await _runtime.worker.stop()
    _runtime = None
    secondary_model.reset()


def _start_schedule_sync_task() -> None:
    global _schedule_sync_task
    if _schedule_sync_task is None or _schedule_sync_task.done():
        _schedule_sync_task = asyncio.create_task(_schedule_sync_loop())


async def _schedule_sync_loop() -> None:
    while True:
        try:
            await run_sync(schedule_service.ScheduleService().sync)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging_service.log_event(
                "schedule_sync_failed",
                level="WARNING",
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(SCHEDULE_SYNC_INTERVAL_SECONDS)


def _load_api_config(*, strict: bool = True) -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError as exc:
        if not strict:
            return _default_api_config()
        raise RuntimeError(f"API 模式需要先配置 {CONFIG_FILE}") from exc

    required_keys = ("api_key", "base_url", "model", "embedding_model")
    missing = [key for key in required_keys if not config.get(key)]
    if missing and strict:
        raise RuntimeError(f"{CONFIG_FILE} 缺少必要配置：{', '.join(missing)}")
    config.setdefault("embedding_api_key", None)
    config.setdefault("embedding_base_url", None)
    config.setdefault("secondary_model", None)
    config.setdefault("secondary_api_key", None)
    config.setdefault("secondary_base_url", None)
    config["logging"] = normalize_logging_settings(config.get("logging"))
    config["vision"] = normalize_vision_config(config.get("vision"))
    config["web_search"] = normalize_web_search_config(config.get("web_search"))
    return config


def _default_api_config() -> dict:
    return {
        "logging": normalize_logging_settings(None),
        "vision": normalize_vision_config(None),
        "web_search": normalize_web_search_config(None),
    }


def _is_model_configured(config: dict) -> bool:
    return all(config.get(key) for key in ("api_key", "base_url", "model", "embedding_model"))


def _job_worker_concurrency(config: dict) -> int:
    try:
        value = int(config.get("job_worker_concurrency", 1))
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, 4))


def _enqueue_startup_retries() -> None:
    record_service.retry_pending_vector_docs()

    if (
        memory_events_service.buckets_with_pending_events(limit_buckets=1)
        or memory_unit_service.list_pending_relinks()
    ):
        job_service.enqueue_memory_reconcile_once({"trigger": "startup_memory_repair"})
