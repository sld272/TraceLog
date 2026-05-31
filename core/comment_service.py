"""Post comment thread service for one-SOUL follow-up conversations."""

from __future__ import annotations

from dataclasses import dataclass
from core import db, evidence_service, logging_service, profile_service, reply_context, soul_memory_service, soul_service, todo_service, tool_config_service
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext


COMMENT_HISTORY_LIMIT = 30
COMMENT_RELATED_POST_LIMIT = 3
RETRIEVAL_USER_MESSAGE_LIMIT = 3
FAILED_COMMENT_REPLY = "这个 SOUL 暂时没有回复成功，稍后可以重试。"


@dataclass(frozen=True)
class CommentThread:
    id: int
    post_id: str
    soul_name: str
    root_comment_id: int
    created_at: float
    updated_at: float
    last_message_at: float | None


@dataclass(frozen=True)
class CommentMessage:
    id: int
    thread_id: int
    role: str
    content: str
    created_at: float


@dataclass(frozen=True)
class CommentContext:
    thread: CommentThread
    soul: SoulContext
    context: str
    messages: list[CommentMessage]
    retrieval_query: str
    relevant_post_ids: list[str]


@dataclass(frozen=True)
class CommentReplyResult:
    thread_id: int
    post_id: str
    soul_name: str
    ok: bool
    reply: str
    user_message_id: int
    assistant_message_id: int | None
    error: str | None


def get_or_create_thread(post_id: str, soul_name: str) -> CommentThread:
    """Return the comment thread for one SOUL under one post, creating it if needed."""
    _assert_soul_writable(soul_name)
    _assert_post_exists(post_id)
    root_comment = _get_root_comment(post_id, soul_name)
    if root_comment is None:
        raise ValueError(f"没有找到 {soul_name} 对 post {post_id} 的首条回复")

    row = db.query_one(
        """
        SELECT id, post_id, soul_name, root_comment_id, created_at, updated_at, last_message_at
        FROM comment_threads
        WHERE post_id = ? AND soul_name = ?
        """,
        (post_id, soul_name),
    )
    if row is not None:
        return _thread_from_row(row)

    now = db.now_ts()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO comment_threads(
                post_id, soul_name, root_comment_id, created_at, updated_at, last_message_at
            )
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (post_id, soul_name, root_comment["id"], now, now),
        )
        thread_id = db.require_lastrowid(cursor, "comment thread insert")
    return get_thread(thread_id)


def get_thread(thread_id: int) -> CommentThread:
    row = db.query_one(
        """
        SELECT id, post_id, soul_name, root_comment_id, created_at, updated_at, last_message_at
        FROM comment_threads
        WHERE id = ?
        """,
        (thread_id,),
    )
    if row is None:
        raise ValueError(f"评论线程不存在：{thread_id}")
    return _thread_from_row(row)


def list_post_threads(post_id: str) -> list[CommentThread]:
    _assert_post_exists(post_id)
    rows = db.query_all(
        """
        SELECT id, post_id, soul_name, root_comment_id, created_at, updated_at, last_message_at
        FROM comment_threads
        WHERE post_id = ?
        ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC, id DESC
        """,
        (post_id,),
    )
    return [_thread_from_row(row) for row in rows]


def list_thread_messages(thread_id: int, limit: int = COMMENT_HISTORY_LIMIT) -> list[CommentMessage]:
    get_thread(thread_id)
    rows = db.query_all(
        """
        SELECT id, thread_id, role, content, created_at
        FROM comment_messages
        WHERE thread_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (thread_id, limit),
    )
    return [_message_from_row(row) for row in reversed(rows)]


def list_thread_messages_after(thread_id: int, after_id: int, limit: int = 100) -> list[CommentMessage]:
    """List comment thread messages after a message id for API event streams."""
    get_thread(thread_id)
    rows = db.query_all(
        """
        SELECT id, thread_id, role, content, created_at
        FROM comment_messages
        WHERE thread_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (thread_id, max(0, int(after_id)), max(1, min(int(limit), 100))),
    )
    return [_message_from_row(row) for row in rows]


def append_user_message(thread_id: int, content: str) -> CommentMessage:
    thread = get_thread(thread_id)
    _assert_soul_writable(thread.soul_name)
    return _append_message(thread_id, "user", content)


