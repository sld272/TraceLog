"""Shared helpers for LLM router modules."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from time import perf_counter
from typing import Any, Callable

from core import logging_service
from core.llm.types import LLMClient


def now_str() -> str:
    now = datetime.now().astimezone()
    weekday = ["一", "二", "三", "四", "五", "六", "日"]
    return now.strftime(f"%Y 年 %m 月 %d 日（周{weekday[now.weekday()]}）%H:%M")


def clean_json_content(content: str | None) -> str:
    text = (content or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def call_json_completion(
    *,
    client: LLMClient,
    model: str,
    operation: str,
    messages: list[dict[str, Any]],
    parser: Callable[[str | None], dict | None],
    timeout: int = 30,
    response_format: dict | None = None,
    trace_context: dict | None = None,
) -> dict | None:
    """Call a JSON-mode chat completion and log the full lifecycle."""
    call_id = _new_call_id()
    started = perf_counter()
    response_content: str | None = None
    parsed: dict | None = None
    status = "ok"
    error: dict | str | None = None

    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "timeout": timeout,
            "messages": messages,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        response = client.chat.completions.create(**kwargs)
        response_content = response.choices[0].message.content
        parsed = parser(response_content)
        if parsed is None:
            status = _invalid_response_status(response_content)
            return None
        return parsed
    except Exception as exc:
        status = "api_error"
        error = _api_error_details(exc, operation=operation, model=model, timeout_s=timeout)
        return None
    finally:
        duration_ms = int((perf_counter() - started) * 1000)
        logging_service.log_llm_call(
            call_id=call_id,
            operation=operation,
            model=model,
            status=status,
            duration_ms=duration_ms,
            timeout_s=timeout,
            messages=messages,
            response_content=response_content,
            parsed=parsed,
            error=error,
            context=trace_context,
            response_format=response_format,
        )


def _invalid_response_status(content: str | None) -> str:
    try:
        json.loads(clean_json_content(content))
    except json.JSONDecodeError:
        return "invalid_json"
    return "invalid_response"


def _api_error_details(exc: Exception, *, operation: str, model: str, timeout_s: int | float | None) -> dict:
    details = {
        "operation": operation,
        "model": model,
        "timeout_s": timeout_s,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
    }
    for attr in ("status_code", "code", "type", "request_id"):
        value = getattr(exc, attr, None)
        if value is not None:
            details[attr] = value
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code is not None and "status_code" not in details:
            details["status_code"] = status_code
        headers = getattr(response, "headers", {})
        request_id = headers.get("x-request-id") if hasattr(headers, "get") else None
        if request_id is not None and "request_id" not in details:
            details["request_id"] = request_id
    return details


def _new_call_id() -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"
