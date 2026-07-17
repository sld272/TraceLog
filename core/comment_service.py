"""Post comment conversation service keyed by (post_id, soul_name)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from core import (
    db,
    attachment_service,
    goal_service,
    logging_service,
    memory_events_service,
    memory_read,
    memory_unit_service,
    query_rewriter,
    record_service,
    reply_context,
    schedule_context,
    soul_service,
    suggestion_pipeline,
    vision_service,
)
from core.attachment_service import Attachment
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext
from core.app_services import job_service, public_post_pipeline

COMMENT_HISTORY_LIMIT = 30
OTHER_SOUL_THREAD_CONTEXT_LIMIT = 6


@dataclass(frozen=True)
class CommentConversation:
    post_id: str
    soul_name: str
    root_comment_id: int | None
    created_at: float | None
    updated_at: float | None
    last_message_at: float | None


@dataclass(frozen=True)
class CommentMessage:
    id: int
    post_id: str
    soul_name: str
    role: str
    content: str
    seq: int
    created_at: float
    edited_at: float | None
    rerun_at: float | None
    metadata: str | None
    attachments: list[Attachment]


@dataclass(frozen=True)
class CommentContext:
    conversation: CommentConversation
    soul: SoulContext
    context: str
    messages: list[CommentMessage]
    cited_memory: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CommentReplyResult:
    post_id: str
    soul_name: str
    ok: bool
    reply: str
    user_message_id: int
    assistant_message_id: int | None
    error: str | None
    suggestions: list[dict] = field(default_factory=list)


def get_conversation(post_id: str, soul_name: str) -> CommentConversation:
    _assert_soul_writable(soul_name)
    _assert_post_exists(post_id)
    root_comment = _get_root_comment(post_id, soul_name)
    if root_comment is None:
        raise ValueError(f"没有找到 {soul_name} 对 post {post_id} 的首条回复")
    return _conversation_from_root(post_id, soul_name, root_comment)


def list_post_conversations(post_id: str) -> list[CommentConversation]:
    _assert_post_exists(post_id)
    rows = db.query_all(
        """
        SELECT comments.*
        FROM comments
        LEFT JOIN post_soul_orders
          ON post_soul_orders.post_id = comments.post_id
         AND post_soul_orders.soul_name = comments.soul_name
        WHERE comments.post_id = ? AND comments.seq = 0
        ORDER BY
            CASE WHEN post_soul_orders.sort_order IS NULL THEN 1 ELSE 0 END ASC,
            post_soul_orders.sort_order ASC,
            CASE WHEN post_soul_orders.sort_order IS NOT NULL THEN comments.soul_name END ASC,
            comments.created_at ASC,
            comments.id ASC
        """,
        (post_id,),
    )
    return [_conversation_from_root(post_id, row["soul_name"], row) for row in rows]


def list_conversation_messages(
    post_id: str,
    soul_name: str,
    limit: int = COMMENT_HISTORY_LIMIT,
    *,
    include_root: bool = True,
    before_seq: int | None = None,
) -> list[CommentMessage]:
    get_conversation(post_id, soul_name)
    min_seq = 0 if include_root else 1
    before_clause = ""
    params: list = [post_id, soul_name, min_seq]
    if before_seq is not None:
        before_clause = "AND seq < ?"
        params.append(int(before_seq))
    params.append(limit)
    rows = db.query_all(
        f"""
        SELECT id, post_id, soul_name, role, content, seq, metadata, created_at, edited_at, rerun_at
        FROM comments
        WHERE post_id = ? AND soul_name = ? AND seq >= ?
        {before_clause}
        ORDER BY seq DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [_message_from_row(row) for row in reversed(rows)]


