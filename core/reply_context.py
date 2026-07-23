"""Shared helpers for assembling reply context."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from core import logging_service, memory_read, turn_prep, web_search_gate, web_search_service
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


def prepare_turn_with_prefetch(
    client: LLMClient | None,
    model: str | None,
    *,
    user_message: str,
    channel: str,
    recent_turns: list[dict] | None = None,
    context_hint: str = "",
    excluded_sources: set[tuple[str, str]] | None = None,
    trace_context: dict | None = None,
) -> tuple[turn_prep.TurnPrep, memory_read.PrefetchedRecall | None]:
    """Run the merged gate+rewrite LLM call and the query-dependent vector recall
    concurrently, returning both.

    Both depend only on the raw ``user_message`` (the rewrite has not happened yet
    and the recall's raw-query channels never needed it), so overlapping them hides
    the recall's embedding+ANN round trips behind the LLM call. The caller then
    hands the prefetch to memory_read.memory_section_with_citations, which reuses it
    when the rewrite left semantic_query unchanged and discards the raw unit hits
    otherwise. The raw-query evidence hits remain reusable in both cases.

    The prefetch is strictly best-effort: any failure is logged and downgraded to
    ``None`` so the caller falls back to the current serial recall. prepare_turn's
    own result propagates unchanged (it already degrades internally), so this seam
    can only ever save time, never add a failure mode."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        prep_future = executor.submit(
            turn_prep.prepare_turn,
            client,
            model,
            user_message=user_message,
            channel=channel,
            recent_turns=recent_turns,
            context_hint=context_hint,
            trace_context=trace_context,
        )
        prefetch_future = executor.submit(
            _prefetch_recall_safely,
            user_message,
            excluded_sources,
            trace_context,
        )
        prep = prep_future.result()
        prefetched = prefetch_future.result()
    return prep, prefetched


def _prefetch_recall_safely(
    user_message: str,
    excluded_sources: set[tuple[str, str]] | None,
    trace_context: dict | None,
) -> memory_read.PrefetchedRecall | None:
    """prefetch_semantic_recall wrapped so a worker-thread failure never escapes:
    it logs a WARNING and returns None, leaving the caller on the serial path."""
    try:
        return memory_read.prefetch_semantic_recall(
            user_message, excluded_sources=excluded_sources
        )
    except Exception:
        logging_service.log_event(
            "recall_prefetch_failed",
            level="WARNING",
            **(trace_context or {}),
        )
        return None
