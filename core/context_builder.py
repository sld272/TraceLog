"""Build shared prompt context for public post replies."""

from __future__ import annotations

from dataclasses import dataclass

from core import goal_service, logging_service, reply_context, todo_service, tool_config_service
from core.llm.types import LLMClient
from core.soul_service import SoulContext, list_enabled_souls


@dataclass(frozen=True)
class BuiltContext:
    shared_context: str
    enabled_souls: list[SoulContext]


def build_context(
    query: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    trace_context: dict | None = None,
) -> BuiltContext:
    """Build the soul-agnostic shared context (goals/todo/web) plus enabled SOULs.

    The user portrait and the rest of memory-v2 (# 记忆, baseline + state + relevant
    + freshness) are appended per-soul downstream (reply_service._call_one_soul) so
    each persona gets its own scope-filtered memory — matching the comment/chat
    reply paths, which also carry the portrait only through # 记忆."""
    enabled_souls = list_enabled_souls()
    sections: list[str] = []

    sections.extend(goal_service.prompt_sections())

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
        shared_context_length=len(shared_context),
    )
    return BuiltContext(
        shared_context=shared_context,
        enabled_souls=enabled_souls,
    )