def list_conversation_messages_after(
    post_id: str,
    soul_name: str,
    after_id: int,
    limit: int = 100,
) -> list[CommentMessage]:
    get_conversation(post_id, soul_name)
    rows = db.query_all(
        """
        SELECT id, post_id, soul_name, role, content, seq, metadata, created_at, edited_at, rerun_at
        FROM comments
        WHERE post_id = ? AND soul_name = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (post_id, soul_name, max(0, int(after_id)), max(1, min(int(limit), 100))),
    )
    return [_message_from_row(row) for row in rows]


def append_comment(
    post_id: str,
    soul_name: str,
    role: str,
    content: str,
    attachment_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> CommentMessage:
    if role not in {"user", "assistant"}:
        raise ValueError(f"非法评论角色：{role}")
    _assert_soul_writable(soul_name)
    _assert_post_exists(post_id)
    if _get_root_comment(post_id, soul_name) is None:
        raise ValueError(f"没有找到 {soul_name} 对 post {post_id} 的首条回复")
    body = content.strip()
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    if not body and not attachment_ids and not _is_failed_reply_metadata(metadata):
        raise ValueError("评论消息不能为空")
    now = db.now_ts()
    with db.immediate_transaction() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS max_seq FROM comments WHERE post_id = ? AND soul_name = ?",
            (post_id, soul_name),
        ).fetchone()
        seq = int(row["max_seq"]) + 1
        cursor = conn.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                soul_name,
                role,
                body,
                seq,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
                now,
            ),
        )
        comment_id = db.require_lastrowid(cursor, "comment insert")
        if body:
            memory_events_service.record_comment_mutation(
                conn,
                comment_id=comment_id,
                post_id=post_id,
                soul_name=soul_name,
                role=role,
                op="create",
                content=body,
                occurred_at=now,
            )
    attachment_service.attach_to_comment(comment_id, attachment_ids)
    message = get_message(comment_id)
    if message.content.strip():
        record_service.index_comment_embedding(message.id, message.post_id, message.soul_name, message.role, message.seq, message.content)
    if role == "user" and body:
        # Only the user's own comments are belief-generating evidence.
        job_service.enqueue_memory_reconcile_once({"trigger": "comment", "post_id": post_id})
    return message


def build_comment_context(
    post_id: str,
    soul_name: str,
    user_message: str,
    client: LLMClient | None = None,
    model: str | None = None,
    *,
    before_seq: int | None = None,
    include_root_comment: bool = True,
) -> CommentContext:
    conversation = get_conversation(post_id, soul_name)
    soul = _load_soul_context(soul_name)
    messages = list_conversation_messages(
        post_id,
        soul_name,
        limit=COMMENT_HISTORY_LIMIT,
        include_root=False,
        before_seq=before_seq,
    )
    llm_messages = [_message_for_llm(message) for message in messages]

    # Rerunning a root assistant comment has no persisted follow-up user row,
    # so provide a synthetic current user message. Normal comment replies have
    # already appended the current user row before building context.
    if _should_append_synthetic_user_message(llm_messages, user_message):
        user_msg = CommentMessage(
            id=-1,
            post_id=post_id,
            soul_name=soul_name,
            role="user",
            content=user_message.strip(),
            seq=-1,
            created_at=0,
            edited_at=None,
            rerun_at=None,
            metadata=None,
            attachments=[],
        )
        llm_messages.append(user_msg)

    sections: list[str] = []

    sections.extend(goal_service.prompt_sections())
    recent_schedule = schedule_context.build_recent_schedule_context()
    if recent_schedule.section:
        sections.append(recent_schedule.section)

    post = _get_post(post_id)
    if post is not None:
        post_content = _post_content_for_llm(post)
        sections.append(f"# 原始 post\n\n[{post['id']}] {post_content}")

    other_soul_context = _other_soul_comment_context(post_id, soul_name)
    if other_soul_context:
        sections.append(other_soul_context)

    root_comment = _get_root_comment(post_id, soul_name)
    if include_root_comment and root_comment is not None:
        sections.append(f"# {soul_name} 的首条回复\n\n{root_comment['content']}")

    trace_ctx = {"post_id": post_id, "soul_name": soul_name}
    # Exclude EVERY comment under this post (all SOULs' threads) from the memory
    # section. Public-post comments all share the global/public bucket, so the
    # freshness seam would otherwise surface the user's parallel comments to OTHER
    # SOULs as the current user's "recent evidence" and pull the reply off-topic
    # (cross-talk). The current thread is already the live multi-turn conversation,
    # and other threads are shown as labeled background — neither belongs in memory.
    # Computed up front (a read-only SELECT) so the recall prefetch and the memory
    # assembly share one excluded set.
    excluded_comment_sources = {
        ("comment_message", str(row["id"]))
        for row in db.query_all("SELECT id FROM comments WHERE post_id = ?", (post_id,))
    }
    # Web-search gate and query rewrite are independent yet used to run serially;
    # merge them into one LLM call, and overlap that call with the query-dependent
    # vector recall (both need only the raw user message), then execute the
    # (already-made) search decision. The prefetch is best-effort: on failure it
    # comes back None and memory assembly falls back to the current serial recall.
    prep, prefetched = reply_context.prepare_turn_with_prefetch(
        client,
        model,
        user_message=user_message,
        channel="comment",
        recent_turns=query_rewriter.recent_turns(llm_messages[:-1]),
        context_hint="\n\n---\n\n".join(sections),
        excluded_sources=excluded_comment_sources,
        trace_context=trace_ctx,
    )
    mentioned_schedule = schedule_context.build_mentioned_schedule_section(
        prep.rewritten.keywords,
        exclude_event_ids=recent_schedule.event_ids,
    )
    if mentioned_schedule:
        sections.append(mentioned_schedule)
    web_section = reply_context.run_web_search_section(
        prep.search_decision,
        channel="comment",
        trace_context=trace_ctx,
    )
    if web_section:
        sections.append(web_section)

    rewrite = prep.rewritten
    memory = memory_read.memory_section_with_citations(
        "comment",
        soul_name,
        user_message,
        excluded_sources=excluded_comment_sources,
        semantic_query=rewrite.semantic_query,
        keywords=rewrite.keywords,
        prefetched=prefetched,
        trace_context=trace_ctx,
    )
    if memory.text:
        sections.append(f"# 记忆\n\n{memory.text}")

    context_text = "\n\n---\n\n".join(sections)
    logging_service.log_event(
        "context_assembly_result",
        channel="comment",
        context_type="comment",
        post_id=post_id,
        soul_name=soul_name,
        sections=reply_context.section_summaries(sections),
        context_length=len(context_text),
        message_count=len(messages),
    )
    return CommentContext(
        conversation=conversation,
        soul=soul,
        context=context_text,
        messages=llm_messages,
        cited_memory=memory.cited_memory,
    )


def call_comment_reply(
    post_id: str,
    soul_name: str,
    user_message: str,
    client: LLMClient,
    model: str,
    attachment_ids: list[str] | None = None,
) -> CommentReplyResult:
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    user_message_row = append_comment(post_id, soul_name, "user", user_message, attachment_ids=attachment_ids)
    llm_user_message = vision_service.content_for_llm(user_message, user_message_row.attachments)
    comment_context = build_comment_context(post_id, soul_name, llm_user_message, client, model)
    data = reply_router.call_soul_comment_reply(
        client,
        model,
        comment_context,
        comment_context.soul,
        trace_context={
            "post_id": post_id,
            "soul_name": soul_name,
            "user_message_id": user_message_row.id,
        },
    )
    if data is None:
        error = "LLM call failed or returned invalid JSON"
        logging_service.log_event(
            "reply_failed",
            level="WARNING",
            channel="comment",
            post_id=post_id,
            soul_name=soul_name,
            user_message_id=user_message_row.id,
            error=error,
        )
        return _failed_result(post_id, soul_name, user_message_row.id, error)

    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        error = "LLM response missing non-empty reply"
        logging_service.log_event(
            "reply_failed",
            level="WARNING",
            channel="comment",
            post_id=post_id,
            soul_name=soul_name,
            user_message_id=user_message_row.id,
            error=error,
        )
        return _failed_result(post_id, soul_name, user_message_row.id, error)

    suggestions = suggestion_pipeline.collect_reply_suggestions(
        user_input=user_message_row.content,
        evidence_ref=f"comment:{user_message_row.id}",
        client=client,
        model=model,
        context=f"post {post_id} 下与 {soul_name} 的评论对话",
        trace_context={
            "channel": "comment",
            "post_id": post_id,
            "soul_name": soul_name,
            "user_message_id": user_message_row.id,
        },
    )
    assistant_message = append_comment(
        post_id,
        soul_name,
        "assistant",
        reply.strip(),
        metadata={
            "status": "ok",
            "memory_citations": memory_read.cited_memory_metadata_from(comment_context.cited_memory),
            "suggestions": suggestions,
        },
    )
    return CommentReplyResult(
        post_id=post_id,
        soul_name=soul_name,
        ok=True,
        reply=reply.strip(),
        user_message_id=user_message_row.id,
        assistant_message_id=assistant_message.id,
        error=None,
        suggestions=suggestions,
    )


def get_message(message_id: int) -> CommentMessage:
    row = db.query_one(
        """
        SELECT id, post_id, soul_name, role, content, seq, metadata, created_at, edited_at, rerun_at
        FROM comments
        WHERE id = ?
        """,
        (message_id,),
    )
    if row is None:
        raise ValueError(f"评论消息不存在：{message_id}")
    return _message_from_row(row)


def _record_comment_deletes(conn, post_id: str, soul_name: str, deleted_rows: list[tuple[int, str]]) -> None:
    now = db.now_ts()
    for comment_id, role in deleted_rows:
        event = memory_events_service.record_comment_mutation(
            conn,
            comment_id=comment_id,
            post_id=post_id,
            soul_name=soul_name,
            role=role,
            op="delete",
            content=None,
            occurred_at=now,
        )
        memory_unit_service.challenge_units_for_source(conn, event.id)


def delete_message(message_id: int) -> dict:
    message = get_message(message_id)
    if message.role != "user":
        raise ValueError("只能删除用户评论；要删除 SOUL 回复，请删除它前面的用户评论或原 post")
    if message.seq == 0:
        rows = db.query_all(
            """
            SELECT id, role
            FROM comments
            WHERE post_id = ? AND soul_name = ?
            ORDER BY seq ASC, id ASC
            """,
            (message.post_id, message.soul_name),
        )
        deleted_rows = [(int(row["id"]), str(row["role"])) for row in rows]
        deleted_ids = [item[0] for item in deleted_rows]
        with db.transaction() as conn:
            _record_comment_deletes(conn, message.post_id, message.soul_name, deleted_rows)
            conn.execute(
                "DELETE FROM comments WHERE post_id = ? AND soul_name = ?",
                (message.post_id, message.soul_name),
            )
    else:
        rows = db.query_all(
            """
            SELECT id, role
            FROM comments
            WHERE post_id = ? AND soul_name = ? AND seq >= ?
            ORDER BY seq ASC, id ASC
            """,
            (message.post_id, message.soul_name, message.seq),
        )
        deleted_rows = [(int(row["id"]), str(row["role"])) for row in rows]
        deleted_ids = [item[0] for item in deleted_rows]
        with db.transaction() as conn:
            _record_comment_deletes(conn, message.post_id, message.soul_name, deleted_rows)
            conn.execute(
                "DELETE FROM comments WHERE post_id = ? AND soul_name = ? AND seq >= ?",
                (message.post_id, message.soul_name, message.seq),
            )

    for deleted_id in deleted_ids:
        record_service.delete_comment_embedding(deleted_id)
    if any(role == "user" for _, role in deleted_rows):
        job_service.enqueue_memory_reconcile_once(
            {"trigger": "comment_delete", "post_id": message.post_id}
        )
    return {
        "ok": True,
        "post_id": message.post_id,
        "soul_name": message.soul_name,
        "deleted_message_ids": deleted_ids,
    }


def rerun_latest_assistant_message(message_id: int, client: LLMClient, model: str) -> dict:
    message = get_message(message_id)
    latest = _latest_conversation_message(message.post_id, message.soul_name)
    if latest is None or latest.id != message.id or latest.role != "assistant":
        raise ValueError("只能重跑最新一条 SOUL 回复")

    post = _get_post(message.post_id)
    if post is None:
        raise ValueError(f"post 不存在：{message.post_id}")

    if message.seq == 0:
        return _rerun_root_assistant_message(message, post, client, model)

    prior_messages = list_conversation_messages(
        message.post_id,
        message.soul_name,
        limit=COMMENT_HISTORY_LIMIT,
        include_root=False,
        before_seq=message.seq,
    )
    rerun_user_message = _rerun_user_message(post, prior_messages)
    context = build_comment_context(
        message.post_id,
        message.soul_name,
        rerun_user_message,
        client,
        model,
        before_seq=message.seq,
        include_root_comment=message.seq != 0,
    )
    data = reply_router.call_soul_comment_reply(
        client,
        model,
        context,
        context.soul,
        trace_context={
            "post_id": message.post_id,
            "soul_name": message.soul_name,
            "rerun_comment_id": message.id,
        },
    )
    if data is None:
        return _mark_existing_assistant_failed(message, "comment rerun failed")
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return _mark_existing_assistant_failed(message, "comment rerun returned empty reply")

    now = db.now_ts()
    metadata = {
        "status": "ok",
        "model": model,
        "rerun": True,
        "memory_citations": memory_read.cited_memory_metadata_from(context.cited_memory),
    }
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE comments
            SET content = ?, metadata = ?, rerun_at = ?
            WHERE id = ?
            """,
            (reply.strip(), json.dumps(metadata, ensure_ascii=False), now, message.id),
        )
        memory_events_service.record_comment_mutation(
            conn,
            comment_id=message.id,
            post_id=message.post_id,
            soul_name=message.soul_name,
            role="assistant",
            op="rerun",
            content=reply.strip(),
            occurred_at=now,
        )

    updated = get_message(message.id)
    record_service.index_comment_embedding(
        updated.id,
        updated.post_id,
        updated.soul_name,
        updated.role,
        updated.seq,
        updated.content,
    )
    return {
        "message": updated,
        "conversation": get_conversation(updated.post_id, updated.soul_name),
        "messages": list_conversation_messages(updated.post_id, updated.soul_name),
    }


