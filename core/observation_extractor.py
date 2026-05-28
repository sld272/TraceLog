"""Cursor-based observation extraction for chat and comment threads."""

from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from typing import Any

from core import chat_service, comment_service, db, logging_service, observation_service
from core.llm import reflection_router
from core.llm.types import LLMClient


CHAT_SOURCE_KIND = "chat_thread"
COMMENT_SOURCE_KIND = "comment_thread"
DEFAULT_LIMIT_PER_THREAD = 20
MAX_INVALID_EXTRACTION_RETRIES = 3
INVALID_EXTRACTION_STATUSES = {"invalid_json", "invalid_response"}


@dataclass(frozen=True)
class ObservationExtractionResult:
    source_kind: str
    source_key: str
    processed_count: int
    observation_count: int
    cursor_value: str
    error: str | None = None
    skipped_poison_batch: bool = False


def run_pending_observation_extractions(
    client: LLMClient,
    model: str,
    *,
    limit_per_thread: int = DEFAULT_LIMIT_PER_THREAD,
) -> list[ObservationExtractionResult]:
    """Extract observations for all thread sources with messages past their cursor."""
    results: list[ObservationExtractionResult] = []
    for source_kind, source_key in _pending_sources():
        result = _extract_source(source_kind, source_key, client, model, limit_per_thread=limit_per_thread)
        if result is not None:
            results.append(result)
    return results


def run_pending_observation_extractions_safely(
    client: LLMClient,
    model: str,
    *,
    limit_per_thread: int = DEFAULT_LIMIT_PER_THREAD,
) -> list[ObservationExtractionResult]:
    """Run pending extraction without interrupting CLI startup or shutdown."""
    results: list[ObservationExtractionResult] = []
    try:
        pending_sources = _pending_sources()
    except sqlite3.Error as exc:
        logging_service.log_event(
            "observation_extraction_failed",
            level="WARNING",
            reason="pending_source_scan_failed",
            error=str(exc),
        )
        return []

    for source_kind, source_key in pending_sources:
        try:
            result = _extract_source(source_kind, source_key, client, model, limit_per_thread=limit_per_thread)
        except Exception as exc:
            logging_service.log_event(
                "observation_extraction_failed",
                level="WARNING",
                source_kind=source_kind,
                source_key=source_key,
                error=str(exc),
            )
            results.append(
                ObservationExtractionResult(
                    source_kind=source_kind,
                    source_key=source_key,
                    processed_count=0,
                    observation_count=0,
                    cursor_value=observation_service.get_cursor(source_kind, source_key) or "0",
                    error=str(exc),
                )
            )
            continue
        if result is not None:
            results.append(result)
    return results


def extract_chat_thread_observations(
    thread_id: int,
    client: LLMClient,
    model: str,
    *,
    limit: int = DEFAULT_LIMIT_PER_THREAD,
) -> ObservationExtractionResult | None:
    """Extract observations from new private chat messages for one thread."""
    thread = chat_service.get_thread(thread_id)
    source_key = str(thread_id)
    cursor = _cursor_int(observation_service.get_cursor(CHAT_SOURCE_KIND, source_key))
    messages = _load_chat_messages(thread_id, cursor, limit)
    if not messages:
        return None

    cursor_value = str(max(message["id"] for message in messages))
    user_messages = [message for message in messages if message["role"] == "user"]
    observations: list[dict[str, Any]] = []
    if user_messages:
        data, status = reflection_router.call_thread_observation_extraction_with_status(
            client=client,
            model=model,
            thread_context=_format_chat_context(thread),
            messages=_format_messages(messages),
            user_message_ids=[message["id"] for message in user_messages],
            trace_context={
                "source_kind": CHAT_SOURCE_KIND,
                "source_key": source_key,
                "thread_id": thread_id,
                "soul_name": thread.soul_name,
                "message_count": len(messages),
            },
        )
        if data is None:
            return _handle_invalid_extraction_batch(
                source_kind=CHAT_SOURCE_KIND,
                source_key=source_key,
                cursor_before=cursor,
                cursor_value=cursor_value,
                messages=messages,
                status=status,
                error="私聊 observation 提取没有返回有效 JSON",
                metadata={"thread_id": thread_id, "soul_name": thread.soul_name},
            )
        observations = data["observations"]

    observation_ids = observation_service.save_extraction_batch(
        source_kind=CHAT_SOURCE_KIND,
        source_key=source_key,
        cursor_value=cursor_value,
        observations=observations,
        source_channel="chat",
        visibility_scope="soul_scoped",
        source_type="chat_message",
        evidence_access="source_soul_only",
        scope_soul_name=thread.soul_name,
        source_excerpt_by_id=_excerpt_by_id(user_messages),
        source_observed_at_by_id=_observed_at_by_id(user_messages),
        metadata={"thread_id": thread_id, "soul_name": thread.soul_name},
    )
    return ObservationExtractionResult(
        source_kind=CHAT_SOURCE_KIND,
        source_key=source_key,
        processed_count=len(messages),
        observation_count=len(observation_ids),
        cursor_value=cursor_value,
    )