def build_comment_context(
    thread_id: int,
    user_message: str,
    client: LLMClient | None = None,
    model: str | None = None,
) -> CommentContext:
    """Build prompt context after the current user message has been appended."""
    thread = get_thread(thread_id)
    soul = _load_soul_context(thread.soul_name)
    messages = list_thread_messages(thread_id, limit=COMMENT_HISTORY_LIMIT)
    sections: list[str] = []

    profile = profile_service.read_profile().strip()
    if profile:
        sections.append(f"# 用户档案\n\n{profile}")

    post = _get_post(thread.post_id)
    if post is not None:
        sections.append(f"# 原始 post\n\n[{post['id']}] {post['content']}")

    retrieval_query = _build_comment_retrieval_query(post, messages, user_message)
    rewritten_query = reply_context.rewrite_for_retrieval(client, model, retrieval_query, "comment_thread", thread_id=thread_id, post_id=thread.post_id, soul_name=thread.soul_name)
    relevant_post_ids = [
        post_id
        for post_id in reply_context.hybrid_search_with_rewrite(
            retrieval_query,
            rewritten_query,
            k=COMMENT_RELATED_POST_LIMIT,
            trace_context={
                "channel": "comment_thread",
                "thread_id": thread_id,
                "post_id": thread.post_id,
                "soul_name": thread.soul_name,
            },
        )
        if post_id != thread.post_id
    ]
    relevant_posts = evidence_service.read_posts_by_ids(relevant_post_ids)
    if relevant_posts:
        sections.append(f"# 相关帖子\n\n{relevant_posts}")

    related_comments = evidence_service.read_soul_comments(thread.soul_name, relevant_post_ids)
    if related_comments:
        sections.append(f"# 该 SOUL 对相关帖子的历史评论\n\n{related_comments}")

    root_comment = _get_comment(thread.root_comment_id)
    if root_comment is not None:
        sections.append(f"# {thread.soul_name} 的首条回复\n\n{root_comment['content']}")

    if tool_config_service.is_tool_enabled("todo"):
        pending = todo_service.list_active_todos()
        if pending:
            lines = [todo_service.format_todo_for_context(todo) for todo in pending]
            sections.append("# 待办事项\n\n" + "\n".join(lines))

    context_text = "\n\n---\n\n".join(sections)
    logging_service.log_event(
        "context_assembly_result",
        channel="comment_thread",
        context_type="comment_thread",
        thread_id=thread_id,
        post_id=thread.post_id,
        soul_name=thread.soul_name,
        sections=reply_context.section_summaries(sections),
        relevant_post_ids=relevant_post_ids,
        context_length=len(context_text),
        message_count=len(messages),
    )
    return CommentContext(
        thread=thread,
        soul=soul,
        context=context_text,
        messages=messages,
        retrieval_query=retrieval_query,
        relevant_post_ids=relevant_post_ids,
    )


