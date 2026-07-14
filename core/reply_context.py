"""Shared helpers for assembling reply context."""

from __future__ import annotations

from core import logging_service, web_search_gate, web_search_service
from core.llm.types import LLMClient


def section_summaries(sections: list[str]) -> list[dict]:
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


def build_web_search_section(
    client: LLMClient | None,
    model: str | None,
    user_message: str,
    *,
    channel: str,
    context_hint: str = "",
    trace_context: dict | None = None,
) -> str:
    """Decide + execute in one shot. Retained for standalone callers (CLI/tests);
    reply paths now split this into turn_prep.prepare_turn + run_web_search_section
    so the gate shares one LLM call with the query rewrite."""
    log_context = {"channel": channel, **(trace_context or {})}
    settings = web_search_service.effective_config()
    if not settings.enabled:
        logging_service.log_event(
            "web_search_skipped",
            **log_context,
            reason="disabled",
            query_count=0,
        )
        return ""
    decision = web_search_gate.decide(
        client,
        model,
        user_message,
        channel=channel,
        context_hint=context_hint,
        trace_context=log_context,
    )
    return run_web_search_section(decision, channel=channel, trace_context=trace_context)


def run_web_search_section(
    decision: web_search_gate.WebSearchDecision,
    *,
    channel: str,
    trace_context: dict | None = None,
) -> str:
    """Execute the search for an already-made decision, format the results into a
    context section, and log the outcome. Fed by a gate/turn-prep decision made
    upstream, so no LLM call happens here."""
    log_context = {"channel": channel, **(trace_context or {})}
    if not decision.should_search:
        logging_service.log_event(
            "web_search_skipped",
            **log_context,
            reason=decision.reason or "gate_decision",
            query_count=0,
        )
        return ""
    settings = web_search_service.effective_config()
    run = web_search_service.search(
        decision.queries,
        config=settings,
        trace_context=log_context,
    )
    section = web_search_service.format_results_for_context(run)
    if section:
        logging_service.log_event(
            "web_search_context_injected",
            **log_context,
            provider=run.provider,
            query_count=len(run.queries),
            result_count=len(run.results),
            context_length=len(section),
        )
    return section
