"""Shared helpers for assembling reply context."""

from __future__ import annotations

from core import logging_service, query_rewriter, retrieval
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


def hybrid_search_with_rewrite(
    retrieval_query: str,
    rewritten_query: query_rewriter.RewrittenQuery,
    *,
    k: int,
    trace_context: dict | None = None,
) -> list[str]:
    if not rewritten_query.used_rewrite:
        return retrieval.hybrid_search(retrieval_query, k=k, trace_context=trace_context)
    return retrieval.hybrid_search(
        retrieval_query,
        k=k,
        semantic_query=rewritten_query.semantic_query,
        fts_keywords=rewritten_query.keywords,
        trace_context=trace_context,
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
