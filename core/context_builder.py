"""Build shared prompt context for public post replies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core import goal_service, logging_service, query_rewriter, reply_context, schedule_context, turn_prep
from core.llm.types import LLMClient
from core.soul_service import SoulContext, list_enabled_souls


@dataclass(frozen=True)
class BuiltContext:
    shared_context: str
    enabled_souls: list[SoulContext]
    rewritten: query_rewriter.RewrittenQuery | None = None


def build_context(
    query: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    trace_context: dict | None = None,
    today: date | None = None,
) -> BuiltContext:
    """Build the soul-agnostic shared context (goals/web) plus enabled SOULs.

    The user portrait and the rest of memory-v2 (# 记忆, baseline + state + relevant
    + freshness) are appended per-soul downstream (reply_service._call_one_soul) so
    each persona gets its own scope-filtered memory — matching the comment/chat
    reply paths, which also carry the portrait only through # 记忆."""
    enabled_souls = list_enabled_souls()
    sections: list[str] = []

    sections.extend(goal_service.prompt_sections())
    recent_schedule = schedule_context.build_recent_schedule_context(today)
    if recent_schedule.section:
        sections.append(recent_schedule.section)

    rewritten: query_rewriter.RewrittenQuery | None = None
    if query and enabled_souls:
        # Merge the web-search gate and the query rewrite into one LLM call, then
        # execute the search decision. The rewrite is carried on BuiltContext so the
        # downstream fanout reuses it instead of issuing its own rewrite call.
        prep = turn_prep.prepare_turn(
            client,
            model,
            user_message=query,
            channel="public_post",
            context_hint="\n\n---\n\n".join(sections),
            trace_context=trace_context,
        )
        mentioned_schedule = schedule_context.build_mentioned_schedule_section(
            prep.rewritten.keywords,
            exclude_event_ids=recent_schedule.event_ids,
            context_date=today,
        )
        if mentioned_schedule:
            sections.append(mentioned_schedule)
        web_section = reply_context.run_web_search_section(
            prep.search_decision,
            channel="public_post",
            trace_context=trace_context,
        )
        if web_section:
            sections.append(web_section)
        # Only hand a rewrite downstream when a real LLM produced it. The CLI builds
        # context without a client (its client lives in fanout), so leave the rewrite
        # to fanout there rather than shipping a no-op fallback.
        if client and model:
            rewritten = prep.rewritten

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
        rewritten=rewritten,
    )
