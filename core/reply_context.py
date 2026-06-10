"""Shared helpers for assembling reply context."""

from __future__ import annotations

from core import logging_service, query_rewriter, retrieval, web_search_gate, web_search_service
from core.llm.types import LLMClient


def rewrite_for_retrieval(
    client: LLMClient | None,
    model: str | None,
    retrieval_query: str,
    channel: str,
    **trace_context,
) -> query_rewriter.RewrittenQuery:
    if client is None or model is None:
        return query_rewriter.RewrittenQuery(
            raw_query=retrieval_query,
            semantic_query=retrieval_query,
            keywords=[],
            used_rewrite=False,
        )
    rewritten = query_rewriter.rewrite_query(
        client,
        model,
        retrieval_query,
        channel,
        trace_context={"channel": channel, **trace_context},
    )
    logging_service.log_event(
        "query_rewrite_result",
        **trace_context,
        channel=channel,
        raw_query=rewritten.raw_query,
        semantic_query=rewritten.semantic_query,
        keywords=rewritten.keywords,
        used_rewrite=rewritten.used_rewrite,
        keyword_count=len(rewritten.keywords),
        semantic_query_length=len(rewritten.semantic_query),
        raw_query_length=len(rewritten.raw_query),
        rewrite_skipped_by_gate=rewritten.rewrite_skipped_by_gate,
    )
    return rewritten


def hybrid_search_documents_with_rewrite(
    retrieval_query: str,
    rewritten_query: query_rewriter.RewrittenQuery,
    *,
    k: int,
    channel: str,
    soul_name: str | None = None,
    trace_context: dict | None = None,
    exclusion: retrieval.RetrievalExclusion | None = None,
) -> list[retrieval.RetrievalDocHit]:
    filter_dict = retrieval.build_retrieval_filter(channel, soul_name)
    if not rewritten_query.used_rewrite:
        return retrieval.hybrid_search_documents(
            retrieval_query,
            k=k,
            trace_context=trace_context,
            filter_dict=filter_dict,
            exclusion=exclusion,
        )
    return retrieval.hybrid_search_documents(
        retrieval_query,
        k=k,
        semantic_query=rewritten_query.semantic_query,
        fts_keywords=rewritten_query.keywords,
        trace_context=trace_context,
        filter_dict=filter_dict,
        exclusion=exclusion,
    )


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
    if not decision.should_search:
        logging_service.log_event(
            "web_search_skipped",
            **log_context,
            reason=decision.reason or "gate_decision",
            query_count=0,
        )
        return ""
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