def extract_comment_thread_observations(
    thread_id: int,
    client: LLMClient,
    model: str,
    *,
    limit: int = DEFAULT_LIMIT_PER_THREAD,
) -> ObservationExtractionResult | None:
    """Extract observations from new public comment-thread messages for one thread."""
    thread = comment_service.get_thread(thread_id)
    source_key = str(thread_id)
    cursor = _cursor_int(observation_service.get_cursor(COMMENT_SOURCE_KIND, source_key))
    messages = _load_comment_messages(thread_id, cursor, limit)
    if not messages:
        return None

    cursor_value = str(max(message["id"] for message in messages))
    user_messages = [message for message in messages if message["role"] == "user"]
    observations: list[dict[str, Any]] = []
    if user_messages:
        data, status = reflection_router.call_thread_observation_extraction_with_status(
            client=client,
            model=model,
            thread_context=_format_comment_context(thread),
            messages=_format_messages(messages),
            user_message_ids=[message["id"] for message in user_messages],
            trace_context={
                "source_kind": COMMENT_SOURCE_KIND,
                "source_key": source_key,
                "thread_id": thread_id,
                "post_id": thread.post_id,
                "soul_name": thread.soul_name,
                "message_count": len(messages),
            },
        )
        if data is None:
            return _handle_invalid_extraction_batch(
                source_kind=COMMENT_SOURCE_KIND,
                source_key=source_key,
                cursor_before=cursor,
                cursor_value=cursor_value,
                messages=messages,
                status=status,
                error="评论线程 observation 提取没有返回有效 JSON",
                metadata={"thread_id": thread_id, "post_id": thread.post_id, "soul_name": thread.soul_name},
            )
        observations = data["observations"]

    observation_ids = observation_service.save_extraction_batch(
        source_kind=COMMENT_SOURCE_KIND,
        source_key=source_key,
        cursor_value=cursor_value,
        observations=observations,
        source_channel="comment_thread",
        visibility_scope="post_visible",
        source_type="comment_message",
        evidence_access="post_visible",
        scope_post_id=thread.post_id,
        source_excerpt_by_id=_excerpt_by_id(user_messages),
        source_observed_at_by_id=_observed_at_by_id(user_messages),
        metadata={"thread_id": thread_id, "post_id": thread.post_id, "soul_name": thread.soul_name},
    )
    return ObservationExtractionResult(
        source_kind=COMMENT_SOURCE_KIND,
        source_key=source_key,
        processed_count=len(messages),
        observation_count=len(observation_ids),
        cursor_value=cursor_value,
    )


def _extract_source(
    source_kind: str,
    source_key: str,
    client: LLMClient,
    model: str,
    *,
    limit_per_thread: int,
) -> ObservationExtractionResult | None:
    if source_kind == CHAT_SOURCE_KIND:
        return extract_chat_thread_observations(int(source_key), client, model, limit=limit_per_thread)
    if source_kind == COMMENT_SOURCE_KIND:
        return extract_comment_thread_observations(int(source_key), client, model, limit=limit_per_thread)
    raise ValueError(f"unknown observation source kind: {source_kind}")


