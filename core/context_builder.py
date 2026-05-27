"""Build shared prompt context for post replies."""

from __future__ import annotations

from dataclasses import dataclass

from core import db, memory_retrieval, profile_service, record_service, todo_service, tool_config_service
from core.soul_service import SoulContext, list_enabled_souls

CONTEXT_POST_COUNT = 3


@dataclass(frozen=True)
class BuiltContext:
    shared_context: str
    enabled_souls: list[SoulContext]
    recent_post_ids: set[str]
    relevant_post_ids: list[str]


def build_context(relevant_post_ids: list[str] | None = None, query: str | None = None) -> BuiltContext:
    """Build shared user/profile/history/todo context plus enabled SOULs."""
    enabled_souls = list_enabled_souls()
    recent_ids = _recent_post_ids()
    sections: list[str] = []

    profile = profile_service.read_profile().strip()
    if profile and profile != profile_service.DEFAULT_USER_MD.strip():
        sections.append(profile)

    recent_posts = record_service.read_recent_posts()
    if recent_posts:
        sections.append(f"# 近期帖子\n\n{recent_posts}")

    effective_relevant_ids: list[str] = []
    if relevant_post_ids:
        candidate_ids = _dedupe_relevant_ids(relevant_post_ids, recent_ids)
        relevant_posts, effective_relevant_ids = _read_posts_by_ids(candidate_ids)
        if relevant_posts:
            sections.append(f"# 相关帖子\n\n{relevant_posts}")

    related_memory = memory_retrieval.search_public_post_memory(query or "", effective_relevant_ids)
    if related_memory:
        sections.append(related_memory)

    if tool_config_service.is_tool_enabled("todo"):
        pending = todo_service.list_active_todos()
        if pending:
            lines = [todo_service.format_todo_for_context(todo) for todo in pending]
            sections.append("# 待办事项\n\n" + "\n".join(lines))

    return BuiltContext(
        shared_context="\n\n---\n\n".join(sections),
        enabled_souls=enabled_souls,
        recent_post_ids=recent_ids,
        relevant_post_ids=effective_relevant_ids,
    )


def _recent_post_ids(count: int = CONTEXT_POST_COUNT) -> set[str]:
    rows = db.query_all(
        """
        SELECT id
        FROM posts
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (count,),
    )
    return {row["id"] for row in rows}


def _dedupe_relevant_ids(post_ids: list[str], excluded_ids: set[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for post_id in post_ids:
        if post_id in excluded_ids or post_id in seen:
            continue
        seen.add(post_id)
        deduped.append(post_id)
    return deduped


def _read_posts_by_ids(post_ids: list[str]) -> tuple[str, list[str]]:
    parts = []
    found_ids: list[str] = []
    for post_id in post_ids:
        row = db.query_one(
            "SELECT id, ts, content FROM posts WHERE id = ?",
            (post_id,),
        )
        if row is not None:
            found_ids.append(post_id)
            parts.append(record_service.format_post(row).strip())
    return "\n\n---\n\n".join(parts), found_ids


