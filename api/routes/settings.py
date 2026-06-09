"""Settings and local workspace status routes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api import deps
from api.deps import run_sync
from core import db, record_service, vector_index_service, vectorstore, vision_service, web_search_service
from core.cli.config import CONFIG_FILE, default_vision_config, default_web_search_config, normalize_vision_config, normalize_web_search_config
from core.logging_service import default_config as default_logging_config
from core.logging_service import normalize_config as normalize_logging_config

router = APIRouter(prefix="/settings", tags=["settings"])


class LoggingSettings(BaseModel):
    enabled: bool = True
    level: str = "INFO"
    history_retention: int = Field(default=5, ge=0, le=100)


class VisionSettings(BaseModel):
    enabled: bool = False
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class WebSearchSettings(BaseModel):
    enabled: bool = False
    provider: Literal["tavily", "duckduckgo"] = "duckduckgo"
    tavily_api_key: str | None = None
    max_results: int = Field(default=5, ge=1, le=8)
    timeout_s: int = Field(default=8, ge=3, le=20)
    cache_ttl_s: int = Field(default=1800, ge=0, le=86400)


class ModelSettingsRequest(BaseModel):
    api_key: str | None = None
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    reuse_embedding_config: bool = False
    logging: LoggingSettings | None = None
    vision: VisionSettings | None = None
    web_search: WebSearchSettings | None = None


@router.get("/model")
async def get_model_settings():
    return await run_sync(_read_model_settings)


@router.put("/model")
async def update_model_settings(request: ModelSettingsRequest):
    try:
        result = await run_sync(_write_model_settings, request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        runtime = await deps.reload_runtime()
        result["runtime_reloaded"] = runtime.configured
        result["restart_required"] = False
        result["config_reloaded"] = runtime.configured
    except Exception as exc:
        result["runtime_reloaded"] = False
        result["restart_required"] = False
        result["config_reloaded"] = False
        result["reload_error"] = str(exc)
    return result


@router.get("/workspace")
async def get_workspace_status():
    return await run_sync(_workspace_status)


@router.post("/vector-index/retry")
async def retry_vector_index():
    processed = await run_sync(record_service.retry_pending_vector_docs)
    return {"processed": processed, "vector_index": await run_sync(_vector_index_status)}


@router.post("/vector-index/reconcile")
async def reconcile_vector_index():
    processed = await run_sync(record_service.reindex_all_vector_docs)
    return {"processed": processed, "vector_index": await run_sync(_vector_index_status)}


def _read_model_settings() -> dict[str, Any]:
    config = _load_config_file()
    logging_config = normalize_logging_config(config.get("logging"))
    vision = normalize_vision_config(config.get("vision"))
    web_search = normalize_web_search_config(config.get("web_search"))
    effective_vision = vision_service.configured_status(config)
    effective_web_search = web_search_service.configured_status(config)
    return {
        "configured": _is_configured(config),
        "has_api_key": bool(config.get("api_key")),
        "api_key_masked": _mask_secret(config.get("api_key")),
        "base_url": config.get("base_url", "https://api.openai.com/v1"),
        "model": config.get("model", "gpt-4o-mini"),
        "embedding_model": config.get("embedding_model", "text-embedding-3-small"),
        "has_embedding_api_key": bool(config.get("embedding_api_key")),
        "embedding_api_key_masked": _mask_secret(config.get("embedding_api_key")),
        "embedding_base_url": config.get("embedding_base_url"),
        "reuse_embedding_config": not bool(config.get("embedding_api_key") or config.get("embedding_base_url")),
        "logging": logging_config,
        "vision": {
            "enabled": bool(vision.get("enabled")),
            "configured": bool(effective_vision.get("configured")),
            "model": vision.get("model"),
            "has_api_key": bool(vision.get("api_key")),
            "api_key_masked": _mask_secret(vision.get("api_key")),
            "base_url": vision.get("base_url"),
            "effective_base_url": effective_vision.get("base_url"),
            "prompt_version": effective_vision.get("prompt_version"),
            "timeout_s": effective_vision.get("timeout_s"),
        },
        "web_search": {
            "enabled": bool(web_search.get("enabled")),
            "configured": bool(effective_web_search.get("configured")),
            "provider": web_search.get("provider"),
            "selected_provider": effective_web_search.get("selected_provider"),
            "tavily_configured": bool(effective_web_search.get("tavily_configured")),
            "duckduckgo_available": bool(effective_web_search.get("duckduckgo_available")),
            "has_tavily_api_key": bool(web_search.get("tavily_api_key")),
            "tavily_api_key_masked": _mask_secret(web_search.get("tavily_api_key")),
            "max_results": int(web_search.get("max_results", 5)),
            "timeout_s": int(web_search.get("timeout_s", 8)),
            "cache_ttl_s": int(web_search.get("cache_ttl_s", 1800)),
        },
        "config_path": str(Path(CONFIG_FILE).resolve()),
    }


def _write_model_settings(payload: dict[str, Any]) -> dict[str, Any]:
    existing = _load_config_file()

    api_key = _clean_optional(payload.get("api_key")) or existing.get("api_key")
    if not api_key:
        raise ValueError("API Key 不能为空")

    if payload.get("reuse_embedding_config"):
        embedding_api_key = None
        embedding_base_url = None
    else:
        embedding_api_key = _clean_optional(payload.get("embedding_api_key"))
        if embedding_api_key is None:
            embedding_api_key = existing.get("embedding_api_key")
        embedding_base_url = _clean_optional(payload.get("embedding_base_url"))

    incoming_vision = normalize_vision_config(payload.get("vision"))
    existing_vision = normalize_vision_config(existing.get("vision"))
    if incoming_vision.get("api_key") is None:
        incoming_vision["api_key"] = existing_vision.get("api_key")

    incoming_web_search = normalize_web_search_config(payload.get("web_search"))
    existing_web_search = normalize_web_search_config(existing.get("web_search"))
    if incoming_web_search.get("tavily_api_key") is None:
        incoming_web_search["tavily_api_key"] = existing_web_search.get("tavily_api_key")

    config = {
        **{key: value for key, value in existing.items() if key != "job_worker_concurrency"},
        "api_key": api_key,
        "base_url": str(payload["base_url"]).strip(),
        "model": str(payload["model"]).strip(),
        "embedding_model": str(payload["embedding_model"]).strip(),
        "embedding_api_key": embedding_api_key,
        "embedding_base_url": embedding_base_url,
        "logging": normalize_logging_config(payload.get("logging")),
        "vision": incoming_vision,
        "web_search": incoming_web_search,
    }

    missing = [key for key in ("api_key", "base_url", "model", "embedding_model") if not config.get(key)]
    if missing:
        raise ValueError(f"缺少必要配置：{', '.join(missing)}")

    _atomic_write_json(Path(CONFIG_FILE), config)
    result = _read_model_settings()
    result["restart_required"] = True
    return result


def _workspace_status() -> dict[str, Any]:
    logs_dir = db.WORKSPACE_DIR / "logs"
    history_dir = logs_dir / "history"
    current_log = logs_dir / "current.jsonl"

    return {
        "workspace_path": str(db.WORKSPACE_DIR),
        "workspace_exists": db.WORKSPACE_DIR.exists(),
        "db_path": str(db.DB_PATH),
        "db_exists": db.DB_PATH.exists(),
        "db_size_bytes": _path_size(db.DB_PATH),
        "souls_dir": str(db.WORKSPACE_DIR / "souls"),
        "soul_memories_dir": str(db.WORKSPACE_DIR / "soul_memories"),
        "user_memory_path": str(db.WORKSPACE_DIR / "user.md"),
        "counts": {
            "posts": _count_table("posts"),
            "comments": _count_table("comments"),
            "souls": _count_table("souls"),
            "enabled_souls": _count_enabled_souls(),
            "todos": _count_table("todos"),
            "jobs": _count_table("jobs"),
            "vision_cache": _count_table("vision_cache"),
        },
        "web_search": web_search_service.configured_status(_load_config_file()),
        "vector_index": _vector_index_status(),
        "logs": {
            "current_log_path": str(current_log),
            "current_log_exists": current_log.exists(),
            "current_log_size_bytes": _path_size(current_log),
            "history_dir": str(history_dir),
            "history_count": len(list(history_dir.glob("*.jsonl"))) if history_dir.exists() else 0,
        },
    }


def _vector_index_status() -> dict[str, Any]:
    state = vector_index_service.current_collection_state()
    return {
        "collection_name": vectorstore.current_collection_name(),
        "embedding_config_hash": vectorstore.current_embedding_config_hash(),
        "source_revision": vector_index_service.current_source_revision(),
        "synced_revision": state.synced_revision if state is not None else 0,
        "ready": state.query_ready if state is not None else False,
        "pending_count": state.pending_count if state is not None else 0,
        "failed_count": state.failed_count if state is not None else 0,
        "missing_count": state.missing_count if state is not None else 0,
        "stale_count": state.stale_count if state is not None else 0,
    }


def _load_config_file() -> dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {
            "logging": default_logging_config(),
            "vision": default_vision_config(),
            "web_search": default_web_search_config(),
        }
    except json.JSONDecodeError as exc:
        raise ValueError(f"{CONFIG_FILE} 不是有效 JSON") from exc
    return data if isinstance(data, dict) else {}


def _is_configured(config: dict[str, Any]) -> bool:
    return all(config.get(key) for key in ("api_key", "base_url", "model", "embedding_model"))


def _mask_secret(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    if len(text) <= 8:
        return "••••"
    return f"{text[:4]}…{text[-4:]}"


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_concurrency(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(number, 4))


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _count_table(table: str) -> int:
    row = db.query_one(f"SELECT COUNT(*) AS count FROM {table}")
    return int(row["count"]) if row is not None else 0


def _count_enabled_souls() -> int:
    row = db.query_one("SELECT COUNT(*) AS count FROM souls WHERE enabled = 1")
    return int(row["count"]) if row is not None else 0
