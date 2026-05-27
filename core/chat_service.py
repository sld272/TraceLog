"""Private chat service for one-on-one SOUL conversations."""

from __future__ import annotations

from dataclasses import dataclass
from core import db, evidence_service, logging_service, memory_retrieval, profile_service, retrieval, soul_memory_service, soul_service, todo_service, tool_config_service
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext

CHAT_HISTORY_LIMIT = 20
RELATED_POST_LIMIT = 3
RETRIEVAL_USER_MESSAGE_LIMIT = 3


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


@dataclass(frozen=True)
class ChatContext:
    thread: ChatThread
    soul: SoulContext
    context: str
    messages: list[ChatMessage]
    retrieval_query: str
    relevant_post_ids: list[str]


@dataclass(frozen=True)
class ChatReplyResult:
    thread_id: int
    soul_name: str
    ok: bool
    reply: str
    user_message_id: int
    assistant_message_id: int | None
    error: str | None


FAILED_CHAT_REPLY = "这个 SOUL 暂时没有回复成功，稍后可以重试。"


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


def list_thread_messages(thread_id: int, limit: int = 30) -> list[ChatMessage]:
    """List recent thread messages in chronological order."""
    get_thread(thread_id)
    rows = db.query_all(
        """
        SELECT id, thread_id, role, content, created_at
        FROM chat_messages
        WHERE thread_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (thread_id, limit),
    )
    return [_message_from_row(row) for row in reversed(rows)]


def append_user_message(thread_id: int, content: str) -> ChatMessage:
    """Append a user message to a writable chat thread."""
    thread = get_thread(thread_id)
    _assert_soul_writable(thread.soul_name)
    return _append_message(thread_id, "user", content)


def build_chat_context(thread_id: int, user_message: str) -> ChatContext:
    """Build prompt context after the current user message has been appended."""
    thread = get_thread(thread_id)
    soul = _load_soul_context(thread.soul_name)
    messages = list_thread_messages(thread_id, limit=CHAT_HISTORY_LIMIT)
    retrieval_query = _build_retrieval_query(user_message, messages)
    sections: list[str] = []

    profile = profile_service.read_profile().strip()
    if profile:
        sections.append(f"# 用户档案\n\n{profile}")

    relevant_post_ids = retrieval.hybrid_search(retrieval_query, k=RELATED_POST_LIMIT)
    relevant_posts = evidence_service.read_posts_by_ids(relevant_post_ids)
    if relevant_posts:
        sections.append(f"# 相关帖子\n\n{relevant_posts}")

    related_memory = memory_retrieval.search_chat_memory(
        retrieval_query,
        thread.soul_name,
        relevant_post_ids,
    )
    if related_memory:
        sections.append(related_memory)

    related_comments = evidence_service.read_soul_comments(thread.soul_name, relevant_post_ids)
    if related_comments:
        sections.append(f"# 该 SOUL 对相关帖子的历史评论\n\n{related_comments}")

    if tool_config_service.is_tool_enabled("todo"):
        pending = todo_service.list_active_todos()
        if pending:
            lines = [todo_service.format_todo_for_context(todo) for todo in pending]
            sections.append("# 待办事项\n\n" + "\n".join(lines))

    return ChatContext(
        thread=thread,
        soul=soul,
        context="\n\n---\n\n".join(sections),
        messages=messages,
        retrieval_query=retrieval_query,
        relevant_post_ids=relevant_post_ids,
    )


def call_chat_reply(
    thread_id: int,
    user_message: str,
    client: LLMClient,
    model: str,
) -> ChatReplyResult:
    """Append user input, call one SOUL, and persist the assistant reply."""
    user_message_row = append_user_message(thread_id, user_message)
    chat_context = build_chat_context(thread_id, user_message)
    data = reply_router.call_soul_chat_reply(
        client,
        model,
        chat_context,
        chat_context.soul,
        trace_context={
            "thread_id": thread_id,
            "soul_name": chat_context.thread.soul_name,
            "user_message_id": user_message_row.id,
            "relevant_post_ids": chat_context.relevant_post_ids,
        },
    )
    if data is None:
        error = "LLM call failed or returned invalid JSON"
        logging_service.log_event(
            "reply_failed",
            level="WARNING",
            channel="chat",
            thread_id=thread_id,
            soul_name=chat_context.thread.soul_name,
            user_message_id=user_message_row.id,
            error=error,
            fallback_reply=FAILED_CHAT_REPLY,
        )
        return _failed_result(chat_context.thread, user_message_row.id, error)

    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        error = "LLM response missing non-empty reply"
        logging_service.log_event(
            "reply_failed",
            level="WARNING",
            channel="chat",
            thread_id=thread_id,
            soul_name=chat_context.thread.soul_name,
            user_message_id=user_message_row.id,
            error=error,
            fallback_reply=FAILED_CHAT_REPLY,
        )
        return _failed_result(chat_context.thread, user_message_row.id, error)

    assistant_message = _append_message(thread_id, "assistant", reply.strip())
    return ChatReplyResult(
        thread_id=thread_id,
        soul_name=chat_context.thread.soul_name,
        ok=True,
        reply=reply.strip(),
        user_message_id=user_message_row.id,
        assistant_message_id=assistant_message.id,
        error=None,
    )


def _append_message(thread_id: int, role: str, content: str) -> ChatMessage:
    body = content.strip()
    if not body:
        raise ValueError("私聊消息不能为空")
    now = db.now_ts()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO chat_messages(thread_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (thread_id, role, body, now),
        )
        message_id = db.require_lastrowid(cursor, "chat message insert")
        conn.execute(
            """
            UPDATE chat_threads
            SET updated_at = ?, last_message_at = ?
            WHERE id = ?
            """,
            (now, now, thread_id),
        )
    return get_message(message_id)


def get_message(message_id: int) -> ChatMessage:
    row = db.query_one(
        """
        SELECT id, thread_id, role, content, created_at
        FROM chat_messages
        WHERE id = ?
        """,
        (message_id,),
    )
    if row is None:
        raise ValueError(f"私聊消息不存在：{message_id}")
    return _message_from_row(row)


def _assert_soul_writable(soul_name: str) -> None:
    record = soul_service.get_soul(soul_name)
    if not record.enabled:
        raise ValueError(f"SOUL 已禁用，旧线程只读：{soul_name}")
    if not record.persona_exists:
        raise ValueError(f"SOUL 人格文件不存在，无法私聊：{soul_name}")


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


def _build_retrieval_query(user_message: str, messages: list[ChatMessage]) -> str:
    """Return the search query for related posts; currently no LLM rewrite is applied."""
    parts = _recent_user_message_contents(messages, limit=RETRIEVAL_USER_MESSAGE_LIMIT)
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


def _failed_result(thread: ChatThread, user_message_id: int, error: str) -> ChatReplyResult:
    return ChatReplyResult(
        thread_id=thread.id,
        soul_name=thread.soul_name,
        ok=False,
        reply=FAILED_CHAT_REPLY,
        user_message_id=user_message_id,
        assistant_message_id=None,
        error=error,
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
    return ChatMessage(
        id=row["id"],
        thread_id=row["thread_id"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
    )