def _rerun_root_assistant_message(message: CommentMessage, post, client: LLMClient, model: str) -> dict:
    _assert_soul_writable(message.soul_name)
    llm_content = _post_content_for_llm(post)
    public_context = public_post_pipeline.build_public_post_reply_context(
        message.post_id,
        llm_content,
        client,
        model,
        trace_context={
            "channel": "public_post",
            "post_id": message.post_id,
            "soul_name": message.soul_name,
            "rerun_comment_id": message.id,
        },
    )
    soul = _load_soul_context(message.soul_name)
    # build_public_post_reply_context already ran turn_prep (gate + rewrite) while
    # assembling the shared context, so reuse that rewrite instead of a second call.
    rewrite = public_context.built_context.rewritten
    if rewrite is None:
        rewrite = query_rewriter.rewrite_query(
            client,
            model,
            llm_content,
            "public_post",
            trace_context={"post_id": message.post_id, "soul_name": message.soul_name},
        )
    memory = memory_read.memory_section_with_citations(
        "public_post",
        message.soul_name,
        llm_content,
        excluded_sources={("post", message.post_id), ("post_vision", message.post_id)},
        semantic_query=rewrite.semantic_query,
        keywords=rewrite.keywords,
        trace_context={"post_id": message.post_id, "soul_name": message.soul_name},
    )
    shared_context = public_context.built_context.shared_context
    soul_context = (
        f"{shared_context}\n\n---\n\n# 记忆\n\n{memory.text}" if memory.text and shared_context
        else f"# 记忆\n\n{memory.text}" if memory.text
        else shared_context
    )
    data = reply_router.call_soul_post_reply(
        llm_content,
        client,
        model,
        soul_context,
        soul,
        trace_context={
            "post_id": message.post_id,
            "soul_name": message.soul_name,
            "rerun_comment_id": message.id,
        },
    )
    if data is None:
        return _mark_existing_assistant_failed(message, "root comment rerun failed")
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return _mark_existing_assistant_failed(message, "root comment rerun returned empty reply")

    now = db.now_ts()
    metadata = {
        "status": "ok",
        "model": model,
        "rerun": True,
        "memory_citations": memory_read.cited_memory_metadata_from(memory.cited_memory),
    }
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE comments
            SET content = ?, metadata = ?, rerun_at = ?
            WHERE id = ?
            """,
            (reply.strip(), json.dumps(metadata, ensure_ascii=False), now, message.id),
        )
        memory_events_service.record_comment_mutation(
            conn,
            comment_id=message.id,
            post_id=message.post_id,
            soul_name=message.soul_name,
            role="assistant",
            op="rerun",
            content=reply.strip(),
            occurred_at=now,
        )

    updated = get_message(message.id)
    record_service.index_comment_embedding(
        updated.id,
        updated.post_id,
        updated.soul_name,
        updated.role,
        updated.seq,
        updated.content,
    )
    return {
        "message": updated,
        "conversation": get_conversation(updated.post_id, updated.soul_name),
        "messages": list_conversation_messages(updated.post_id, updated.soul_name),
    }


def _other_soul_comment_context(post_id: str, current_soul: str) -> str:
    threads = db.query_all(
        """
        SELECT soul_name, MAX(created_at) AS last_message_at
        FROM comments
        WHERE post_id = ? AND soul_name <> ?
        GROUP BY soul_name
        HAVING SUM(CASE WHEN role = 'user' AND seq > 0 THEN 1 ELSE 0 END) > 0
        ORDER BY last_message_at DESC, soul_name ASC
        """,
        (post_id, current_soul),
    )
    if not threads:
        return ""

    sections = [
        "# 本帖其他评论区（公开氛围，仅供你知道）\n"
        "下面是用户在本帖和**其他 SOUL** 的公开互动。默认不要把这里的话题扯进你的回复，"
        "也不要把这些（用户对别人说的）消息当成对你说的；**只有**当它们和用户这次"
        "对你说的话**直接相关**（如自相矛盾、可自然呼应）时，才可顺势点到。"
    ]
    for thread in threads:
        soul_name = str(thread["soul_name"])
        rows = db.query_all(
            """
            SELECT role, content, seq
            FROM comments
            WHERE post_id = ? AND soul_name = ?
            ORDER BY seq DESC, id DESC
            LIMIT ?
            """,
            (post_id, soul_name, OTHER_SOUL_THREAD_CONTEXT_LIMIT),
        )
        rows = list(reversed(rows))
        if not rows:
            continue
        lines = [f"## {soul_name}"]
        for row in rows:
            label = _comment_context_label(str(row["role"]), int(row["seq"]), soul_name)
            lines.append(f"[{label}] {row['content']}")
        sections.append("\n".join(lines))
    if len(sections) == 1:
        return ""
    return "\n\n".join(sections)


def _comment_context_label(role: str, seq: int, soul_name: str) -> str:
    # Used ONLY for other SOULs' threads shown as background. The user line here
    # is the user talking TO that other SOUL — label it explicitly so the current
    # SOUL never mistakes it for a follow-up addressed to itself (which crossed
    # wires when the user replied to several SOULs at once).
    if role == "user":
        return f"用户对 {soul_name} 说"
    if seq == 0:
        return f"{soul_name} · 首评"
    return f"{soul_name} · 回复"


def _mark_existing_assistant_failed(message: CommentMessage, error: str) -> dict:
    now = db.now_ts()
    metadata = {"status": "failed", "error": error, "rerun": True}
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE comments
            SET content = '', metadata = ?, rerun_at = ?
            WHERE id = ?
            """,
            (json.dumps(metadata, ensure_ascii=False), now, message.id),
        )

    updated = get_message(message.id)
    return {
        "message": updated,
        "conversation": get_conversation(updated.post_id, updated.soul_name),
        "messages": list_conversation_messages(updated.post_id, updated.soul_name),
    }


