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


def hybrid_search_documents_with_rewrite(
    retrieval_query: str,
    rewritten_query: query_rewriter.RewrittenQuery,
    *,
    k: int,
    channel: str,
    soul_name: str | None = None,
    trace_context: dict | None = None,
) -> list[retrieval.RetrievalDocHit]:
    filter_dict = retrieval.build_retrieval_filter(channel, soul_name)
    if not rewritten_query.used_rewrite:
        hits = retrieval.hybrid_search_documents(
            retrieval_query,
            k=k,
            trace_context=trace_context,
            filter_dict=filter_dict,
        )
        return hits or _legacy_post_hits(retrieval_query, k=k, trace_context=trace_context)
    hits = retrieval.hybrid_search_documents(
        retrieval_query,
        k=k,
        semantic_query=rewritten_query.semantic_query,
        fts_keywords=rewritten_query.keywords,
        trace_context=trace_context,
        filter_dict=filter_dict,
    )
    return hits or _legacy_post_hits(
        retrieval_query,
        k=k,
        semantic_query=rewritten_query.semantic_query,
        fts_keywords=rewritten_query.keywords,
        trace_context=trace_context,
    )


def _legacy_post_hits(
    retrieval_query: str,
    *,
    k: int,
    semantic_query: str | None = None,
    fts_keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> list[retrieval.RetrievalDocHit]:
    post_ids = retrieval.hybrid_search(
        retrieval_query,
        k=k,
        semantic_query=semantic_query,
        fts_keywords=fts_keywords,
        trace_context=trace_context,
    )
    return [
        retrieval.RetrievalDocHit(
            doc_id=f"post-{post_id}",
            type="post",
            source_id=post_id,
            score=1.0,
            rank=index + 1,
            metadata={"type": "post", "post_id": post_id},
            sources=["legacy"],
            reasons=["legacy_post_id"],
        )
        for index, post_id in enumerate(post_ids)
    ]


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
