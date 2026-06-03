"""Shared helpers for rendering post evidence in prompt contexts."""

from __future__ import annotations

from core import attachment_service, db, record_service, vision_service


def read_posts_by_ids(post_ids: list[str]) -> str:
    parts = []
    for post_id in post_ids:
        row = db.query_one(
            "SELECT id, ts, content FROM posts WHERE id = ?",
            (post_id,),
        )
        if row is not None:
            parts.append(record_service.format_post(row).strip())
    return "\n\n---\n\n".join(parts)


def read_soul_comments(soul_name: str, post_ids: list[str]) -> str:
    lines = []
    for post_id in post_ids:
        row = db.query_one(
            """
            SELECT content
            FROM comments
            WHERE post_id = ? AND soul_name = ? AND seq = 0
            """,
            (post_id, soul_name),
        )
        if row is not None:
            lines.append(f"- {post_id}: {row['content']}")
    return "\n".join(lines)


def format_retrieval_hits(hits: list, *, current_soul: str | None = None) -> str:
    """Expand mixed retrieval hits into prompt-ready evidence blocks."""
    del current_soul
    parts: list[str] = []
    seen_posts: set[str] = set()
    seen_comments: set[tuple[str, str]] = set()
    seen_chats: set[int] = set()
    for hit in hits:
        hit_type = getattr(hit, "type", None)
        metadata = getattr(hit, "metadata", {}) or {}
        if hit_type in {"post", "post_vision"}:
            post_id = str(metadata.get("post_id") or getattr(hit, "source_id", ""))
            if post_id and post_id not in seen_posts:
                seen_posts.add(post_id)
                expanded = expand_post(post_id)
                if expanded:
                    parts.append(expanded)
        elif hit_type == "comment":
            post_id = str(metadata.get("post_id") or "")
            soul_name = str(metadata.get("soul_name") or "")
            key = (post_id, soul_name)
            if post_id and soul_name and key not in seen_comments:
                seen_comments.add(key)
                expanded = expand_comment_conversation(post_id, soul_name)
                if expanded:
                    parts.append(expanded)
        elif hit_type == "chat":
            try:
                thread_id = int(metadata.get("thread_id"))
                message_id = int(metadata.get("message_id") or getattr(hit, "source_id", 0))
            except (TypeError, ValueError):
                continue
            if thread_id not in seen_chats:
                seen_chats.add(thread_id)
                expanded = expand_chat_window(thread_id, message_id)
                if expanded:
                    parts.append(expanded)
    return "\n\n".join(parts)


def expand_post(post_id: str) -> str:
    row = db.query_one("SELECT id, ts, content FROM posts WHERE id = ?", (post_id,))
    if row is None:
        return ""
    return f"## 用户公开记录 · {row['ts']} · post {row['id']}\n{_post_content_for_evidence(row)}"


def expand_comment_conversation(post_id: str, soul_name: str, limit: int = 30) -> str:
    post = db.query_one("SELECT id, content FROM posts WHERE id = ?", (post_id,))
    rows = db.query_all(
        """
        SELECT role, content, seq
        FROM comments
        WHERE post_id = ? AND soul_name = ?
        ORDER BY seq ASC
        LIMIT ?
        """,
        (post_id, soul_name, limit),
    )
    if not rows:
        return ""
    title = f"## 公开评论对话 · post {post_id} · {soul_name}"
    if post is not None:
        title += f"\n[关于 post] {_post_content_for_evidence(post)}"
    lines = [title]
    for row in rows:
        label = _comment_label(row["role"], int(row["seq"]), soul_name)
        lines.append(f"[{label}] {row['content']}")
    return "\n".join(lines)


def expand_chat_window(thread_id: int, around_message_id: int, limit: int = 12) -> str:
    thread = db.query_one("SELECT id, soul_name FROM chat_threads WHERE id = ?", (thread_id,))
    if thread is None:
        return ""
    anchor = db.query_one("SELECT created_at FROM chat_messages WHERE id = ?", (around_message_id,))
    anchor_ts = anchor["created_at"] if anchor is not None else None
    if anchor_ts is None:
        rows = db.query_all(
            """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE thread_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (thread_id, limit),
        )
        rows = list(reversed(rows))
    else:
        rows = db.query_all(
            """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE thread_id = ?
            ORDER BY ABS(created_at - ?), id ASC
            LIMIT ?
            """,
            (thread_id, anchor_ts, limit),
        )
        rows = sorted(rows, key=lambda row: row["created_at"])
    if not rows:
        return ""
    lines = [f"## 私聊片段 · {thread['soul_name']} · thread {thread_id}"]
    for row in rows:
        speaker = "用户" if row["role"] == "user" else thread["soul_name"]
        lines.append(f"[{speaker} · 私聊] {row['content']}")
    return "\n".join(lines)


def _comment_label(role: str, seq: int, soul_name: str) -> str:
    if role == "user":
        return "用户 · 追问"
    if seq == 0:
        return f"{soul_name} · 首评"
    return f"{soul_name} · 回复"


def _post_content_for_evidence(row) -> str:
    content = str(row["content"] or "")
    vision_context = vision_service.cached_context_for_post(str(row["id"]))
    if vision_context:
        return f"{content}\n\n{vision_context}" if content.strip() else vision_context
    attachment_count = len(attachment_service.list_post_attachments(str(row["id"])))
    return attachment_service.content_for_llm(content, attachment_count)
