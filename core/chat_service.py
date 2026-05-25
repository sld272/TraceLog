"""Private chat service for one-on-one SOUL conversations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import memory
import router
from core import db, retrieval, soul_memory_service, soul_service
from core.soul_service import SoulContext

if TYPE_CHECKING:
    from openai import OpenAI

CHAT_HISTORY_LIMIT = 20
RELATED_POST_LIMIT = 3


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
    relevant_post_ids: list[str]


@dataclass(frozen=True)
class ChatReplyResult:
    thread_id: int
    soul_name: str
    ok: bool
    reply: str
    todos_to_upsert: list[dict]
    todos_to_delete: list[dict]
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
        thread_id = int(cursor.lastrowid)
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
    """Build prompt context for a one-on-one SOUL chat reply."""
    thread = get_thread(thread_id)
    soul = _load_soul_context(thread.soul_name)
    sections: list[str] = []

    profile = memory.read_profile().strip()
    if profile:
        sections.append(f"# 用户档案\n\n{profile}")

    relevant_post_ids = retrieval.hybrid_search(user_message, k=RELATED_POST_LIMIT)
    relevant_posts = _read_posts_by_ids(relevant_post_ids)
    if relevant_posts:
        sections.append(f"# 相关帖子\n\n{relevant_posts}")

    related_comments = _read_soul_comments(thread.soul_name, relevant_post_ids)
    if related_comments:
        sections.append(f"# 该 SOUL 对相关帖子的历史评论\n\n{related_comments}")

    pending = [todo for todo in memory.load_todos() if todo.get("status") != "已完成"]
    if pending:
        sections.append("# 待办事项\n\n" + "\n".join(_format_todo(todo) for todo in pending))

    messages = list_thread_messages(thread_id, limit=CHAT_HISTORY_LIMIT)
    if messages:
        sections.append("# 当前私聊线程\n\n" + "\n".join(_format_message(m) for m in messages))

    return ChatContext(
        thread=thread,
        soul=soul,
        context="\n\n---\n\n".join(sections),
        relevant_post_ids=relevant_post_ids,
    )


def call_chat_reply(
    thread_id: int,
    user_message: str,
    client: "OpenAI",
    model: str,
) -> ChatReplyResult:
    """Append user input, call one SOUL, and persist the assistant reply."""
    user_message_row = append_user_message(thread_id, user_message)
    chat_context = build_chat_context(thread_id, user_message)
    data = router.call_soul_chat_reply(user_message, client, model, chat_context, chat_context.soul)
    if data is None:
        return _failed_result(chat_context.thread, user_message_row.id, "LLM call failed or returned invalid JSON")

    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return _failed_result(chat_context.thread, user_message_row.id, "LLM response missing non-empty reply")

    assistant_message = _append_message(thread_id, "assistant", reply.strip())
    return ChatReplyResult(
        thread_id=thread_id,
        soul_name=chat_context.thread.soul_name,
        ok=True,
        reply=reply.strip(),
        todos_to_upsert=list(data.get("todos_to_upsert", [])),
        todos_to_delete=list(data.get("todos_to_delete", [])),
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
        message_id = int(cursor.lastrowid)
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


def _read_posts_by_ids(post_ids: list[str]) -> str:
    parts = []
    for post_id in post_ids:
        row = db.query_one(
            "SELECT id, ts, content FROM posts WHERE id = ?",
            (post_id,),
        )
        if row is not None:
            parts.append(memory.format_post(row).strip())
    return "\n\n---\n\n".join(parts)


def _read_soul_comments(soul_name: str, post_ids: list[str]) -> str:
    lines = []
    for post_id in post_ids:
        row = db.query_one(
            """
            SELECT content
            FROM comments
            WHERE post_id = ? AND soul_name = ?
            """,
            (post_id, soul_name),
        )
        if row is not None:
            lines.append(f"- {post_id}: {row['content']}")
    return "\n".join(lines)


def _format_todo(todo: dict) -> str:
    date_str = todo.get("date") or "待定"
    start = todo.get("start_time")
    end = todo.get("end_time")
    if start and end:
        time_str = f" {start}~{end}"
    elif start:
        time_str = f" {start}"
    else:
        time_str = ""
    return f"- [{todo.get('id', '?')}] {todo['task']}（{date_str}{time_str}）"


def _format_message(message: ChatMessage) -> str:
    role = "用户" if message.role == "user" else "SOUL"
    return f"{role}: {message.content}"


def _failed_result(thread: ChatThread, user_message_id: int, error: str) -> ChatReplyResult:
    return ChatReplyResult(
        thread_id=thread.id,
        soul_name=thread.soul_name,
        ok=False,
        reply=FAILED_CHAT_REPLY,
        todos_to_upsert=[],
        todos_to_delete=[],
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
