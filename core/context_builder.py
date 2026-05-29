"""Build shared prompt context for post replies."""

from __future__ import annotations

from dataclasses import dataclass

from core import db, logging_service, memory_retrieval, profile_service, record_service, todo_service, tool_config_service
from core.soul_service import SoulContext, list_enabled_souls

CONTEXT_POST_COUNT = 3


@dataclass(frozen=True)
class BuiltContext:
    shared_context: str
    enabled_souls: list[SoulContext]
    recent_post_ids: set[str]
    relevant_post_ids: list[str]
    soul_memory_context_by_name: dict[str, str]


def build_context(
    relevant_post_ids: list[str] | None = None,
    query: str | None = None,
    fts_keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> BuiltContext:
    """Build shared user/profile/history/todo context plus enabled SOULs."""
    enabled_souls = list_enabled_souls()
    recent_ids = _recent_post_ids()
    sections: list[str] = []

    profile = profile_service.read_profile().strip()
    if profile and profile != profile_service.DEFAULT_USER_MD.strip():
        sections.append(profile)

    effective_relevant_ids: list[str] = []
    relevant_posts = ""
    if relevant_post_ids:
        candidate_ids = _dedupe_relevant_ids(relevant_post_ids, recent_ids)
        relevant_posts, effective_relevant_ids = _read_posts_by_ids(candidate_ids)

    related_memory = memory_retrieval.search_public_post_memory(
        query or "",
        effective_relevant_ids,
        fts_keywords=fts_keywords,
        trace_context=trace_context,
    )
    if related_memory:
        sections.append(related_memory)

    soul_memory_context_by_name = {
        soul.name: memory_context
        for soul in enabled_souls
        if (
            memory_context := memory_retrieval.search_soul_post_memory(
                query or "",
                soul.name,
                effective_relevant_ids,
                fts_keywords=fts_keywords,
                trace_context={**(trace_context or {}), "channel": "public_post_soul", "soul_name": soul.name},
            )
        )
    }

    recent_posts = record_service.read_recent_posts()
    if recent_posts:
        sections.append(f"# 近期帖子\n\n{recent_posts}")

    if not related_memory and relevant_posts:
        sections.append(f"# 相关帖子\n\n{relevant_posts}")

    if tool_config_service.is_tool_enabled("todo"):
        pending = todo_service.list_active_todos()
        if pending:
            lines = [todo_service.format_todo_for_context(todo) for todo in pending]
            sections.append("# 待办事项\n\n" + "\n".join(lines))

    shared_context = "\n\n---\n\n".join(sections)
    logging_service.log_event(
        "context_assembly_result",
        **(trace_context or {}),
        context_type="public_post",
        sections=_section_summaries(sections),
        memory_ids=_memory_ids_from_context(related_memory),
        related_memory_present=bool(related_memory),
        soul_memory_present_by_name={name: bool(context) for name, context in soul_memory_context_by_name.items()},
        soul_memory_ids_by_name={
            name: _memory_ids_from_context(context)
            for name, context in soul_memory_context_by_name.items()
        },
        raw_related_post_fallback_used=bool(not related_memory and relevant_posts),
        recent_post_ids=sorted(recent_ids),
        relevant_post_ids=effective_relevant_ids,
        shared_context_length=len(shared_context),
    )
    return BuiltContext(
        shared_context=shared_context,
        enabled_souls=enabled_souls,
        recent_post_ids=recent_ids,
        relevant_post_ids=effective_relevant_ids,
        soul_memory_context_by_name=soul_memory_context_by_name,
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


def _section_summaries(sections: list[str]) -> list[dict]:
    summaries = []
    for section in sections:
        first_line = section.splitlines()[0] if section.splitlines() else ""
        summaries.append(
            {
                "title": first_line[:80],
                "length": len(section),
            }
        )
    return summaries


def _memory_ids_from_context(value: str) -> list[int]:
    ids: list[int] = []
    for line in value.splitlines():
        if not line.startswith("- ["):
            continue
        end = line.find("]")
        if end <= 3:
            continue
        raw_id = line[3:end]
        if raw_id.isdigit():
            ids.append(int(raw_id))
    return ids
