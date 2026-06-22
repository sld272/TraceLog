"""Build shared prompt context for public post replies."""

from __future__ import annotations

from dataclasses import dataclass

from core import db, goal_service, logging_service, memory_read, memory_view_service, record_service, reply_context, todo_service, tool_config_service
from core.llm.types import LLMClient
from core.soul_service import SoulContext, list_enabled_souls


@dataclass(frozen=True)
class BuiltContext:
    shared_context: str
    enabled_souls: list[SoulContext]
    relevant_post_ids: list[str]


def build_context(
    relevant_post_ids: list[str] | None = None,
    query: str | None = None,
    fts_keywords: list[str] | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    trace_context: dict | None = None,
) -> BuiltContext:
    """Build shared user/profile/history/todo context plus enabled SOULs."""
    enabled_souls = list_enabled_souls()
    sections: list[str] = []

    # Parity with chat/comment replies: pull the always-on portrait/state PLUS
    # query-relevant memory units (retrieve_units), not just the static portrait
    # blob. Without this, non-core beliefs (in_portrait=0) that are highly
    # relevant to the post never reach the reply.
    if query:
        memory_section = memory_read.memory_section_for("public_post", None, query)
        if memory_section:
            sections.append(f"# 记忆\n\n{memory_section}")
    else:
        portrait = memory_view_service.read_portrait_body(
            "global", "public", memory_view_service.VIEW_USER_PORTRAIT
        )
        if portrait:
            sections.append(f"# 用户档案\n\n{portrait}")

    sections.extend(goal_service.prompt_sections())

    effective_relevant_ids: list[str] = []
    if relevant_post_ids:
        relevant_posts, effective_relevant_ids = _read_posts_by_ids(_dedupe_relevant_ids(relevant_post_ids))
        if relevant_posts:
            sections.append(f"# 当前用户的历史相关帖子\n\n{relevant_posts}")

    if tool_config_service.is_tool_enabled("todo"):
        pending = todo_service.list_active_todos()
        if pending:
            lines = [todo_service.format_todo_for_context(todo) for todo in pending]
            sections.append("# 待办事项\n\n" + "\n".join(lines))

    if query and enabled_souls:
        web_section = reply_context.build_web_search_section(
            client,
            model,
            query,
            channel="public_post",
            context_hint="\n\n---\n\n".join(sections),
            trace_context=trace_context,
        )
        if web_section:
            sections.append(web_section)

    shared_context = "\n\n---\n\n".join(sections)
    logging_service.log_event(
        "context_assembly_result",
        **(trace_context or {}),
        context_type="public_post",
        sections=reply_context.section_summaries(sections),
        raw_related_posts_present=bool(effective_relevant_ids),
        relevant_post_ids=effective_relevant_ids,
        shared_context_length=len(shared_context),
    )
    return BuiltContext(
        shared_context=shared_context,
        enabled_souls=enabled_souls,
        relevant_post_ids=effective_relevant_ids,
    )


def _dedupe_relevant_ids(post_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for post_id in post_ids:
        if post_id in seen:
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
