"""Orchestrates one turn's pre-reply LLM prep: web-search gate + query rewrite.

Merged into a single LLM call when both are actually needed, with each half keeping
its own independent fallback so a bad, partial, or missing response degrades exactly
like the two original standalone calls did. Search execution stays out of here — the
caller feeds ``search_decision`` to ``reply_context.run_web_search_section``."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from core import logging_service, query_rewriter, web_search_gate, web_search_service
from core.llm import secondary_model, turn_prep_router
from core.llm.types import LLMClient


@dataclass(frozen=True)
class TurnPrep:
    rewritten: query_rewriter.RewrittenQuery
    search_decision: web_search_gate.WebSearchDecision


def prepare_turn(
    client: LLMClient | None,
    model: str | None,
    *,
    user_message: str,
    channel: str,
    recent_turns: list[dict] | None = None,
    context_hint: str = "",
    trace_context: dict | None = None,
) -> TurnPrep:
    """Return this turn's rewrite + search decision, using one merged LLM call on
    the normal path and independent fallbacks on every degradation."""
    started = perf_counter()
    raw = str(user_message or "").strip()
    search_enabled = web_search_service.effective_config().enabled

    # Empty message or slash-command: the rewrite gate already skips these without
    # an LLM call, and per product we skip search too — an empty message has nothing
    # to search, and a slash-command is a control input, not a reply that should
    # trigger a web lookup. Both fall back with no merged call.
    if not query_rewriter.should_rewrite_query(raw, channel):
        rewritten = query_rewriter.rewrite_query(
            client, model, raw, channel, recent_turns=recent_turns, trace_context=trace_context
        )
        reason = "empty_user_message" if not raw else "skipped_command"
        return _finish(rewritten, web_search_gate.default_decision(reason), channel, trace_context, started, merged=False)

    resolved_client, resolved_model = secondary_model.resolve(client, model)
    if resolved_client is None or resolved_model is None:
        # No usable model at all: fall back on both halves without any call, matching
        # the old gate's "missing_llm_client" short-circuit and the rewrite's guard.
        rewritten = query_rewriter.RewrittenQuery(
            raw_query=raw, semantic_query=raw, keywords=[], used_rewrite=False
        )
        reason = "disabled" if not search_enabled else "missing_llm_client"
        return _finish(rewritten, web_search_gate.default_decision(reason), channel, trace_context, started, merged=False)

    if not search_enabled:
        # Search is off: no merged call is worthwhile — the gate is guaranteed to say
        # "no search" — so run the rewrite alone (single call) and waste nothing.
        rewritten = query_rewriter.rewrite_query(
            client, model, raw, channel, recent_turns=recent_turns, trace_context=trace_context
        )
        return _finish(rewritten, web_search_gate.default_decision("disabled"), channel, trace_context, started, merged=False)

    # Normal path: one merged call feeds both halves, each with its own fallback.
    data = turn_prep_router.call_turn_prep(
        client,
        model,
        user_message=raw,
        channel=channel,
        recent_turns=recent_turns,
        context_hint=context_hint,
        trace_context=trace_context,
    )
    if data is None:
        # Whole call failed/invalid: both halves fall back independently.
        rewritten = query_rewriter.RewrittenQuery(
            raw_query=raw, semantic_query=raw, keywords=[], used_rewrite=False
        )
        decision = web_search_gate.default_decision("turn_prep_failed")
        web_search_gate.log_decision(decision, channel=channel, trace_context=trace_context, skipped=True)
        return _finish(rewritten, decision, channel, trace_context, started, merged=True)

    # Both halves parsed by their own validation kernel; a half that came back thin
    # (e.g. empty semantic_query, or all-private queries) degrades on its own without
    # dragging the other half down.
    decision = web_search_gate.WebSearchDecision(
        should_search=bool(data["should_search"]),
        queries=list(data["queries"]),
        reason=str(data.get("reason") or ""),
        freshness_required=bool(data.get("freshness_required")),
    )
    web_search_gate.log_decision(decision, channel=channel, trace_context=trace_context, skipped=False)
    rewritten = query_rewriter.rewrite_from_fields(raw, data.get("semantic_query"), data.get("keywords"))
    return _finish(rewritten, decision, channel, trace_context, started, merged=True)


def _finish(
    rewritten: query_rewriter.RewrittenQuery,
    decision: web_search_gate.WebSearchDecision,
    channel: str,
    trace_context: dict | None,
    started: float,
    *,
    merged: bool,
) -> TurnPrep:
    context = {"channel": channel, **(trace_context or {})}
    logging_service.log_event(
        "turn_prep_used",
        **context,
        used_rewrite=rewritten.used_rewrite,
        should_search=decision.should_search,
        merged_call=merged,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )
    return TurnPrep(rewritten=rewritten, search_decision=decision)
