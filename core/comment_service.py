"""Post comment conversation service keyed by (post_id, soul_name)."""

from __future__ import annotations

from dataclasses import dataclass

from core import (
    db,
    evidence_service,
    logging_service,
    profile_service,
    record_service,
    reply_context,
    soul_memory_service,
    soul_service,
    todo_service,
    tool_config_service,
)
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext

COMMENT_HISTORY_LIMIT = 30
COMMENT_RELATED_MEMORY_LIMIT = 5
RETRIEVAL_USER_MESSAGE_LIMIT = 3
FAILED_COMMENT_REPLY = "这个 SOUL 暂时没有回复成功，稍后可以重试。"


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
) -> list[CommentMessage]:
    get_conversation(post_id, soul_name)
    min_seq = 0 if include_root else 1
    rows = db.query_all(
        """
        SELECT id, post_id, soul_name, role, content, seq, created_at
        FROM comments
        WHERE post_id = ? AND soul_name = ? AND seq >= ?
        ORDER BY seq DESC
        LIMIT ?
        """,
        (post_id, soul_name, min_seq, limit),
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
        SELECT id, post_id, soul_name, role, content, seq, created_at
        FROM comments
        WHERE post_id = ? AND soul_name = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (post_id, soul_name, max(0, int(after_id)), max(1, min(int(limit), 100))),
    )
    return [_message_from_row(row) for row in rows]


def append_comment(post_id: str, soul_name: str, role: str, content: str) -> CommentMessage:
    if role not in {"user", "assistant"}:
        raise ValueError(f"非法评论角色：{role}")
    _assert_soul_writable(soul_name)
    _assert_post_exists(post_id)
    if _get_root_comment(post_id, soul_name) is None:
        raise ValueError(f"没有找到 {soul_name} 对 post {post_id} 的首条回复")
    body = content.strip()
    if not body:
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
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            (post_id, soul_name, role, body, seq, now),
        )
        comment_id = db.require_lastrowid(cursor, "comment insert")
    message = get_message(comment_id)
    record_service.index_comment_embedding(message.id, message.post_id, message.soul_name, message.role, message.seq, message.content)
    return message


def build_comment_context(
    post_id: str,
    soul_name: str,
    user_message: str,
    client: LLMClient | None = None,
    model: str | None = None,
) -> CommentContext:
    conversation = get_conversation(post_id, soul_name)
    soul = _load_soul_context(soul_name)
    messages = list_conversation_messages(post_id, soul_name, limit=COMMENT_HISTORY_LIMIT, include_root=False)
    sections: list[str] = []

    profile = profile_service.read_profile().strip()
    if profile:
        sections.append(f"# 用户档案\n\n{profile}")

    post = _get_post(post_id)
    if post is not None:
        sections.append(f"# 原始 post\n\n[{post['id']}] {post['content']}")

    retrieval_query = _build_comment_retrieval_query(post, messages, user_message)
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
    )
    related_memory = evidence_service.format_retrieval_hits(retrieval_hits, current_soul=soul_name)
    if related_memory:
        sections.append(f"# 相关记忆\n\n{related_memory}")

    root_comment = _get_root_comment(post_id, soul_name)
    if root_comment is not None:
        sections.append(f"# {soul_name} 的首条回复\n\n{root_comment['content']}")

    if tool_config_service.is_tool_enabled("todo"):
        pending = todo_service.list_active_todos()
        if pending:
            lines = [todo_service.format_todo_for_context(todo) for todo in pending]
            sections.append("# 待办事项\n\n" + "\n".join(lines))

    context_text = "\n\n---\n\n".join(sections)
    relevant_post_ids = _post_ids_from_hits(retrieval_hits, exclude=post_id)
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
        messages=messages,
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
) -> CommentReplyResult:
    user_message_row = append_comment(post_id, soul_name, "user", user_message)
    comment_context = build_comment_context(post_id, soul_name, user_message, client, model)
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
            fallback_reply=FAILED_COMMENT_REPLY,
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
            fallback_reply=FAILED_COMMENT_REPLY,
        )
        return _failed_result(post_id, soul_name, user_message_row.id, error)

    assistant_message = append_comment(post_id, soul_name, "assistant", reply.strip())
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
        SELECT id, post_id, soul_name, role, content, seq, created_at
        FROM comments
        WHERE id = ?
        """,
        (message_id,),
    )
    if row is None:
        raise ValueError(f"评论消息不存在：{message_id}")
    return _message_from_row(row)


def _failed_result(post_id: str, soul_name: str, user_message_id: int, error: str) -> CommentReplyResult:
    return CommentReplyResult(
        post_id=post_id,
        soul_name=soul_name,
        ok=False,
        reply=FAILED_COMMENT_REPLY,
        user_message_id=user_message_id,
        assistant_message_id=None,
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
    return CommentMessage(
        id=int(row["id"]),
        post_id=row["post_id"],
        soul_name=row["soul_name"],
        role=row["role"],
        content=row["content"],
        seq=int(row["seq"]),
        created_at=float(row["created_at"]),
    )


def _build_comment_retrieval_query(post, messages: list[CommentMessage], user_message: str) -> str:
    parts: list[str] = []
    if post is not None:
        parts.append(str(post["content"]).strip())
    parts.extend(_recent_user_message_contents(messages, limit=RETRIEVAL_USER_MESSAGE_LIMIT))
    if parts:
        return "\n".join(part for part in parts if part)
    return user_message


def _recent_user_message_contents(messages: list[CommentMessage], limit: int) -> list[str]:
    contents = [
        message.content.strip()
        for message in messages
        if message.role == "user" and message.content.strip()
    ]
    return contents[-limit:]


def _post_ids_from_hits(hits: list, *, exclude: str | None = None) -> list[str]:
    post_ids: list[str] = []
    for hit in hits:
        post_id = None
        if getattr(hit, "type", None) == "post":
            post_id = str(getattr(hit, "source_id", ""))
        else:
            metadata = getattr(hit, "metadata", {}) or {}
            post_id = metadata.get("post_id")
        if post_id and post_id != exclude and post_id not in post_ids:
            post_ids.append(post_id)
    return post_ids
