"""Shared helpers for rendering post evidence in prompt contexts."""

from __future__ import annotations

from core import db, record_service


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
            WHERE post_id = ? AND soul_name = ?
            """,
            (post_id, soul_name),
        )
        if row is not None:
            lines.append(f"- {post_id}: {row['content']}")
    return "\n".join(lines)