def call_comment_reply(
    thread_id: int,
    user_message: str,
    client: LLMClient,
    model: str,
) -> CommentReplyResult:
    """Append user input, call one SOUL, and persist the assistant comment reply."""
    user_message_row = append_user_message(thread_id, user_message)
    comment_context = build_comment_context(thread_id, user_message, client, model)
    data = reply_router.call_soul_comment_reply(
        client,
        model,
        comment_context,
        comment_context.soul,
        trace_context={
            "thread_id": thread_id,
            "post_id": comment_context.thread.post_id,
            "soul_name": comment_context.thread.soul_name,
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
            thread_id=thread_id,
            post_id=comment_context.thread.post_id,
            soul_name=comment_context.thread.soul_name,
            user_message_id=user_message_row.id,
            error=error,
            fallback_reply=FAILED_COMMENT_REPLY,
        )
        return _failed_result(comment_context.thread, user_message_row.id, error)

    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        error = "LLM response missing non-empty reply"
        logging_service.log_event(
            "reply_failed",
            level="WARNING",
            channel="comment",
            thread_id=thread_id,
            post_id=comment_context.thread.post_id,
            soul_name=comment_context.thread.soul_name,
            user_message_id=user_message_row.id,
            error=error,
            fallback_reply=FAILED_COMMENT_REPLY,
        )
        return _failed_result(comment_context.thread, user_message_row.id, error)

    assistant_message = _append_message(thread_id, "assistant", reply.strip())
    return CommentReplyResult(
        thread_id=thread_id,
        post_id=comment_context.thread.post_id,
        soul_name=comment_context.thread.soul_name,
        ok=True,
        reply=reply.strip(),
        user_message_id=user_message_row.id,
        assistant_message_id=assistant_message.id,
        error=None,
    )


def get_message(message_id: int) -> CommentMessage:
    row = db.query_one(
        """
        SELECT id, thread_id, role, content, created_at
        FROM comment_messages
        WHERE id = ?
        """,
        (message_id,),
    )
    if row is None:
        raise ValueError(f"评论消息不存在：{message_id}")
    return _message_from_row(row)


def _append_message(thread_id: int, role: str, content: str) -> CommentMessage:
    body = content.strip()
    if not body:
        raise ValueError("评论消息不能为空")
    now = db.now_ts()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO comment_messages(thread_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (thread_id, role, body, now),
        )
        message_id = db.require_lastrowid(cursor, "comment message insert")
        conn.execute(
            """
            UPDATE comment_threads
            SET updated_at = ?, last_message_at = ?
            WHERE id = ?
            """,
            (now, now, thread_id),
        )
    return get_message(message_id)


def _failed_result(thread: CommentThread, user_message_id: int, error: str) -> CommentReplyResult:
    return CommentReplyResult(
        thread_id=thread.id,
        post_id=thread.post_id,
        soul_name=thread.soul_name,
        ok=False,
        reply=FAILED_COMMENT_REPLY,
        user_message_id=user_message_id,
        assistant_message_id=None,
        error=error,
    )


def _load_soul_context(soul_name: str) -> SoulContext:
    record = soul_service.get_soul(soul_name)
    persona = (db.WORKSPACE_DIR / record.file_path).read_text(encoding="utf-8")
    soul_memory = soul_memory_service.read_soul_memory(soul_name)
    return SoulContext(
        name=record.name,
        description=record.description,
        sort_order=record.sort_order,
        persona=persona,
        soul_memory=soul_memory,
    )


def _assert_soul_writable(soul_name: str) -> None:
    record = soul_service.get_soul(soul_name)
    if not record.enabled:
        raise ValueError(f"SOUL 已禁用，旧评论线程只读：{soul_name}")
    if not record.persona_exists:
        raise ValueError(f"SOUL 人格文件不存在，无法回复评论：{soul_name}")


def _assert_post_exists(post_id: str) -> None:
    if _get_post(post_id) is None:
        raise ValueError(f"post 不存在：{post_id}")


def _get_post(post_id: str):
    return db.query_one("SELECT id, ts, content FROM posts WHERE id = ?", (post_id,))


def _get_root_comment(post_id: str, soul_name: str):
    return db.query_one(
        """
        SELECT id, post_id, soul_name, content, created_at
        FROM comments
        WHERE post_id = ? AND soul_name = ?
        """,
        (post_id, soul_name),
    )


def _get_comment(comment_id: int):
    return db.query_one(
        """
        SELECT id, post_id, soul_name, content, created_at
        FROM comments
        WHERE id = ?
        """,
        (comment_id,),
    )


def _thread_from_row(row) -> CommentThread:
    return CommentThread(
        id=int(row["id"]),
        post_id=row["post_id"],
        soul_name=row["soul_name"],
        root_comment_id=int(row["root_comment_id"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        last_message_at=float(row["last_message_at"]) if row["last_message_at"] is not None else None,
    )


def _message_from_row(row) -> CommentMessage:
    return CommentMessage(
        id=int(row["id"]),
        thread_id=int(row["thread_id"]),
        role=row["role"],
        content=row["content"],
        created_at=float(row["created_at"]),
    )


def _build_comment_retrieval_query(post, messages: list[CommentMessage], user_message: str) -> str:
    parts: list[str] = []
    if post is not None:
        content = str(post["content"]).strip()
        if content:
            parts.append(content)
    parts.extend(_recent_user_message_contents(messages, limit=RETRIEVAL_USER_MESSAGE_LIMIT))
    if parts:
        return "\n".join(parts)
    return user_message


def _recent_user_message_contents(messages, limit: int) -> list[str]:
    contents = [
        message.content.strip()
        for message in messages
        if message.role == "user" and message.content.strip()
    ]
    return contents[-limit:]