def _failed_result(post_id: str, soul_name: str, user_message_id: int, error: str) -> CommentReplyResult:
    assistant_message = append_comment(
        post_id,
        soul_name,
        "assistant",
        "",
        metadata={"status": "failed", "error": error},
    )
    return CommentReplyResult(
        post_id=post_id,
        soul_name=soul_name,
        ok=False,
        reply="",
        user_message_id=user_message_id,
        assistant_message_id=assistant_message.id,
        error=error,
        suggestions=[],
    )


def _load_soul_context(soul_name: str) -> SoulContext:
    record = soul_service.get_soul(soul_name)
    soul = (db.WORKSPACE_DIR / record.file_path).read_text(encoding="utf-8")
    return SoulContext(
        name=record.name,
        description=record.description,
        sort_order=record.sort_order,
        soul=soul,
    )


def _assert_soul_writable(soul_name: str) -> None:
    record = soul_service.get_soul(soul_name)
    if not record.enabled:
        raise ValueError(f"SOUL 已禁用，旧评论只读：{soul_name}")
    if not record.soul_exists:
        raise ValueError(f"SOUL 人格文件不存在，无法回复评论：{soul_name}")


def _assert_post_exists(post_id: str) -> None:
    if db.query_one("SELECT 1 FROM posts WHERE id = ?", (post_id,)) is None:
        raise ValueError(f"post 不存在：{post_id}")


