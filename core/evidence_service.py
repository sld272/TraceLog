"""Shared helpers for rendering post evidence in prompt contexts."""

from __future__ import annotations

from core import attachment_service, db, vision_service

MAX_EVIDENCE_ITEMS = 3
EVIDENCE_SNIPPET_CHARS = 120
DELETED_SNIPPET = "(原始内容已删除)"


def format_retrieval_hits(
    hits: list,
    *,
    current_soul: str | None = None,
    exclude_comment_conversations: set[tuple[str, str]] | None = None,
) -> str:
    """Expand mixed retrieval hits into prompt-ready evidence blocks."""
    del current_soul
    excluded_comment_conversations = exclude_comment_conversations or set()
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
            if key in excluded_comment_conversations:
                continue
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


def build_evidence_summary(hits: list, *, limit: int = MAX_EVIDENCE_ITEMS) -> list[dict]:
    """Return compact evidence items suitable for message metadata snapshots."""
    items: list[dict] = []
    for hit in hits[: max(0, int(limit))]:
        hit_type = str(getattr(hit, "type", "") or "")
        metadata = getattr(hit, "metadata", {}) or {}
        post_id = _post_id_for_hit(hit_type, hit, metadata)
        items.append(
            {
                "doc_id": str(getattr(hit, "doc_id", "") or ""),
                "type": hit_type,
                "source_id": str(getattr(hit, "source_id", "") or ""),
                "post_id": post_id,
                "score": getattr(hit, "score", None),
                "distance": getattr(hit, "distance", None),
                "sources": list(getattr(hit, "sources", []) or []),
                "reasons": list(getattr(hit, "reasons", []) or []),
                "snippet": _snippet_for_hit(hit_type, hit, metadata),
            }
        )
    return items


def expand_post(post_id: str) -> str:
    row = db.query_one("SELECT id, ts, content FROM posts WHERE id = ?", (post_id,))
    if row is None:
        return ""
    return f"## 用户公开记录 · {row['ts']} · post {row['id']}\n{_post_content_for_evidence(row)}"


def evidence_metadata(hits: list) -> dict:
    """Build the versioned evidence metadata object stored on assistant messages."""
    return {"version": 1, "items": build_evidence_summary(hits)}


def post_id_evidence_metadata(post_ids: list[str]) -> dict:
    """Build simplified evidence metadata for public post first replies."""
    hits = []
    for index, post_id in enumerate(_dedupe_strings(post_ids), start=1):
        hits.append(
            {
                "doc_id": f"post-{post_id}",
                "type": "post",
                "source_id": post_id,
                "post_id": post_id,
                "score": None,
                "distance": None,
                "sources": ["context"],
                "reasons": [f"context:rank={index}"],
                "snippet": _truncate_snippet(_read_post_content(post_id)),
            }
        )
    return {"version": 1, "items": hits[:MAX_EVIDENCE_ITEMS]}


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


def _post_id_for_hit(hit_type: str, hit, metadata: dict) -> str | None:
    if hit_type == "post":
        return str(metadata.get("post_id") or getattr(hit, "source_id", "") or "") or None
    if hit_type == "post_vision":
        return str(metadata.get("post_id") or getattr(hit, "source_id", "") or "") or None
    if hit_type == "comment":
        return str(metadata.get("post_id") or "") or None
    return None


def _snippet_for_hit(hit_type: str, hit, metadata: dict) -> str:
    if hit_type == "post":
        return _truncate_snippet(_read_post_content(str(metadata.get("post_id") or getattr(hit, "source_id", ""))))
    if hit_type == "post_vision":
        body = _read_vector_doc_content(str(getattr(hit, "doc_id", "") or ""))
        if not body:
            post_id = str(metadata.get("post_id") or getattr(hit, "source_id", "") or "")
            body = vision_service.cached_context_for_post(post_id) or _read_post_content(post_id)
        return _truncate_snippet(body)
    if hit_type == "comment":
        comment_id = str(metadata.get("comment_id") or getattr(hit, "source_id", "") or "")
        return _truncate_snippet(_read_comment_content(comment_id))
    if hit_type == "chat":
        message_id = str(metadata.get("message_id") or getattr(hit, "source_id", "") or "")
        return _truncate_snippet(_read_chat_message_content(message_id))
    return DELETED_SNIPPET


def _read_post_content(post_id: str) -> str:
    if not post_id:
        return ""
    row = db.query_one("SELECT content FROM posts WHERE id = ?", (post_id,))
    return str(row["content"] or "") if row is not None else ""


def _read_vector_doc_content(doc_id: str) -> str:
    if not doc_id:
        return ""
    row = db.query_one("SELECT content FROM vector_docs WHERE doc_id = ?", (doc_id,))
    return str(row["content"] or "") if row is not None else ""


def _read_comment_content(comment_id: str) -> str:
    try:
        numeric_id = int(comment_id)
    except (TypeError, ValueError):
        return ""
    row = db.query_one("SELECT content FROM comments WHERE id = ?", (numeric_id,))
    return str(row["content"] or "") if row is not None else ""


def _read_chat_message_content(message_id: str) -> str:
    try:
        numeric_id = int(message_id)
    except (TypeError, ValueError):
        return ""
    row = db.query_one("SELECT content FROM chat_messages WHERE id = ?", (numeric_id,))
    return str(row["content"] or "") if row is not None else ""


def _truncate_snippet(content: str) -> str:
    compact = " ".join(str(content or "").split())
    if not compact:
        return DELETED_SNIPPET
    if len(compact) <= EVIDENCE_SNIPPET_CHARS:
        return compact
    return compact[:EVIDENCE_SNIPPET_CHARS].rstrip() + "..."


def _dedupe_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in deduped:
            deduped.append(clean)
    return deduped


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