def _handle_invalid_extraction_batch(
    *,
    source_kind: str,
    source_key: str,
    cursor_before: int,
    cursor_value: str,
    messages: list[dict[str, Any]],
    status: str | None,
    error: str,
    metadata: dict[str, Any],
) -> ObservationExtractionResult:
    if status not in INVALID_EXTRACTION_STATUSES:
        detail = f"{error}: {status}" if status else error
        raise ValueError(detail)

    message_ids = [int(message["id"]) for message in messages]
    previous = _cursor_metadata(source_kind, source_key)
    skipped_batches = previous.get("skipped_poison_batches")
    if not isinstance(skipped_batches, list):
        skipped_batches = []

    same_batch = (
        previous.get("failure_kind") == "invalid_llm_response"
        and previous.get("failed_cursor_before") == str(cursor_before)
        and previous.get("failed_cursor_value") == str(cursor_value)
        and previous.get("failed_message_ids") == message_ids
    )
    failure_count = int(previous.get("failure_count") or 0) + 1 if same_batch else 1
    now = db.now_ts()
    failure_metadata = {
        **metadata,
        "failure_count": failure_count,
        "failure_kind": "invalid_llm_response",
        "failure_status": status,
        "failed_cursor_before": str(cursor_before),
        "failed_cursor_value": str(cursor_value),
        "failed_message_ids": message_ids,
        "last_error": error,
        "last_failed_at": now,
        "skipped_poison_batches": skipped_batches,
    }

    if failure_count >= MAX_INVALID_EXTRACTION_RETRIES:
        skipped_summary = {
            "cursor_before": str(cursor_before),
            "cursor_value": str(cursor_value),
            "message_ids": message_ids,
            "failure_count": failure_count,
            "failure_status": status,
            "skipped_at": now,
        }
        failure_metadata["skipped_poison_batch"] = skipped_summary
        failure_metadata["skipped_poison_batches"] = [*skipped_batches, skipped_summary]
        observation_service.set_cursor(source_kind, source_key, cursor_value, metadata=failure_metadata)
        reason = f"skipped_poison_batch_after_{MAX_INVALID_EXTRACTION_RETRIES}_invalid_results"
        logging_service.log_event(
            "observation_extraction_poison_batch_skipped",
            level="WARNING",
            source_kind=source_kind,
            source_key=source_key,
            cursor_before=str(cursor_before),
            cursor_value=cursor_value,
            message_ids=message_ids,
            failure_count=failure_count,
            status=status,
        )
        return ObservationExtractionResult(
            source_kind=source_kind,
            source_key=source_key,
            processed_count=len(messages),
            observation_count=0,
            cursor_value=cursor_value,
            error=reason,
            skipped_poison_batch=True,
        )

    observation_service.set_cursor(source_kind, source_key, str(cursor_before), metadata=failure_metadata)
    reason = f"invalid_extraction_result_retry_{failure_count}_of_{MAX_INVALID_EXTRACTION_RETRIES}"
    logging_service.log_event(
        "observation_extraction_failed",
        level="WARNING",
        reason=reason,
        source_kind=source_kind,
        source_key=source_key,
        cursor_before=str(cursor_before),
        cursor_value=cursor_value,
        message_ids=message_ids,
        failure_count=failure_count,
        status=status,
    )
    return ObservationExtractionResult(
        source_kind=source_kind,
        source_key=source_key,
        processed_count=0,
        observation_count=0,
        cursor_value=str(cursor_before),
        error=reason,
    )


