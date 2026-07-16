"""Private chat service for one-on-one SOUL conversations."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from core import attachment_service, db, goal_service, logging_service, memory_events_service, memory_read, memory_unit_service, query_rewriter, record_service, reply_context, soul_service, suggestion_pipeline, vision_service
from core.app_services import job_service
from core.attachment_service import Attachment
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext

CHAT_HISTORY_LIMIT = 20
_REQUEST_LOCKS = tuple(threading.Lock() for _ in range(64))


@dataclass(frozen=True)
class ChatThread:
    id: int
    soul_name: str
    title: str | None
    created_at: float
    updated_at: float
    last_message_at: float | None


@dataclass(frozen=True)
class ChatMessage:
    id: int
    thread_id: int
    role: str
    content: str
    created_at: float
    edited_at: float | None
    rerun_at: float | None
    metadata: str | None
    client_request_id: str | None
    attachments: list[Attachment]


@dataclass(frozen=True)
class ChatContext:
    thread: ChatThread
    soul: SoulContext
    context: str
    messages: list[ChatMessage]
    cited_memory: list[dict] = field(default_factory=list)
    # per-stage build durations (seconds) — reply latency instrumentation:
    # the serial LLM legs (web gate -> query rewrite -> reply) are the
    # suspected bottleneck; measure before parallelizing anything.
    timings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChatReplyResult:
    thread_id: int
    soul_name: str
    ok: bool
    reply: str
    user_message_id: int
    assistant_message_id: int | None
    error: str | None
    suggestions: list[dict] = field(default_factory=list)


FAILED_REPLY_CONTENT = ""


def list_chat_threads(soul_name: str | None = None) -> list[ChatThread]:
    """List chat threads, newest activity first."""
    params: tuple = ()
    where = ""
    if soul_name is not None:
        soul_service.validate_soul_name(soul_name)
        where = "WHERE soul_name = ?"
        params = (soul_name,)
    rows = db.query_all(
        f"""
        SELECT id, soul_name, title, created_at, updated_at, last_message_at
        FROM chat_threads
        {where}
        ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC, id DESC
        """,
        params,
    )
    return [_thread_from_row(row) for row in rows]


def get_or_create_thread(soul_name: str) -> ChatThread:
    """Return the newest writable thread for a SOUL, creating one if needed."""
    _assert_soul_writable(soul_name)
    row = db.query_one(
        """
        SELECT id, soul_name, title, created_at, updated_at, last_message_at
        FROM chat_threads
        WHERE soul_name = ?
        ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC, id DESC
        LIMIT 1
        """,
        (soul_name,),
    )
    if row is not None:
        return _thread_from_row(row)

    now = db.now_ts()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO chat_threads(soul_name, title, created_at, updated_at, last_message_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (soul_name, f"与{soul_name}的私聊", now, now),
        )
        thread_id = db.require_lastrowid(cursor, "chat thread insert")
    return get_thread(thread_id)


def get_thread(thread_id: int) -> ChatThread:
    """Return one chat thread."""
    row = db.query_one(
        """
        SELECT id, soul_name, title, created_at, updated_at, last_message_at
        FROM chat_threads
        WHERE id = ?
        """,
        (thread_id,),
    )
    if row is None:
        raise ValueError(f"私聊线程不存在：{thread_id}")
    return _thread_from_row(row)


def list_thread_messages(thread_id: int, limit: int = 30, *, before_message_id: int | None = None) -> list[ChatMessage]:
    """List recent thread messages in chronological order."""
    get_thread(thread_id)
    before_clause = ""
    params: list = [thread_id]
    if before_message_id is not None:
        before_clause = "AND id < ?"
        params.append(int(before_message_id))
    params.append(limit)
    rows = db.query_all(
        f"""
        SELECT id, thread_id, role, content, created_at, edited_at, rerun_at, metadata,
               client_request_id
        FROM chat_messages
        WHERE thread_id = ?
        {before_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [_message_from_row(row) for row in reversed(rows)]


def list_thread_messages_after(thread_id: int, after_id: int, limit: int = 100) -> list[ChatMessage]:
    """List thread messages after a message id for API event streams."""
    get_thread(thread_id)
    rows = db.query_all(
        """
        SELECT id, thread_id, role, content, created_at, edited_at, rerun_at, metadata,
               client_request_id
        FROM chat_messages
        WHERE thread_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (thread_id, max(0, int(after_id)), max(1, min(int(limit), 100))),
    )
    return [_message_from_row(row) for row in rows]


def _normalize_client_request_id(request_id: str | None) -> str | None:
    if request_id is None:
        return None
    normalized = str(request_id).strip()
    if not normalized:
        raise ValueError("request_id 不能为空")
    if len(normalized) > 128:
        raise ValueError("request_id 过长")
    return normalized


def _request_lock(request_id: str | None):
    """Serialize one idempotent turn in the local desktop process.

    A fixed lock stripe avoids an unbounded request-id lock registry. SQLite's
    unique index remains the final guard if separate workers race."""
    if request_id is None:
        return nullcontext()
    return _REQUEST_LOCKS[hash(request_id) % len(_REQUEST_LOCKS)]


def _message_for_client_request(
    thread_id: int,
    request_id: str,
    role: str,
) -> ChatMessage | None:
    row = db.query_one(
        """
        SELECT id, thread_id, role, content, created_at, edited_at, rerun_at, metadata,
               client_request_id
        FROM chat_messages
        WHERE thread_id = ? AND client_request_id = ? AND role = ?
        LIMIT 1
        """,
        (thread_id, request_id, role),
    )
    return _message_from_row(row) if row is not None else None


def _assert_same_idempotent_input(
    existing: ChatMessage,
    content: str,
    attachment_ids: list[str],
) -> None:
    existing_attachment_ids = [attachment.id for attachment in existing.attachments]
    if existing.content != content.strip() or existing_attachment_ids != attachment_ids:
        raise ValueError("同一 request_id 不能用于不同的私聊内容或附件")


def _reply_result_for_request(user_message: ChatMessage) -> ChatReplyResult | None:
    request_id = user_message.client_request_id
    if request_id is None:
        return None
    assistant = _message_for_client_request(user_message.thread_id, request_id, "assistant")
    if assistant is None:
        return None
    try:
        metadata = json.loads(assistant.metadata or "{}")
    except json.JSONDecodeError:
        metadata = {}
    status = metadata.get("status")
    ok = status == "ok"
    suggestions = metadata.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []
    error = metadata.get("error")
    if not isinstance(error, str):
        error = None
    thread = get_thread(user_message.thread_id)
    return ChatReplyResult(
        thread_id=thread.id,
        soul_name=thread.soul_name,
        ok=ok,
        reply=assistant.content if ok else FAILED_REPLY_CONTENT,
        user_message_id=user_message.id,
        assistant_message_id=assistant.id,
        error=error,
        suggestions=suggestions,
    )


def append_user_message(
    thread_id: int,
    content: str,
    attachment_ids: list[str] | None = None,
    *,
    client_request_id: str | None = None,
) -> ChatMessage:
    """Append a user message to a writable chat thread."""
    thread = get_thread(thread_id)
    _assert_soul_writable(thread.soul_name)
    request_id = _normalize_client_request_id(client_request_id)
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    if request_id is not None:
        existing = _message_for_client_request(thread_id, request_id, "user")
        if existing is not None:
            _assert_same_idempotent_input(existing, content, attachment_ids)
            return existing
    try:
        message = _append_message(
            thread_id,
            "user",
            content,
            attachment_ids=attachment_ids,
            client_request_id=request_id,
        )
    except sqlite3.IntegrityError:
        # The unique index is the cross-worker source of truth if two requests
        # race between the lookup and INSERT.
        if request_id is None:
            raise
        existing = _message_for_client_request(thread_id, request_id, "user")
        if existing is None:
            raise
        _assert_same_idempotent_input(existing, content, attachment_ids)
        return existing
    if message.content.strip():
        job_service.enqueue_memory_reconcile_once({"trigger": "chat", "soul_name": thread.soul_name})
    return message


def build_chat_context(
    thread_id: int,
    user_message: str,
    client: LLMClient | None = None,
    model: str | None = None,
    *,
    before_message_id: int | None = None,
) -> ChatContext:
    """Build prompt context after the current user message has been appended."""
    thread = get_thread(thread_id)
    soul = _load_soul_context(thread.soul_name)
    messages = list_thread_messages(thread_id, limit=CHAT_HISTORY_LIMIT, before_message_id=before_message_id)
    llm_messages = [_message_for_llm(message) for message in messages]
    sections: list[str] = []

    sections.extend(goal_service.prompt_sections())

    timings: dict[str, float | bool] = {}
    trace_ctx = {"thread_id": thread_id, "soul_name": thread.soul_name}
    excluded_sources = {
        ("chat_message", str(message.id))
        for message in llm_messages
        if message.id > 0
    }
    stage_started = db.now_ts()
    # Web-search gate and query rewrite are independent yet used to run serially;
    # merge them into one LLM call, and overlap that call with the query-dependent
    # vector recall (both need only the raw user message), then execute the
    # (already-made) search decision. The prefetch is best-effort: on failure it
    # comes back None and memory assembly falls back to the current serial recall.
    prep, prefetched = reply_context.prepare_turn_with_prefetch(
        client,
        model,
        user_message=user_message,
        channel="chat",
        recent_turns=query_rewriter.recent_turns(llm_messages[:-1]),
        context_hint="\n\n---\n\n".join(sections),
        excluded_sources=excluded_sources,
        trace_context=trace_ctx,
    )
    rewrite = prep.rewritten
    web_section = reply_context.run_web_search_section(
        prep.search_decision,
        channel="chat",
        trace_context=trace_ctx,
    )
    # turn_prep_s spans the merged gate+rewrite call overlapped with the recall
    # prefetch, plus the search execution (what web_gate_s + query_rewrite_s covered
    # together before the merge).
    timings["turn_prep_s"] = round(db.now_ts() - stage_started, 3)
    if web_section:
        sections.append(web_section)

    stage_started = db.now_ts()
    memory = memory_read.memory_section_with_citations(
        "chat",
        thread.soul_name,
        user_message,
        excluded_sources=excluded_sources,
        semantic_query=rewrite.semantic_query,
        keywords=rewrite.keywords,
        prefetched=prefetched,
        trace_context=trace_ctx,
    )
    timings["memory_read_s"] = round(db.now_ts() - stage_started, 3)
    # Did the rewrite leave semantic_query on the prefetched raw query, letting the
    # unit ANN be reused instead of re-embedded? (Evidence recall reuses regardless.)
    timings["recall_prefetch_reused"] = bool(
        prefetched is not None and prefetched.query == (rewrite.semantic_query or user_message)
    )
    if memory.text:
        sections.append(f"# 记忆\n\n{memory.text}")

    context_text = "\n\n---\n\n".join(sections)
    logging_service.log_event(
        "context_assembly_result",
        channel="chat",
        context_type="chat",
        thread_id=thread_id,
        soul_name=thread.soul_name,
        sections=reply_context.section_summaries(sections),
        context_length=len(context_text),
        message_count=len(messages),
    )
    return ChatContext(
        thread=thread,
        soul=soul,
        context=context_text,
        messages=llm_messages,
        cited_memory=memory.cited_memory,
        timings=timings,
    )


def call_chat_reply(
    thread_id: int,
    user_message: str,
    client: LLMClient,
    model: str,
    attachment_ids: list[str] | None = None,
    *,
    request_id: str | None = None,
) -> ChatReplyResult:
    """Append user input, call one SOUL, and persist the assistant reply.

    A repeated ``request_id`` returns the already-persisted turn instead of
    appending or billing it twice."""
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    normalized_request_id = _normalize_client_request_id(request_id)
    with _request_lock(normalized_request_id):
        user_message_row = append_user_message(
            thread_id,
            user_message,
            attachment_ids=attachment_ids,
            client_request_id=normalized_request_id,
        )
        existing = _reply_result_for_request(user_message_row)
        if existing is not None:
            return existing
        return _call_assistant_reply_for_user_message(user_message_row, client, model)


def _call_assistant_reply_for_user_message(
    user_message_row: ChatMessage,
    client: LLMClient,
    model: str,
) -> ChatReplyResult:
    """Generate and append the assistant reply for an existing latest user message."""
    total_started = db.now_ts()
    llm_user_message = vision_service.content_for_llm(user_message_row.content, user_message_row.attachments)
    chat_context = build_chat_context(user_message_row.thread_id, llm_user_message, client, model)
    reply_started = db.now_ts()
    data = reply_router.call_soul_chat_reply(
        client,
        model,
        chat_context,
        chat_context.soul,
        trace_context=_reply_trace_context(user_message_row, chat_context),
    )
    _log_reply_latency(chat_context, user_message_row, reply_started, total_started)
    reply, error = _valid_reply_or_error(data)
    if error is not None:
        _log_reply_failed(chat_context, user_message_row, error)
        return _failed_result(chat_context.thread, user_message_row, error)
    return _finalize_chat_reply(chat_context, user_message_row, reply, client, model)


def stream_chat_reply(
    thread_id: int,
    content: str,
    client: LLMClient,
    model: str,
    attachment_ids: list[str] | None = None,
    *,
    request_id: str | None = None,
) -> Iterator[dict]:
    """Append user input and stream the assistant reply as a sequence of event
    dicts: ``{"type": "delta", "text": ...}`` per incremental chunk, then a final
    ``{"type": "done", "result": <ChatReplyResult dict>}`` emitted only after all
    post-processing (persist, suggestions, memory accounting) has finished."""
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    normalized_request_id = _normalize_client_request_id(request_id)
    with _request_lock(normalized_request_id):
        user_message_row = append_user_message(
            thread_id,
            content,
            attachment_ids=attachment_ids,
            client_request_id=normalized_request_id,
        )
        existing = _reply_result_for_request(user_message_row)
        if existing is not None:
            yield {"type": "done", "result": asdict(existing)}
            return
        yield from _stream_assistant_reply_for_user_message(user_message_row, client, model)


def _stream_assistant_reply_for_user_message(
    user_message_row: ChatMessage,
    client: LLMClient,
    model: str,
) -> Iterator[dict]:
    total_started = db.now_ts()
    llm_user_message = vision_service.content_for_llm(user_message_row.content, user_message_row.attachments)
    chat_context = build_chat_context(user_message_row.thread_id, llm_user_message, client, model)
    trace_context = _reply_trace_context(user_message_row, chat_context)
    reply_started = db.now_ts()

    # The router streams via an on_delta callback (blocking until done); bridge
    # that push-model into this pull-model generator with a queue + worker thread
    # so deltas reach the client incrementally. Only the LLM call runs on the
    # worker; every DB write stays on this thread, after the join.
    delta_queue: queue.Queue = queue.Queue()
    sentinel = object()
    outcome: dict = {}

    def on_delta(text: str) -> None:
        delta_queue.put(text)

    def run_stream() -> None:
        try:
            outcome["data"] = reply_router.call_soul_chat_reply_stream(
                client,
                model,
                chat_context,
                chat_context.soul,
                on_delta=on_delta,
                trace_context=trace_context,
            )
        except reply_router.ChatReplyStreamError as exc:
            outcome["data"] = None
            outcome["stream_error"] = str(exc)
        except Exception as exc:  # defensive: never let a worker crash strand the queue
            outcome["data"] = None
            outcome["stream_error"] = str(exc)
        finally:
            delta_queue.put(sentinel)

    worker = threading.Thread(target=run_stream, daemon=True)
    worker.start()
    while True:
        item = delta_queue.get()
        if item is sentinel:
            break
        yield {"type": "delta", "text": item}
    worker.join()

    data = outcome.get("data")
    if data is None:
        # Stream failed (transport error) or produced nothing: fall back to a
        # single non-streaming call, then run the shared post-processing.
        logging_service.log_event(
            "reply_stream_fallback",
            level="WARNING",
            channel="chat",
            thread_id=user_message_row.thread_id,
            soul_name=chat_context.thread.soul_name,
            user_message_id=user_message_row.id,
            reason="stream_error" if "stream_error" in outcome else "empty_stream",
        )
        data = reply_router.call_soul_chat_reply(
            client, model, chat_context, chat_context.soul, trace_context=trace_context
        )

    _log_reply_latency(chat_context, user_message_row, reply_started, total_started)
    reply, error = _valid_reply_or_error(data)
    if error is not None:
        _log_reply_failed(chat_context, user_message_row, error)
        result = _failed_result(chat_context.thread, user_message_row, error)
    else:
        result = _finalize_chat_reply(chat_context, user_message_row, reply, client, model)
    yield {"type": "done", "result": asdict(result)}


def _reply_trace_context(user_message_row: ChatMessage, chat_context: ChatContext) -> dict:
    return {
        "thread_id": user_message_row.thread_id,
        "soul_name": chat_context.thread.soul_name,
        "user_message_id": user_message_row.id,
    }


def _valid_reply_or_error(data: dict | None) -> tuple[str | None, str | None]:
    """Validate a reply payload; return (reply, None) or (None, error message)."""
    if data is None:
        return None, "LLM call failed or returned invalid JSON"
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None, "LLM response missing non-empty reply"
    return reply, None


def _log_reply_latency(
    chat_context: ChatContext,
    user_message_row: ChatMessage,
    reply_started: float,
    total_started: float,
) -> None:
    # latency breakdown across the serial legs (web gate -> rewrite -> memory
    # read -> reply LLM): measure first, parallelize only what proves slow.
    # For the streaming path reply_llm_s spans the first stream call through the
    # full text (extending across the non-streaming fallback when one runs).
    logging_service.log_event(
        "reply_latency",
        channel="chat",
        thread_id=user_message_row.thread_id,
        soul_name=chat_context.thread.soul_name,
        **chat_context.timings,
        reply_llm_s=round(db.now_ts() - reply_started, 3),
        total_s=round(db.now_ts() - total_started, 3),
    )


def _log_reply_failed(chat_context: ChatContext, user_message_row: ChatMessage, error: str) -> None:
    logging_service.log_event(
        "reply_failed",
        level="WARNING",
        channel="chat",
        thread_id=user_message_row.thread_id,
        soul_name=chat_context.thread.soul_name,
        user_message_id=user_message_row.id,
        error=error,
    )


def _finalize_chat_reply(
    chat_context: ChatContext,
    user_message_row: ChatMessage,
    reply: str,
    client: LLMClient,
    model: str,
) -> ChatReplyResult:
    """Shared post-reply pipeline (streaming + non-streaming): collect
    suggestions, persist the assistant message, and build the success result."""
    body = reply.strip()
    suggestions = suggestion_pipeline.collect_reply_suggestions(
        user_input=user_message_row.content,
        evidence_ref=f"chat:{user_message_row.id}",
        client=client,
        model=model,
        context=f"与 {chat_context.thread.soul_name} 的私聊",
        trace_context={
            "channel": "chat",
            "thread_id": user_message_row.thread_id,
            "soul_name": chat_context.thread.soul_name,
            "user_message_id": user_message_row.id,
        },
    )
    assistant_message = _append_message(
        user_message_row.thread_id,
        "assistant",
        body,
        metadata={
            "status": "ok",
            "memory_citations": memory_read.cited_memory_metadata_from(chat_context.cited_memory),
            "suggestions": suggestions,
        },
        client_request_id=user_message_row.client_request_id,
    )
    return ChatReplyResult(
        thread_id=user_message_row.thread_id,
        soul_name=chat_context.thread.soul_name,
        ok=True,
        reply=body,
        user_message_id=user_message_row.id,
        assistant_message_id=assistant_message.id,
        error=None,
        suggestions=suggestions,
    )


def _append_message(
    thread_id: int,
    role: str,
    content: str,
    *,
    attachment_ids: list[str] | None = None,
    metadata: dict | None = None,
    client_request_id: str | None = None,
) -> ChatMessage:
    thread = get_thread(thread_id)
    body = content.strip()
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    if not body and not attachment_ids and not _is_failed_reply_metadata(metadata):
        raise ValueError("私聊消息不能为空")
    now = db.now_ts()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO chat_messages(
                thread_id, role, content, created_at, metadata, client_request_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                role,
                body,
                now,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
                client_request_id,
            ),
        )
        message_id = db.require_lastrowid(cursor, "chat message insert")
        if body:
            memory_events_service.record_chat_mutation(
                conn,
                message_id=message_id,
                soul_name=thread.soul_name,
                op="create",
                content=body,
                occurred_at=now,
                role=role,
            )
        conn.execute(
            """
            UPDATE chat_threads
            SET updated_at = ?, last_message_at = ?
            WHERE id = ?
            """,
            (now, now, thread_id),
        )
    message = get_message(message_id)
    attachment_service.attach_to_chat_message(message.id, attachment_ids)
    message = get_message(message_id)
    if message.content.strip():
        record_service.index_chat_message_embedding(message.id, message.thread_id, thread.soul_name, message.role, message.content)
    return message


def get_message(message_id: int) -> ChatMessage:
    row = db.query_one(
        """
        SELECT id, thread_id, role, content, created_at, edited_at, rerun_at, metadata,
               client_request_id
        FROM chat_messages
        WHERE id = ?
        """,
        (message_id,),
    )
    if row is None:
        raise ValueError(f"私聊消息不存在：{message_id}")
    return _message_from_row(row)


def edit_user_message(message_id: int, content: str, attachment_ids: list[str] | None = None) -> dict:
    message = get_message(message_id)
    if message.role != "user":
        raise ValueError("只能编辑用户消息")
    body = content.strip()
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    if not body and not attachment_ids:
        raise ValueError("私聊消息不能为空")

    deleted_rows = _messages_after(message.thread_id, message.id)
    deleted_ids = [int(row["id"]) for row in deleted_rows]
    soul_name = get_thread(message.thread_id).soul_name
    now = db.now_ts()
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE chat_messages
            SET content = ?, edited_at = ?
            WHERE id = ?
            """,
            (body, now, message.id),
        )
        edit_event = memory_events_service.record_chat_mutation(
            conn, message_id=message.id, soul_name=soul_name, op="edit", content=body, occurred_at=now, role="user",
        )
        memory_unit_service.challenge_units_for_source(conn, edit_event.id)
        conn.execute("DELETE FROM chat_message_attachments WHERE message_id = ?", (message.id,))
        conn.executemany(
            """
            INSERT OR IGNORE INTO chat_message_attachments(message_id, attachment_id, sort_order)
            VALUES (?, ?, ?)
            """,
            [(message.id, attachment_id, index) for index, attachment_id in enumerate(attachment_ids)],
        )
        conn.executemany(
            "UPDATE attachments SET linked_at = COALESCE(linked_at, ?) WHERE id = ?",
            [(now, attachment_id) for attachment_id in attachment_ids],
        )
        if deleted_ids:
            for deleted_row in deleted_rows:
                delete_event = memory_events_service.record_chat_mutation(
                    conn,
                    message_id=int(deleted_row["id"]),
                    soul_name=soul_name,
                    op="delete",
                    content=None,
                    occurred_at=now,
                    role=str(deleted_row["role"]),
                )
                memory_unit_service.challenge_units_for_source(conn, delete_event.id)
            conn.execute(
                f"DELETE FROM chat_messages WHERE id IN ({','.join('?' for _ in deleted_ids)})",
                tuple(deleted_ids),
            )
        _refresh_thread_activity(conn, message.thread_id, now)

    for deleted_id in deleted_ids:
        record_service.delete_chat_message_embedding(deleted_id)
    updated = get_message(message.id)
    thread = get_thread(message.thread_id)
    record_service.index_chat_message_embedding(updated.id, updated.thread_id, thread.soul_name, updated.role, updated.content)
    job_service.enqueue_memory_reconcile_once(
        {"trigger": "chat_edit", "soul_name": soul_name}
    )
    return {
        "thread": thread,
        "message": updated,
        "messages": list_thread_messages(thread.id),
    }


def edit_user_message_and_reply(
    message_id: int,
    content: str,
    client: LLMClient,
    model: str,
    attachment_ids: list[str] | None = None,
) -> dict:
    edited = edit_user_message(message_id, content, attachment_ids=attachment_ids)
    result = _call_assistant_reply_for_user_message(edited["message"], client, model)
    thread = get_thread(edited["thread"].id)
    return {
        "thread": thread,
        "message": edited["message"],
        "result": result,
        "messages": list_thread_messages(thread.id),
    }


def rerun_assistant_message(message_id: int, client: LLMClient, model: str) -> dict:
    message = get_message(message_id)
    if message.role != "assistant":
        raise ValueError("只能重跑 SOUL 回复")
    thread = get_thread(message.thread_id)
    prior_messages = list_thread_messages(thread.id, limit=CHAT_HISTORY_LIMIT, before_message_id=message.id)
    user_message = _last_user_message_content_for_llm(prior_messages)
    if not user_message:
        raise ValueError("没有可用于重跑的用户消息")
    chat_context = build_chat_context(thread.id, user_message, client, model, before_message_id=message.id)
    data = reply_router.call_soul_chat_reply(
        client,
        model,
        chat_context,
        chat_context.soul,
        trace_context={
            "thread_id": thread.id,
            "soul_name": thread.soul_name,
            "rerun_message_id": message.id,
        },
    )
    if data is None:
        raise RuntimeError("chat rerun failed")
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError("chat rerun returned empty reply")

    deleted_rows = _messages_after(thread.id, message.id)
    deleted_ids = [int(row["id"]) for row in deleted_rows]
    now = db.now_ts()
    metadata = {
        "status": "ok",
        "model": model,
        "rerun": True,
        "memory_citations": memory_read.cited_memory_metadata_from(chat_context.cited_memory),
    }
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE chat_messages
            SET content = ?, metadata = ?, rerun_at = ?
            WHERE id = ?
            """,
            (reply.strip(), json.dumps(metadata, ensure_ascii=False), now, message.id),
        )
        memory_events_service.record_chat_mutation(
            conn, message_id=message.id, soul_name=thread.soul_name, op="rerun", content=reply.strip(), occurred_at=now, role="assistant",
        )
        if deleted_ids:
            for deleted_row in deleted_rows:
                delete_event = memory_events_service.record_chat_mutation(
                    conn,
                    message_id=int(deleted_row["id"]),
                    soul_name=thread.soul_name,
                    op="delete",
                    content=None,
                    occurred_at=now,
                    role=str(deleted_row["role"]),
                )
                memory_unit_service.challenge_units_for_source(conn, delete_event.id)
            conn.execute(
                f"DELETE FROM chat_messages WHERE id IN ({','.join('?' for _ in deleted_ids)})",
                tuple(deleted_ids),
            )
        _refresh_thread_activity(conn, thread.id, now)

    for deleted_id in deleted_ids:
        record_service.delete_chat_message_embedding(deleted_id)
    updated = get_message(message.id)
    thread = get_thread(thread.id)
    record_service.index_chat_message_embedding(updated.id, updated.thread_id, thread.soul_name, updated.role, updated.content)
    if any(str(row["role"]) == "user" for row in deleted_rows):
        job_service.enqueue_memory_reconcile_once(
            {"trigger": "chat_rerun_delete", "soul_name": thread.soul_name}
        )
    return {
        "thread": thread,
        "message": updated,
        "messages": list_thread_messages(thread.id),
    }


def _assert_soul_writable(soul_name: str) -> None:
    record = soul_service.get_soul(soul_name)
    if not record.enabled:
        raise ValueError(f"SOUL 已禁用，旧线程只读：{soul_name}")
    if not record.soul_exists:
        raise ValueError(f"SOUL 人格文件不存在，无法私聊：{soul_name}")


def _load_soul_context(soul_name: str) -> SoulContext:
    record = soul_service.get_soul(soul_name)
    soul = (db.WORKSPACE_DIR / record.file_path).read_text(encoding="utf-8")
    return SoulContext(
        name=record.name,
        description=record.description,
        sort_order=record.sort_order,
        soul=soul,
    )


def _last_user_message_content(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content.strip():
            return message.content.strip()
    return ""


def _last_user_message_content_for_llm(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and (message.content.strip() or message.attachments):
            return _message_for_llm(message).content.strip()
    return ""


def _message_ids_after(thread_id: int, message_id: int) -> list[int]:
    return [int(row["id"]) for row in _messages_after(thread_id, message_id)]


def _messages_after(thread_id: int, message_id: int) -> list:
    return db.query_all(
        """
        SELECT id, role
        FROM chat_messages
        WHERE thread_id = ? AND id > ?
        ORDER BY id ASC
        """,
        (thread_id, message_id),
    )


def _refresh_thread_activity(conn, thread_id: int, now: float) -> None:
    row = conn.execute(
        """
        SELECT created_at
        FROM chat_messages
        WHERE thread_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (thread_id,),
    ).fetchone()
    last_message_at = row["created_at"] if row is not None else None
    conn.execute(
        """
        UPDATE chat_threads
        SET updated_at = ?, last_message_at = ?
        WHERE id = ?
        """,
        (now, last_message_at, thread_id),
    )