def _get_post(post_id: str):
    return db.query_one("SELECT id, ts, content FROM posts WHERE id = ?", (post_id,))


def _get_root_comment(post_id: str, soul_name: str):
    return db.query_one(
        """
        SELECT id, post_id, soul_name, role, content, seq, metadata, created_at
        FROM comments
        WHERE post_id = ? AND soul_name = ? AND seq = 0
        """,
        (post_id, soul_name),
    )


def _latest_conversation_message(post_id: str, soul_name: str) -> CommentMessage | None:
    row = db.query_one(
        """
        SELECT id, post_id, soul_name, role, content, seq, metadata, created_at, edited_at, rerun_at
        FROM comments
        WHERE post_id = ? AND soul_name = ?
        ORDER BY seq DESC, id DESC
        LIMIT 1
        """,
        (post_id, soul_name),
    )
    return _message_from_row(row) if row is not None else None


def _conversation_from_root(post_id: str, soul_name: str, root_comment) -> CommentConversation:
    activity = db.query_one(
        """
        SELECT MAX(created_at) AS last_message_at, MAX(id) AS last_id
        FROM comments
        WHERE post_id = ? AND soul_name = ? AND seq > 0
        """,
        (post_id, soul_name),
    )
    last_message_at = activity["last_message_at"] if activity is not None else None
    return CommentConversation(
        post_id=post_id,
        soul_name=soul_name,
        root_comment_id=int(root_comment["id"]) if root_comment is not None else None,
        created_at=float(root_comment["created_at"]) if root_comment is not None else None,
        updated_at=float(last_message_at or root_comment["created_at"]) if root_comment is not None else None,
        last_message_at=float(last_message_at) if last_message_at is not None else None,
    )


