"""Post comment conversation service keyed by (post_id, soul_name)."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from core import (
    db,
    attachment_service,
    evidence_service,
    logging_service,
    profile_service,
    record_service,
    reply_context,
    retrieval,
    soul_memory_service,
    soul_service,
    todo_service,
    tool_config_service,
    vision_service,
)
from core.attachment_service import Attachment
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext
from core.app_services import public_post_pipeline

COMMENT_HISTORY_LIMIT = 30
COMMENT_RELATED_MEMORY_LIMIT = 5
RETRIEVAL_USER_MESSAGE_LIMIT = 3
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
    retrieval_query: str
    relevant_post_ids: list[str]
    retrieval_hits: list


@dataclass(frozen=True)
class CommentReplyResult:
    post_id: str
    soul_name: str
    ok: bool
    reply: str
    user_message_id: int
    assistant_message_id: int | None
    error: str | None


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
        SELECT *
        FROM comments
        WHERE post_id = ? AND seq = 0
        ORDER BY created_at ASC, id ASC
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
    attachment_service.attach_to_comment(comment_id, attachment_ids)
    message = get_message(comment_id)
    if message.content.strip():
        record_service.index_comment_embedding(message.id, message.post_id, message.soul_name, message.role, message.seq, message.content)
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

    profile = profile_service.read_profile().strip()
    if profile:
        sections.append(f"# 用户档案\n\n{profile}")

    post = _get_post(post_id)
    if post is not None:
        post_content = _post_content_for_llm(post)
        sections.append(f"# 原始 post\n\n[{post['id']}] {post_content}")

    other_soul_context = _other_soul_comment_context(post_id, soul_name)
    if other_soul_context:
        sections.append(other_soul_context)

    retrieval_query = _build_comment_retrieval_query(post, llm_messages, user_message)
    rewritten_query = reply_context.rewrite_for_retrieval(
        client,
        model,
        retrieval_query,
        "comment",
        post_id=post_id,
        soul_name=soul_name,
    )
    retrieval_hits = reply_context.hybrid_search_documents_with_rewrite(
        retrieval_query,
        rewritten_query,
        k=COMMENT_RELATED_MEMORY_LIMIT,
        channel="comment",
        soul_name=soul_name,
        trace_context={"channel": "comment", "post_id": post_id, "soul_name": soul_name},
        exclusion=retrieval.RetrievalExclusion(
            post_ids=frozenset({post_id}),
            comment_post_ids=frozenset({post_id}),
        ),
    )
    related_memory = evidence_service.format_retrieval_hits(
        retrieval_hits,
        current_soul=soul_name,
    )
    if related_memory:
        sections.append(f"# 相关记忆\n\n{related_memory}")

    root_comment = _get_root_comment(post_id, soul_name)
    if include_root_comment and root_comment is not None:
        sections.append(f"# {soul_name} 的首条回复\n\n{root_comment['content']}")

    if tool_config_service.is_tool_enabled("todo"):
        pending = todo_service.list_active_todos()
        if pending:
            lines = [todo_service.format_todo_for_context(todo) for todo in pending]
            sections.append("# 待办事项\n\n" + "\n".join(lines))

    web_section = reply_context.build_web_search_section(
        client,
        model,
        user_message,
        channel="comment",
        context_hint="\n\n---\n\n".join(sections),
        trace_context={"post_id": post_id, "soul_name": soul_name},
    )
    if web_section:
        sections.append(web_section)

    context_text = "\n\n---\n\n".join(sections)
    relevant_post_ids = _post_ids_from_hits(retrieval_hits)
    logging_service.log_event(
        "context_assembly_result",
        channel="comment",
        context_type="comment",
        post_id=post_id,
        soul_name=soul_name,
        sections=reply_context.section_summaries(sections),
        relevant_post_ids=relevant_post_ids,
        retrieval_hit_count=len(retrieval_hits),
        context_length=len(context_text),
        message_count=len(messages),
    )
    return CommentContext(
        conversation=conversation,
        soul=soul,
        context=context_text,
        messages=llm_messages,
        retrieval_query=retrieval_query,
        relevant_post_ids=relevant_post_ids,
        retrieval_hits=retrieval_hits,
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
            "relevant_post_ids": comment_context.relevant_post_ids,
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

    assistant_message = append_comment(
        post_id,
        soul_name,
        "assistant",
        reply.strip(),
        metadata={
            "status": "ok",
            "evidence": evidence_service.evidence_metadata(comment_context.retrieval_hits),
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


def delete_message(message_id: int) -> dict:
    message = get_message(message_id)
    if message.role != "user":
        raise ValueError("只能删除用户评论；要删除 SOUL 回复，请删除它前面的用户评论或原 post")
    if message.seq == 0:
        rows = db.query_all(
            """
            SELECT id
            FROM comments
            WHERE post_id = ? AND soul_name = ?
            ORDER BY seq ASC, id ASC
            """,
            (message.post_id, message.soul_name),
        )
        deleted_ids = [int(row["id"]) for row in rows]
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM comments WHERE post_id = ? AND soul_name = ?",
                (message.post_id, message.soul_name),
            )
    else:
        rows = db.query_all(
            """
            SELECT id
            FROM comments
            WHERE post_id = ? AND soul_name = ? AND seq >= ?
            ORDER BY seq ASC, id ASC
            """,
            (message.post_id, message.soul_name, message.seq),
        )
        deleted_ids = [int(row["id"]) for row in rows]
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM comments WHERE post_id = ? AND soul_name = ? AND seq >= ?",
                (message.post_id, message.soul_name, message.seq),
            )

    for deleted_id in deleted_ids:
        record_service.delete_comment_embedding(deleted_id)
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
            "relevant_post_ids": context.relevant_post_ids,
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
        "evidence": evidence_service.evidence_metadata(context.retrieval_hits),
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
    data = reply_router.call_soul_post_reply(
        llm_content,
        client,
        model,
        public_context.built_context.shared_context,
        soul,
        trace_context={
            "post_id": message.post_id,
            "soul_name": message.soul_name,
            "rerun_comment_id": message.id,
            "relevant_post_ids": public_context.relevant_post_ids,
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
        "evidence": evidence_service.post_id_evidence_metadata(public_context.relevant_post_ids),
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

    sections = ["# 本帖其他评论区对话(其他 SOUL,公开评论背景)"]
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
    if role == "user":
        return "用户 · 追问"
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
    )


def _load_soul_context(soul_name: str) -> SoulContext:
    record = soul_service.get_soul(soul_name)
    soul = (db.WORKSPACE_DIR / record.file_path).read_text(encoding="utf-8")
    soul_memory = soul_memory_service.read_soul_memory(soul_name)
    return SoulContext(
        name=record.name,
        description=record.description,
        sort_order=record.sort_order,
        soul=soul,
        soul_memory=soul_memory,
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


def _build_comment_retrieval_query(post, messages: list[CommentMessage], user_message: str) -> str:
    parts: list[str] = []
    if post is not None:
        parts.append(_post_content_for_llm(post).strip())
    parts.extend(_recent_user_message_contents(messages, limit=RETRIEVAL_USER_MESSAGE_LIMIT))
    if parts:
        return "\n".join(part for part in parts if part)
    return user_message


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


def _recent_user_message_contents(messages: list[CommentMessage], limit: int) -> list[str]:
    contents = [
        message.content.strip()
        for message in messages
        if message.role == "user" and message.content.strip()
    ]
    return contents[-limit:]


def _post_ids_from_hits(hits: list) -> list[str]:
    post_ids: list[str] = []
    for hit in hits:
        post_id = None
        if getattr(hit, "type", None) == "post":
            post_id = str(getattr(hit, "source_id", ""))
        else:
            metadata = getattr(hit, "metadata", {}) or {}
            post_id = metadata.get("post_id")
        if post_id and post_id not in post_ids:
            post_ids.append(post_id)
    return post_ids