def _is_failed_reply_metadata(metadata: dict | None) -> bool:
    return isinstance(metadata, dict) and metadata.get("status") == "failed"




def _failed_result(thread: ChatThread, user_message: ChatMessage, error: str) -> ChatReplyResult:
    assistant_message = _append_message(
        thread.id,
        "assistant",
        FAILED_REPLY_CONTENT,
        metadata={"status": "failed", "error": error},
        client_request_id=user_message.client_request_id,
    )
    return ChatReplyResult(
        thread_id=thread.id,
        soul_name=thread.soul_name,
        ok=False,
        reply=FAILED_REPLY_CONTENT,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        error=error,
        suggestions=[],
    )


def _thread_from_row(row) -> ChatThread:
    return ChatThread(
        id=row["id"],
        soul_name=row["soul_name"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_message_at=row["last_message_at"],
    )


def _message_from_row(row) -> ChatMessage:
    message_id = int(row["id"])
    return ChatMessage(
        id=message_id,
        thread_id=row["thread_id"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
        edited_at=float(row["edited_at"]) if row["edited_at"] is not None else None,
        rerun_at=float(row["rerun_at"]) if row["rerun_at"] is not None else None,
        metadata=row["metadata"],
        client_request_id=row["client_request_id"],
        attachments=attachment_service.list_chat_message_attachments(message_id),
    )


def _message_for_llm(message: ChatMessage) -> ChatMessage:
    content = vision_service.content_with_cached_summaries(message.content, message.attachments)
    return replace(message, content=content)