def _cursor_metadata(source_kind: str, source_key: str) -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT metadata
        FROM observation_cursors
        WHERE source_kind = ? AND source_key = ?
        """,
        (source_kind, source_key),
    )
    if row is None or not row["metadata"]:
        return {}
    try:
        data = json.loads(row["metadata"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pending_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    chat_rows = db.query_all(
        """
        SELECT chat_threads.id AS thread_id
        FROM chat_threads
        WHERE EXISTS (
            SELECT 1
            FROM chat_messages
            WHERE chat_messages.thread_id = chat_threads.id
              AND chat_messages.id > COALESCE((
                  SELECT CAST(cursor_value AS INTEGER)
                  FROM observation_cursors
                  WHERE source_kind = 'chat_thread'
                    AND source_key = CAST(chat_threads.id AS TEXT)
              ), 0)
        )
        ORDER BY chat_threads.id ASC
        """
    )
    sources.extend((CHAT_SOURCE_KIND, str(row["thread_id"])) for row in chat_rows)

    comment_rows = db.query_all(
        """
        SELECT comment_threads.id AS thread_id
        FROM comment_threads
        WHERE EXISTS (
            SELECT 1
            FROM comment_messages
            WHERE comment_messages.thread_id = comment_threads.id
              AND comment_messages.id > COALESCE((
                  SELECT CAST(cursor_value AS INTEGER)
                  FROM observation_cursors
                  WHERE source_kind = 'comment_thread'
                    AND source_key = CAST(comment_threads.id AS TEXT)
              ), 0)
        )
        ORDER BY comment_threads.id ASC
        """
    )
    sources.extend((COMMENT_SOURCE_KIND, str(row["thread_id"])) for row in comment_rows)
    return sources


def _load_chat_messages(thread_id: int, cursor: int, limit: int) -> list[dict[str, Any]]:
    rows = db.query_all(
        """
        SELECT id, role, content, created_at
        FROM chat_messages
        WHERE thread_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (thread_id, cursor, max(limit, 1)),
    )
    return [_message_row_to_dict(row) for row in rows]


def _load_comment_messages(thread_id: int, cursor: int, limit: int) -> list[dict[str, Any]]:
    rows = db.query_all(
        """
        SELECT id, role, content, created_at
        FROM comment_messages
        WHERE thread_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (thread_id, cursor, max(limit, 1)),
    )
    return [_message_row_to_dict(row) for row in rows]


def _message_row_to_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "role": row["role"],
        "content": row["content"],
        "created_at": float(row["created_at"]),
    }


def _format_chat_context(thread: chat_service.ChatThread) -> str:
    return (
        "channel: private_chat\n"
        f"thread_id: {thread.id}\n"
        f"soul_name: {thread.soul_name}\n"
        "visibility_boundary: soul_scoped\n"
        "evidence_access: source_soul_only"
    )


def _format_comment_context(thread: comment_service.CommentThread) -> str:
    post = db.query_one("SELECT id, ts, content FROM posts WHERE id = ?", (thread.post_id,))
    root = db.query_one("SELECT id, content FROM comments WHERE id = ?", (thread.root_comment_id,))
    parts = [
        "channel: comment_thread",
        f"thread_id: {thread.id}",
        f"post_id: {thread.post_id}",
        f"soul_name: {thread.soul_name}",
        "visibility_boundary: post_visible",
        "evidence_access: post_visible",
    ]
    if post is not None:
        parts.append(f"original_post: [{post['id']}] {post['content']}")
    if root is not None:
        parts.append(f"root_comment: [{root['id']}] {root['content']}")
    return "\n".join(parts)


def _format_messages(messages: list[dict[str, Any]]) -> str:
    parts = []
    for message in messages:
        parts.append(
            "---\n"
            f"id: {message['id']}\n"
            f"role: {message['role']}\n"
            f"created_at: {message['created_at']}\n"
            "---\n\n"
            f"{message['content']}"
        )
    return "\n\n".join(parts)


def _excerpt_by_id(messages: list[dict[str, Any]]) -> dict[int, str]:
    return {message["id"]: _excerpt(message["content"]) for message in messages}


def _observed_at_by_id(messages: list[dict[str, Any]]) -> dict[int, float]:
    return {message["id"]: float(message["created_at"]) for message in messages}


def _excerpt(content: str, limit: int = 500) -> str:
    text = str(content or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _cursor_int(value: str | None) -> int:
    try:
        return int(value or "0")
    except ValueError:
        return 0