def _message_from_row(row) -> CommentMessage:
    message_id = int(row["id"])
    return CommentMessage(
        id=message_id,
        post_id=row["post_id"],
        soul_name=row["soul_name"],
        role=row["role"],
        content=row["content"],
        seq=int(row["seq"]),
        created_at=float(row["created_at"]),
        edited_at=float(row["edited_at"]) if row["edited_at"] is not None else None,
        rerun_at=float(row["rerun_at"]) if row["rerun_at"] is not None else None,
        metadata=row["metadata"],
        attachments=attachment_service.list_comment_attachments(message_id),
    )


def _is_failed_reply_metadata(metadata: dict | None) -> bool:
    return isinstance(metadata, dict) and metadata.get("status") == "failed"


def _message_for_llm(message: CommentMessage) -> CommentMessage:
    content = vision_service.content_with_cached_summaries(message.content, message.attachments)
    return replace(message, content=content)


def _should_append_synthetic_user_message(messages: list[CommentMessage], user_message: str) -> bool:
    body = user_message.strip()
    if not body:
        return False
    if not messages:
        return True
    latest = messages[-1]
    return latest.role != "user" or latest.content.strip() != body


def _rerun_user_message(post, prior_messages: list[CommentMessage]) -> str:
    for message in reversed(prior_messages):
        if message.role == "user" and (message.content.strip() or message.attachments):
            return _message_for_llm(message).content.strip()
    return _post_content_for_llm(post)


def _post_content_for_llm(post) -> str:
    content = str(post["content"] or "")
    vision_context = vision_service.cached_context_for_post(str(post["id"]))
    if vision_context:
        return f"{content}\n\n{vision_context}" if content.strip() else vision_context
    attachment_count = len(attachment_service.list_post_attachments(str(post["id"])))
    return attachment_service.content_for_llm(content, attachment_count)
