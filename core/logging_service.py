"""Local JSONL logging for TraceLog runtime events."""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from core import db

DEFAULT_LOGGING_CONFIG = {
    "enabled": True,
    "level": "INFO",
    "preview_chars": 300,
    "history_retention": 5,
}

_lock = threading.RLock()
_enabled = False
_level = logging.INFO
_preview_chars = 300
_history_retention = 5
_current_log_path: Path | None = None
_LOGGING_CONFIG_KEYS = set(DEFAULT_LOGGING_CONFIG)

_SENSITIVE_KEYS = {"api_key", "authorization", "password", "secret", "token"}
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE),
]


def default_config() -> dict:
    """Return a fresh default logging config."""
    return dict(DEFAULT_LOGGING_CONFIG)


def normalize_config(config: dict | None) -> dict:
    """Merge user logging config with safe defaults."""
    raw = config if isinstance(config, dict) else {}
    merged = default_config()
    merged.update(
        {
            key: value
            for key, value in raw.items()
            if key in _LOGGING_CONFIG_KEYS and value is not None
        }
    )

    try:
        merged["preview_chars"] = max(0, int(merged.get("preview_chars", 300)))
    except (TypeError, ValueError):
        merged["preview_chars"] = 300

    try:
        merged["history_retention"] = max(0, int(merged.get("history_retention", 5)))
    except (TypeError, ValueError):
        merged["history_retention"] = 5

    level_name = str(merged.get("level", "INFO")).upper()
    merged["level"] = level_name if hasattr(logging, level_name) else "INFO"
    merged["enabled"] = bool(merged.get("enabled", True))
    return merged


def init_logging(config: dict | None = None) -> None:
    """Initialize JSONL logging and rotate the previous current log."""
    global _enabled, _level, _preview_chars, _history_retention, _current_log_path

    settings = normalize_config(config)
    with _lock:
        _enabled = bool(settings["enabled"])
        _level = int(getattr(logging, settings["level"], logging.INFO))
        _preview_chars = settings["preview_chars"]
        _history_retention = settings["history_retention"]
        _current_log_path = db.WORKSPACE_DIR / "logs" / "current.jsonl"

        if not _enabled:
            return

        try:
            log_dir = _current_log_path.parent
            history_dir = log_dir / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            if _current_log_path.exists() and _current_log_path.stat().st_size > 0:
                archived = _unique_history_path(history_dir)
                shutil.move(str(_current_log_path), str(archived))
            _prune_history(history_dir, _history_retention)
            _current_log_path.touch(exist_ok=True)
        except OSError:
            _enabled = False
            _current_log_path = None


def get_logger(name: str) -> logging.Logger:
    """Return a stdlib logger under the TraceLog namespace."""
    return logging.getLogger(f"tracelog.{name}")


def log_event(event: str, level: str = "INFO", **fields: Any) -> None:
    """Write one structured runtime event."""
    numeric_level = int(getattr(logging, level.upper(), logging.INFO))
    if numeric_level < _level:
        return
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "level": level.upper(),
        "event": event,
        **fields,
    }
    _write_jsonl(payload)


def log_llm_call(
    *,
    call_id: str,
    operation: str,
    model: str,
    status: str,
    duration_ms: int,
    timeout_s: int | float | None,
    messages: list[dict[str, Any]],
    response_content: str | None = None,
    parsed: Any = None,
    error: Any = None,
    context: dict | None = None,
    response_format: dict | None = None,
) -> None:
    """Write one LLM call log entry with full local debug payload."""
    request_payload = {
        "model": model,
        "timeout": timeout_s,
        "response_format": response_format,
        "messages": messages,
    }
    response_payload = {
        "content": response_content,
        "content_length": len(response_content or ""),
        "content_stripped_length": len((response_content or "").strip()),
    }

    fields: dict[str, Any] = {
        "call_id": call_id,
        "operation": operation,
        "model": model,
        "status": status,
        "duration_ms": duration_ms,
        "timeout_s": timeout_s,
        "context": context or {},
        "request": request_payload,
        "response": response_payload,
    }
    if parsed is not None:
        fields["parsed"] = parsed
    if error:
        fields["error"] = error

    level = "ERROR" if status == "api_error" else "WARNING" if status != "ok" else "INFO"
    log_event("llm_call", level=level, **fields)


def _write_jsonl(payload: dict[str, Any]) -> None:
    if not _enabled or _current_log_path is None:
        return
    try:
        clean_payload = _redact(payload)
        line = json.dumps(clean_payload, ensure_ascii=False, default=str)
        with _lock:
            if _current_log_path is None:
                return
            with _current_log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        pass


def _unique_history_path(history_dir: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    candidate = history_dir / f"{stamp}.jsonl"
    if not candidate.exists():
        return candidate
    for index in range(1, 1000):
        candidate = history_dir / f"{stamp}-{index:03d}.jsonl"
        if not candidate.exists():
            return candidate
    return history_dir / f"{stamp}-{int(perf_counter() * 1000)}.jsonl"


def _prune_history(history_dir: Path, retention: int) -> None:
    files = sorted(
        (path for path in history_dir.glob("*.jsonl") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    for old_file in files[retention:]:
        try:
            old_file.unlink()
        except OSError:
            pass


def _summarize_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    return {
        "role": message.get("role"),
        "content_preview": _preview(content),
        "content_length": len(content) if isinstance(content, str) else None,
    }


def _summarize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return {"preview": _preview(value), "length": len(value)}
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    if isinstance(value, dict):
        return {
            key: _summarize_value(item)
            for key, item in value.items()
        }
    return {"type": type(value).__name__}


def _preview(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if _preview_chars <= 0:
        return ""
    text = value.replace("\r\n", "\n")
    if len(text) <= _preview_chars:
        return text
    return text[:_preview_chars] + "..."


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    return value
