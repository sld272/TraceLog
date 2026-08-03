"""Shared helpers for LLM router modules."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from time import perf_counter
from typing import Any, Callable

from core import logging_service
from core.llm.types import LLMClient

# Extra sends allowed when a provider hands back an empty answer. Two brings a
# ~5% per-call blank rate down to roughly one in ten thousand.
EMPTY_CONTENT_RETRIES = 2


class StreamCompletionError(Exception):
    """A streaming chat completion failed mid-flight.

    Carries the length of text accumulated before the failure so callers can
    log how far the stream got, without exposing the (discarded) partial text.
    """

    def __init__(self, message: str, *, accumulated_length: int = 0) -> None:
        super().__init__(message)
        self.accumulated_length = accumulated_length


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
    status_callback: Callable[[dict[str, Any]], None] | None = None,
    empty_content_retries: int = EMPTY_CONTENT_RETRIES,
) -> dict | None:
    """Call a JSON-mode chat completion and log the full lifecycle.

    Reasoning models sometimes end a generation right after their thinking pass
    and hand back an empty ``content`` with ``finish_reason='stop'`` — measured
    at roughly 5% of calls on long structured-JSON prompts. It is a per-call
    fluke rather than a property of the prompt, so a blank answer is re-sent
    instead of being reported as unparseable. Each attempt is logged on its own,
    so the retries stay visible.
    """
    attempts = max(1, empty_content_retries + 1)
    for attempt in range(1, attempts + 1):
        parsed, retry = _json_completion_attempt(
            client=client,
            model=model,
            operation=operation,
            messages=messages,
            parser=parser,
            timeout=timeout,
            response_format=response_format,
            trace_context=trace_context,
            status_callback=status_callback,
            last_attempt=attempt == attempts,
        )
        if not retry:
            return parsed
    return None


def _json_completion_attempt(
    *,
    client: LLMClient,
    model: str,
    operation: str,
    messages: list[dict[str, Any]],
    parser: Callable[[str | None], dict | None],
    timeout: int,
    response_format: dict | None,
    trace_context: dict | None,
    status_callback: Callable[[dict[str, Any]], None] | None,
    last_attempt: bool,
) -> tuple[dict | None, bool]:
    """One request. Returns (parsed, whether an empty answer is worth retrying)."""
    call_id = _new_call_id()
    started = perf_counter()
    response_content: str | None = None
    parsed: dict | None = None
    status = "ok"
    error: dict | str | None = None
    usage: dict | None = None
    finish_reason: str | None = None
    retry = False

    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "timeout": timeout,
            "messages": messages,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        response = client.chat.completions.create(**kwargs)
        usage = _completion_usage(response)
        finish_reason = _completion_finish_reason(response)
        response_content = _message_content(response)
        if not (response_content or "").strip():
            if not last_attempt:
                status = "empty_content"
                retry = True
                return None, True
            # Last chance: the answer is sometimes left in the thinking channel.
            salvaged = _json_object_tail(_reasoning_content(response))
            parsed = parser(salvaged) if salvaged is not None else None
            if parsed is None:
                status = "empty_content"
                return None, False
            status = "ok_from_reasoning"
            response_content = salvaged
            return parsed, False
        parsed = parser(response_content)
        if parsed is None:
            status = _invalid_response_status(response_content, finish_reason)
            return None, False
        return parsed, False
    except Exception as exc:
        status = "api_error"
        error = _api_error_details(exc, operation=operation, model=model, timeout_s=timeout)
        return None, False
    finally:
        duration_ms = int((perf_counter() - started) * 1000)
        if status_callback is not None:
            status_callback({"status": status, "error": error, "will_retry": retry})
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
            usage=usage,
            finish_reason=finish_reason,
        )


def stream_completion(
    *,
    client: LLMClient,
    model: str,
    operation: str,
    messages: list[dict[str, Any]],
    on_delta: Callable[[str], None],
    timeout: int = 30,
    trace_context: dict | None = None,
) -> str:
    """Stream a chat completion, invoking ``on_delta`` for each non-empty text
    delta, and log the full lifecycle (the streaming sibling of
    ``call_json_completion``).

    Returns the accumulated text on success. Raises ``StreamCompletionError`` on
    a transport failure — the partial text is discarded, only its length is kept
    for the log so callers never surface a truncated reply.
    """
    call_id = _new_call_id()
    started = perf_counter()
    chunks: list[str] = []
    status = "ok"
    error: dict | str | None = None
    usage: dict | None = None
    finish_reason: str | None = None

    try:
        stream = client.chat.completions.create(
            model=model,
            timeout=timeout,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            chunk_usage = _completion_usage(chunk)
            if chunk_usage is not None:
                usage = chunk_usage
            chunk_finish_reason = _completion_finish_reason(chunk)
            if chunk_finish_reason is not None:
                finish_reason = chunk_finish_reason
            text = _stream_delta_text(chunk)
            if text:
                chunks.append(text)
                on_delta(text)
        return "".join(chunks)
    except Exception as exc:
        status = "api_error"
        error = _api_error_details(exc, operation=operation, model=model, timeout_s=timeout)
        raise StreamCompletionError(str(exc), accumulated_length=len("".join(chunks))) from exc
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
            response_content="".join(chunks),
            error=error,
            context=trace_context,
            usage=usage,
            finish_reason=finish_reason,
        )


def _stream_delta_text(chunk: Any) -> str:
    """Extract the incremental text from one streamed chunk.

    Tolerates the empty ``choices`` chunks some OpenAI-compatible providers emit
    (usage-only or keep-alive frames)."""
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None) if delta is not None else None
    return content or ""


def _completion_usage(response: Any) -> dict | None:
    """Best-effort extraction across OpenAI and compatible response objects."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    result = {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
    return result if any(value is not None for value in result.values()) else None


def _completion_finish_reason(response: Any) -> str | None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    value = getattr(choices[0], "finish_reason", None)
    return str(value) if value is not None else None


def _invalid_response_status(content: str | None, finish_reason: str | None = None) -> str:
    try:
        json.loads(clean_json_content(content))
    except json.JSONDecodeError:
        # Broken JSON that stopped at the token cap is a length problem, not a
        # formatting one — retrying the same request would truncate again.
        return "truncated" if finish_reason == "length" else "invalid_json"
    return "invalid_response"


def _message(response: Any) -> Any:
    choices = getattr(response, "choices", None) or []
    return getattr(choices[0], "message", None) if choices else None


def _message_content(response: Any) -> str | None:
    message = _message(response)
    return getattr(message, "content", None) if message is not None else None


def _reasoning_content(response: Any) -> str | None:
    """The provider's thinking channel, wherever the SDK parked it."""
    message = _message(response)
    if message is None:
        return None
    value = getattr(message, "reasoning_content", None)
    if value is None:
        extra = getattr(message, "model_extra", None) or {}
        value = extra.get("reasoning_content") or extra.get("reasoning")
    return str(value) if value else None


def _json_object_tail(text: str | None) -> str | None:
    """Last complete JSON object in free text, or None when there is none."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return text[index : index + end]
    return None


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
