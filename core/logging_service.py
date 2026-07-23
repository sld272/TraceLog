"""Local JSONL logging for TraceLog runtime events."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from core import db

DEFAULT_ROTATE_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_HISTORY_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_HISTORY_MAX_DAYS = 14
MIN_ROTATE_MAX_BYTES = 1 * 1024 * 1024
MAX_ROTATE_MAX_BYTES = 100 * 1024 * 1024
MIN_HISTORY_MAX_BYTES = 10 * 1024 * 1024
MAX_HISTORY_MAX_BYTES = 1024 * 1024 * 1024
MAX_HISTORY_MAX_DAYS = 365
MAX_STRING_LENGTH = 16_000

DEFAULT_LOGGING_CONFIG = {
    "enabled": True,
    "level": "INFO",
    "capture_content": False,
    "rotate_max_bytes": DEFAULT_ROTATE_MAX_BYTES,
    "history_max_bytes": DEFAULT_HISTORY_MAX_BYTES,
    "history_max_days": DEFAULT_HISTORY_MAX_DAYS,
}

_lock = threading.RLock()
_enabled = False
_level = logging.INFO
_capture_content = False
_rotate_max_bytes = DEFAULT_ROTATE_MAX_BYTES
_history_max_bytes = DEFAULT_HISTORY_MAX_BYTES
_history_max_days = DEFAULT_HISTORY_MAX_DAYS
_current_log_path: Path | None = None
_current_bytes = 0
_LOGGING_CONFIG_KEYS = set(DEFAULT_LOGGING_CONFIG)

_SENSITIVE_KEYS = {"api_key", "authorization", "password", "secret", "token"}
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE),
]
_IMAGE_DATA_URL_RE = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.IGNORECASE)


def default_config() -> dict:
    """Return a fresh default logging config."""
    return dict(DEFAULT_LOGGING_CONFIG)


def normalize_config(config: dict | None) -> dict:
    """Merge user logging config with defaults and clamp storage budgets."""
    raw = config if isinstance(config, dict) else {}
    merged = default_config()
    merged.update(
        {
            key: value
            for key, value in raw.items()
            if key in _LOGGING_CONFIG_KEYS and value is not None
        }
    )

    merged["rotate_max_bytes"] = _clamp_int(
        merged.get("rotate_max_bytes"),
        DEFAULT_ROTATE_MAX_BYTES,
        MIN_ROTATE_MAX_BYTES,
        MAX_ROTATE_MAX_BYTES,
    )
    merged["history_max_bytes"] = _clamp_int(
        merged.get("history_max_bytes"),
        DEFAULT_HISTORY_MAX_BYTES,
        MIN_HISTORY_MAX_BYTES,
        MAX_HISTORY_MAX_BYTES,
    )
    merged["history_max_days"] = _clamp_int(
        merged.get("history_max_days"),
        DEFAULT_HISTORY_MAX_DAYS,
        1,
        MAX_HISTORY_MAX_DAYS,
    )
    level_name = str(merged.get("level", "INFO")).upper()
    merged["level"] = level_name if hasattr(logging, level_name) else "INFO"
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["capture_content"] = bool(merged.get("capture_content", False))
    return merged


def init_logging(config: dict | None = None) -> None:
    """Initialize JSONL logging and archive the previous process log."""
    global _current_bytes, _current_log_path

    settings = normalize_config(config)
    with _lock:
        _apply_config(settings)
        _current_log_path = db.WORKSPACE_DIR / "logs" / "current.jsonl"
        _current_bytes = _safe_size(_current_log_path)

        try:
            log_dir = _current_log_path.parent
            history_dir = log_dir / "history"
            log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            history_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            _make_private_dir(log_dir)
            _make_private_dir(history_dir)
            _migrate_log_permissions(log_dir, history_dir)

            if _current_bytes > 0:
                _archive_current_locked(history_dir)
            _prune_history(history_dir, _history_max_days, _history_max_bytes)
            _ensure_private_file(_current_log_path)
            _current_bytes = _safe_size(_current_log_path)
        except OSError:
            # Logging must never stop the application. A later write retries
            # opening/creating the current file.
            _current_bytes = _safe_size(_current_log_path)


def update_config(config: dict | None = None) -> None:
    """Hot-update logging behavior without archiving or rotating files."""
    settings = normalize_config(config)
    with _lock:
        _apply_config(settings)


def get_log_stats() -> dict[str, Any]:
    """Return current + history disk usage for the settings UI."""
    log_dir = db.WORKSPACE_DIR / "logs"
    history_dir = log_dir / "history"
    files = [log_dir / "current.jsonl"]
    try:
        files.extend(path for path in history_dir.glob("*.jsonl") if path.is_file())
    except OSError:
        pass

    file_count = 0
    total_bytes = 0
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            file_count += 1
            total_bytes += stat.st_size
    return {
        "enabled": _enabled,
        "capture_content": _capture_content,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "path": str(log_dir.resolve()),
    }


def clear_logs() -> dict[str, Any]:
    """Delete history and truncate current while preserving its private mode."""
    global _current_bytes

    log_dir = db.WORKSPACE_DIR / "logs"
    history_dir = log_dir / "history"
    current_path = log_dir / "current.jsonl"
    with _lock:
        try:
            history_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            _make_private_dir(log_dir)
            _make_private_dir(history_dir)
        except OSError:
            pass
        try:
            history_files = list(history_dir.glob("*.jsonl"))
        except OSError:
            history_files = []
        for path in history_files:
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
        try:
            _ensure_private_file(current_path, truncate=True)
            if _current_log_path == current_path:
                _current_bytes = 0
        except OSError:
            pass
    return get_log_stats()


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


def is_enabled_for(level: str = "DEBUG") -> bool:
    """Whether an event at ``level`` would be written."""
    if not _enabled:
        return False
    numeric_level = int(getattr(logging, level.upper(), logging.INFO))
    return numeric_level >= _level


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
    usage: dict | None = None,
    finish_reason: str | None = None,
) -> None:
    """Write one LLM call, gating conversational content independently."""
    include_content = _capture_content or status != "ok"
    request_payload: dict[str, Any] = {
        "model": model,
        "timeout": timeout_s,
        "response_format": response_format,
    }
    if include_content:
        request_payload["messages"] = messages
    else:
        request_payload["messages_count"] = len(messages)
        request_payload["messages_content_length"] = sum(
            _string_leaf_length(message.get("content")) for message in messages
        )

    response_payload: dict[str, Any] = {
        "content_length": len(response_content or ""),
        "content_stripped_length": len((response_content or "").strip()),
    }
    if include_content:
        response_payload["content"] = response_content

    fields: dict[str, Any] = {
        "call_id": call_id,
        "operation": operation,
        "model": model,
        "status": status,
        "duration_ms": duration_ms,
        "timeout_s": timeout_s,
        "context": context or {},
        "usage": usage,
        "finish_reason": finish_reason,
        "request": request_payload,
        "response": response_payload,
    }
    if include_content and parsed is not None:
        fields["parsed"] = parsed
    if error:
        fields["error"] = error

    level = "ERROR" if status == "api_error" else "WARNING" if status != "ok" else "INFO"
    log_event("llm_call", level=level, **fields)


def _write_jsonl(payload: dict[str, Any]) -> None:
    global _current_bytes

    if not _enabled or _current_log_path is None:
        return
    try:
        clean_payload = _truncate(_redact(payload))
        line_bytes = (json.dumps(clean_payload, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with _lock:
            if not _enabled or _current_log_path is None:
                return
            descriptor = os.open(
                _current_log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "ab") as handle:
                handle.write(line_bytes)
            _current_bytes += len(line_bytes)
            if _current_bytes > _rotate_max_bytes:
                _archive_current_locked(_current_log_path.parent / "history")
    except Exception:
        pass


def _archive_current_locked(history_dir: Path) -> bool:
    global _current_bytes

    if _current_log_path is None or _safe_size(_current_log_path) <= 0:
        _current_bytes = _safe_size(_current_log_path)
        return False
    try:
        history_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _make_private_dir(history_dir)
        archived = _unique_history_path(history_dir)
        shutil.move(str(_current_log_path), str(archived))
    except OSError:
        _current_bytes = _safe_size(_current_log_path)
        return False

    _make_private_file(archived)
    _current_bytes = 0
    try:
        _ensure_private_file(_current_log_path)
    except OSError:
        pass
    _prune_history(history_dir, _history_max_days, _history_max_bytes)
    return True


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


def _prune_history(history_dir: Path, max_days: int, max_bytes: int) -> None:
    """Delete expired logs, then enforce the aggregate byte budget oldest-first."""
    try:
        candidates = list(history_dir.glob("*.jsonl"))
    except OSError:
        return

    cutoff = time.time() - max_days * 24 * 60 * 60
    retained: list[tuple[Path, os.stat_result]] = []
    for path in candidates:
        try:
            stat = path.stat()
            if not path.is_file():
                continue
            if stat.st_mtime < cutoff:
                path.unlink()
                continue
            retained.append((path, stat))
        except OSError:
            continue

    total_bytes = sum(stat.st_size for _, stat in retained)
    for path, stat in sorted(retained, key=lambda item: (item[1].st_mtime, item[0].name)):
        if total_bytes <= max_bytes:
            break
        try:
            path.unlink()
            total_bytes -= stat.st_size
        except OSError:
            pass


def _apply_config(settings: dict[str, Any]) -> None:
    global _capture_content, _enabled, _history_max_bytes, _history_max_days, _level, _rotate_max_bytes

    _enabled = bool(settings["enabled"])
    _level = int(getattr(logging, settings["level"], logging.INFO))
    _capture_content = bool(settings["capture_content"])
    _rotate_max_bytes = int(settings["rotate_max_bytes"])
    _history_max_bytes = int(settings["history_max_bytes"])
    _history_max_days = int(settings["history_max_days"])


def _ensure_private_file(path: Path, *, truncate: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    if truncate:
        flags |= os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)
    _make_private_file(path)


def _make_private_dir(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _make_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _migrate_log_permissions(log_dir: Path, history_dir: Path) -> None:
    for directory in (log_dir, history_dir):
        try:
            paths = list(directory.glob("*.jsonl"))
        except OSError:
            continue
        for path in paths:
            _make_private_file(path)


def _safe_size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _string_leaf_length(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_string_leaf_length(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_string_leaf_length(item) for item in value)
    return 0


def _truncate(value: Any) -> Any:
    if isinstance(value, dict):
        return {item_key: _truncate(item_value) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_truncate(item) for item in value]
    if isinstance(value, tuple):
        return [_truncate(item) for item in value]
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        removed = len(value) - MAX_STRING_LENGTH
        return f"{value[:MAX_STRING_LENGTH]}…[truncated {removed} chars]"
    return value


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if key is not None and key.lower() == "url" and isinstance(value, str) and _IMAGE_DATA_URL_RE.match(value):
        return "[REDACTED_IMAGE_DATA_URL]"
    if isinstance(value, dict):
        return {item_key: _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        if _IMAGE_DATA_URL_RE.match(value):
            return "[REDACTED_IMAGE_DATA_URL]"
        redacted = value
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    return value
