"""Build shared prompt context for public post replies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core import goal_schedule_service, goal_service, logging_service, query_rewriter, reply_context, turn_prep
from core.llm.types import LLMClient
from core.schedule_service import ScheduleService
from core.soul_service import SoulContext, list_enabled_souls

LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEDULE_CONTEXT_LIMIT = 10


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
    context_date = today or datetime.now(LOCAL_TIMEZONE).date()
    schedule_section = _schedule_context_section(context_date)
    if schedule_section:
        sections.append(schedule_section)

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


def _schedule_context_section(context_date: date) -> str:
    result = ScheduleService().list_events(context_date, context_date + timedelta(days=7))
    if not result["connected"]:
        return ""
    events = result["events"][:SCHEDULE_CONTEXT_LIMIT]
    if not events:
        return ""

    progress_by_goal: dict[str, dict[str, Any]] = {}
    progress_now = datetime.combine(context_date, time(hour=12), LOCAL_TIMEZONE)
    lines: list[str] = []
    for event in events:
        try:
            start = datetime.fromisoformat(str(event["start_local"]))
            end = datetime.fromisoformat(str(event["end_local"]))
        except (KeyError, ValueError):
            continue
        if event.get("all_day"):
            when = f"{start.date().isoformat()} 全天"
        else:
            when = f"{start.date().isoformat()} {start:%H:%M}–{end:%H:%M}"
        goal_details: list[str] = []
        for goal_link in event.get("goal_links") or []:
            goal_id = str(goal_link.get("goal_id") or "")
            goal_title = str(goal_link.get("goal_title") or "").strip()
            if not goal_id or not goal_title:
                continue
            progress = progress_by_goal.get(goal_id)
            if progress is None:
                try:
                    progress = goal_schedule_service.weekly_progress(goal_id, now=progress_now)
                except goal_schedule_service.GoalNotFoundError:
                    continue
                progress_by_goal[goal_id] = progress
            expectation = progress.get("expectation")
            if expectation and progress.get("text"):
                goal_details.append(
                    f"目标：{goal_title}（{expectation['label']}，本周 {progress['text']}）"
                )
            else:
                goal_details.append(f"目标：{goal_title}")
        goals = f"；{'；'.join(goal_details)}" if goal_details else ""
        lines.append(f"- {when} {event.get('subject') or '（无标题）'}{goals}")

    if not lines:
        return ""
    return "# 近期日程\n\n[今天至未来 7 天]\n" + "\n".join(lines)
