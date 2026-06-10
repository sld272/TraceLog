"""Hybrid retrieval over SQLite FTS5 and ChromaDB."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import sqlite3
from typing import Any

from core import db
from core import fts_query
from core import logging_service

LIKE_FALLBACK_TRUST = 0.85
MAX_VECTOR_DISTANCE = 0.65


@dataclass(frozen=True)
class RetrievalHit:
    post_id: str
    source: str
    rank: int
    raw_score: float | None = None


@dataclass(frozen=True)
class HybridHit:
    post_id: str
    score: float
    fts_score: float
    vector_score: float
    sources: list[str]
    reasons: list[str]


@dataclass(frozen=True)
class RetrievalDocHit:
    doc_id: str
    type: str
    source_id: str
    score: float
    rank: int
    metadata: dict[str, Any]
    sources: list[str]
    reasons: list[str]
    distance: float | None = None


def fts_search_scored(
    query: str,
    k: int = 20,
    fts_keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> list[RetrievalHit]:
    """Return FTS hits with source, rank position, and raw FTS rank for debug."""
    clean = fts_query.sanitize_fts5(query)
    if not clean and not fts_keywords:
        return []

    keyword_candidates = fts_query.keyword_candidates(fts_keywords or [])
    keyword_match = fts_query.quote_match_candidates(keyword_candidates)
    deterministic_candidates: list[str] = []
    if keyword_match:
        match = keyword_match
        table = "posts_fts_trigram" if any(fts_query.has_cjk(keyword) for keyword in fts_keywords or []) else "posts_fts"
        source = "fts_rewrite"
    elif fts_query.has_cjk(clean):
        if len(clean.replace(" ", "")) < 3:
            hits = _like_search_scored(clean, k)
            _log_fts_query_built(
                query=query,
                clean=clean,
                fts_keywords=fts_keywords or [],
                keyword_candidates=keyword_candidates,
                deterministic_candidates=[],
                match="",
                table="posts",
                source="like_fallback",
                fallback_type="short_cjk_like",
                hit_count=len(hits),
                trace_context=trace_context,
            )
            return hits
        table = "posts_fts_trigram"
        source = "fts_trigram"
    else:
        table = "posts_fts"
        source = "fts"

    if not keyword_match:
        deterministic_candidates = fts_query.match_candidates(clean)
        match = fts_query.quote_match_candidates(deterministic_candidates)
    if not match:
        return []
    sql = f"""
        SELECT posts.id, rank AS raw_rank
        FROM {table}
        JOIN posts ON posts.rowid = {table}.rowid
        WHERE {table} MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    try:
        hits = [
            RetrievalHit(
                post_id=row["id"],
                source=source,
                rank=index + 1,
                raw_score=float(row["raw_rank"]) if row["raw_rank"] is not None else None,
            )
            for index, row in enumerate(db.query_all(sql, (match, k)))
        ]
        _log_fts_query_built(
            query=query,
            clean=clean,
            fts_keywords=fts_keywords or [],
            keyword_candidates=keyword_candidates,
            deterministic_candidates=deterministic_candidates,
            match=match,
            table=table,
            source=source,
            fallback_type=None,
            hit_count=len(hits),
            trace_context=trace_context,
        )
        return hits
    except sqlite3.Error:
        _log_fts_query_built(
            query=query,
            clean=clean,
            fts_keywords=fts_keywords or [],
            keyword_candidates=keyword_candidates,
            deterministic_candidates=deterministic_candidates,
            match=match,
            table=table,
            source=source,
            fallback_type="sqlite_error",
            hit_count=0,
            trace_context=trace_context,
        )
        return []


def vector_search_scored(query: str, k: int = 20) -> list[RetrievalHit]:
    """Return vector hits with rank and Chroma distance when available."""
    try:
        from core import vectorstore

        hits = [
            RetrievalHit(
                post_id=hit.post_id,
                source="vector",
                rank=hit.rank,
                raw_score=hit.distance,
            )
            for hit in vectorstore.query_post_hits(query, n_results=k)
        ]
        return _filter_vector_hits(hits, key=lambda hit: hit.raw_score, trace="posts")
    except Exception:
        return []


def build_retrieval_filter(channel: str, soul_name: str | None = None) -> dict | None:
    """Build a Chroma metadata filter for one reply context."""
    if channel == "public_post":
        return {"$or": [{"type": {"$eq": "post"}}, {"type": {"$eq": "post_vision"}}]}
    if channel in {"chat", "comment", "comment_thread"} and soul_name:
        return {
            "$or": [
                {"type": {"$eq": "post"}},
                {"type": {"$eq": "post_vision"}},
                {"type": {"$eq": "comment"}},
                {
                    "$and": [
                        {"type": {"$eq": "chat"}},
                        {"soul_name": {"$eq": soul_name}},
                    ]
                },
            ]
        }
    return {"type": {"$eq": "post"}}


def hybrid_search_documents(
    query: str,
    k: int = 5,
    candidate_k: int = 20,
    semantic_query: str | None = None,
    fts_keywords: list[str] | None = None,
    trace_context: dict | None = None,
    filter_dict: dict | None = None,
) -> list[RetrievalDocHit]:
    """Return mixed post/comment/chat retrieval hits."""
    candidate_limit = max(k, candidate_k)
    fts_hits = (
        fts_search_scored(query, k=candidate_limit, fts_keywords=fts_keywords, trace_context=trace_context)
        if fts_keywords is not None
        else fts_search_scored(query, k=candidate_limit, trace_context=trace_context)
    )
    vector_query = semantic_query or query
    vector_hits = vector_search_documents_scored(vector_query, k=candidate_limit, filter_dict=filter_dict)
    final_hits = _merge_document_hits(query, fts_hits, vector_hits, k)
    _log_hybrid_doc_retrieval_result(
        query=query,
        semantic_query=semantic_query,
        fts_keywords=fts_keywords,
        fts_hits=fts_hits,
        vector_hits=vector_hits,
        final_hits=final_hits,
        trace_context=trace_context,
    )
    return final_hits


def vector_search_documents_scored(
    query: str,
    k: int = 20,
    filter_dict: dict | None = None,
) -> list:
    try:
        from core import vectorstore

        hits = vectorstore.query_documents(query, n_results=k, where=filter_dict)
        return _filter_vector_hits(hits, key=lambda hit: hit.distance, trace="documents")
    except Exception:
        return []


def _within_vector_distance(distance: float | None) -> bool:
    if distance is None:
        return True
    return distance <= MAX_VECTOR_DISTANCE


def _filter_vector_hits(hits: list, *, key, trace: str) -> list:
    kept = []
    dropped_distances: list[float] = []
    missing_distance_count = 0

    for hit in hits:
        distance = key(hit)
        if distance is None:
            missing_distance_count += 1
            kept.append(hit)
            continue
        if _within_vector_distance(distance):
            kept.append(hit)
        else:
            dropped_distances.append(float(distance))

    if dropped_distances or missing_distance_count:
        logging_service.log_event(
            "vector_hits_filtered",
            target=trace,
            dropped_count=len(dropped_distances),
            kept_count=len(kept),
            missing_distance_count=missing_distance_count,
            max_distance=MAX_VECTOR_DISTANCE,
            dropped_distances=[round(distance, 4) for distance in dropped_distances[:10]],
        )
    return [replace(hit, rank=index + 1) for index, hit in enumerate(kept)]


def hybrid_search(
    query: str,
    k: int = 3,
    semantic_query: str | None = None,
    fts_keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> list[str]:
    """Return top hybrid result ids for prompt context."""
    return [
        hit.post_id
        for hit in hybrid_search_scored(
            query,
            k=k,
            min_score=None,
            semantic_query=semantic_query,
            fts_keywords=fts_keywords,
            trace_context=trace_context,
        )
    ]


def hybrid_search_scored(
    query: str,
    k: int = 3,
    min_score: float | None = None,
    candidate_k: int = 20,
    allow_fallback: bool = True,
    semantic_query: str | None = None,
    fts_keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> list[HybridHit]:
    """Combine FTS5 and ChromaDB with dynamic weights and explainable scores."""
    fts_hits = (
        fts_search_scored(query, k=candidate_k, fts_keywords=fts_keywords)
        if fts_keywords is not None
        else fts_search_scored(query, k=candidate_k)
    )
    vector_query = semantic_query or query
    vector_hits = vector_search_scored(vector_query, k=candidate_k)
    if not fts_hits and not vector_hits:
        _log_hybrid_retrieval_result(
            query=query,
            semantic_query=semantic_query,
            fts_keywords=fts_keywords,
            fts_hits=[],
            vector_hits=[],
            final_hits=[],
            trace_context=trace_context,
        )
        return []

    post_ids = fts_query.ordered_unique([hit.post_id for hit in fts_hits + vector_hits])
    contents = _read_candidate_contents(post_ids)
    fts_weight, vector_weight = _infer_query_weights(query)
    fts_scores = _score_fts_hits(fts_hits)
    vector_scores = _score_vector_hits(vector_hits)
    best_hits = _best_hits_by_post(fts_hits, vector_hits)
    scored: list[tuple[HybridHit, bool, int]] = []

    for post_id in post_ids:
        fts_score = fts_scores.get(post_id, 0.0)
        vector_score = vector_scores.get(post_id, 0.0)
        fts_part = fts_weight * fts_score
        vector_part = vector_weight * vector_score
        base_score = max(fts_part, vector_part) + 0.20 * min(fts_part, vector_part)
        bonus_score, bonus_reasons = _content_bonus(query, contents.get(post_id, ""))
        final_score = min(base_score + bonus_score, 1.0)
        sources, reasons = _build_reasons(post_id, best_hits, bonus_reasons)
        agreement = "agreement" in reasons
        best_rank = min((hit.rank for hit in best_hits.get(post_id, [])), default=candidate_k + 1)
        scored.append(
            (
                HybridHit(
                    post_id=post_id,
                    score=round(final_score, 6),
                    fts_score=round(fts_score, 6),
                    vector_score=round(vector_score, 6),
                    sources=sources,
                    reasons=reasons,
                ),
                agreement,
                best_rank,
            )
        )

    ordered = [
        item[0]
        for item in sorted(
            scored,
            key=lambda item: (item[0].score, item[1], -item[2], item[0].post_id),
            reverse=True,
        )
    ]
    if min_score is None:
        final_hits = ordered[:k]
        _log_hybrid_retrieval_result(
            query=query,
            semantic_query=semantic_query,
            fts_keywords=fts_keywords,
            fts_hits=fts_hits,
            vector_hits=vector_hits,
            final_hits=final_hits,
            trace_context=trace_context,
        )
        return final_hits

    filtered = [hit for hit in ordered if hit.score >= min_score]
    if filtered:
        final_hits = filtered[:k]
        _log_hybrid_retrieval_result(
            query=query,
            semantic_query=semantic_query,
            fts_keywords=fts_keywords,
            fts_hits=fts_hits,
            vector_hits=vector_hits,
            final_hits=final_hits,
            trace_context=trace_context,
        )
        return final_hits
    if allow_fallback and ordered:
        final_hits = ordered[: min(k, 1)]
        _log_hybrid_retrieval_result(
            query=query,
            semantic_query=semantic_query,
            fts_keywords=fts_keywords,
            fts_hits=fts_hits,
            vector_hits=vector_hits,
            final_hits=final_hits,
            trace_context=trace_context,
        )
        return final_hits
    _log_hybrid_retrieval_result(
        query=query,
        semantic_query=semantic_query,
        fts_keywords=fts_keywords,
        fts_hits=fts_hits,
        vector_hits=vector_hits,
        final_hits=[],
        trace_context=trace_context,
    )
    return []


def _like_search_scored(query: str, k: int) -> list[RetrievalHit]:
    pattern = f"%{query.replace(' ', '')}%"
    rows = db.query_all(
        """
        SELECT id
        FROM posts
        WHERE content LIKE ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (pattern, k),
    )
    return [
        RetrievalHit(post_id=row["id"], source="like_fallback", rank=index + 1)
        for index, row in enumerate(rows)
    ]


def _merge_document_hits(query: str, fts_hits: list[RetrievalHit], vector_hits: list, k: int) -> list[RetrievalDocHit]:
    fts_scores = _score_fts_hits(fts_hits)
    vector_scores = _score_vector_doc_hits(vector_hits)
    by_doc: dict[str, RetrievalDocHit] = {}

    for hit in fts_hits:
        doc_id = f"post-{hit.post_id}"
        base_score = fts_scores.get(hit.post_id, 0.0)
        bonus_score, bonus_reasons = _content_bonus(query, _read_post_content(hit.post_id))
        by_doc[doc_id] = RetrievalDocHit(
            doc_id=doc_id,
            type="post",
            source_id=hit.post_id,
            score=round(min(base_score + bonus_score, 1.0), 6),
            rank=hit.rank,
            metadata={"type": "post", "post_id": hit.post_id},
            sources=[hit.source],
            reasons=[f"{hit.source}:rank={hit.rank}", *bonus_reasons],
            distance=None,
        )

    for hit in vector_hits:
        doc_id = str(hit.doc_id)
        doc_type = str(hit.type)
        raw_score = vector_scores.get(doc_id, 0.0) * _type_weight(doc_type)
        existing = by_doc.get(doc_id)
        if existing is not None:
            sources = fts_query.ordered_unique([*existing.sources, "vector"])
            reasons = [*existing.reasons, f"vector:rank={hit.rank}", "agreement"]
            by_doc[doc_id] = RetrievalDocHit(
                doc_id=existing.doc_id,
                type=existing.type,
                source_id=existing.source_id,
                score=round(max(existing.score, raw_score), 6),
                rank=min(existing.rank, int(hit.rank)),
                metadata={**dict(hit.metadata), **existing.metadata},
                sources=sources,
                reasons=reasons,
                distance=hit.distance if hit.distance is not None else existing.distance,
            )
            continue
        by_doc[doc_id] = RetrievalDocHit(
            doc_id=doc_id,
            type=doc_type,
            source_id=str(hit.source_id),
            score=round(raw_score, 6),
            rank=int(hit.rank),
            metadata=dict(hit.metadata),
            sources=["vector"],
            reasons=[f"vector:rank={hit.rank}"],
            distance=hit.distance,
        )

    return sorted(by_doc.values(), key=lambda hit: (hit.score, -hit.rank, hit.doc_id), reverse=True)[:k]


def _score_vector_doc_hits(hits: list) -> dict[str, float]:
    distances = [hit.distance for hit in hits if hit.distance is not None]
    min_distance = min(distances) if distances else None
    max_distance = max(distances) if distances else None
    scores: dict[str, float] = {}
    total = len(hits)
    for hit in hits:
        rank_score = _position_score(int(hit.rank), total)
        if hit.distance is None or min_distance is None or max_distance is None or max_distance == min_distance:
            distance_score = rank_score
        else:
            distance_score = 1.0 - ((hit.distance - min_distance) / (max_distance - min_distance))
        scores[str(hit.doc_id)] = 0.7 * distance_score + 0.3 * rank_score
    return scores


def _type_weight(doc_type: str) -> float:
    if doc_type == "comment":
        return 0.85
    if doc_type == "chat":
        return 0.75
    return 1.0


def _read_post_content(post_id: str) -> str:
    row = db.query_one("SELECT content FROM posts WHERE id = ?", (post_id,))
    return str(row["content"]) if row is not None else ""


def _score_fts_hits(hits: list[RetrievalHit]) -> dict[str, float]:
    scores: dict[str, float] = {}
    total = len(hits)
    for hit in hits:
        score = _position_score(hit.rank, total)
        if hit.source == "like_fallback":
            score *= LIKE_FALLBACK_TRUST
        scores[hit.post_id] = max(scores.get(hit.post_id, 0.0), score)
    return scores


def _score_vector_hits(hits: list[RetrievalHit]) -> dict[str, float]:
    distances = [hit.raw_score for hit in hits if hit.raw_score is not None]
    min_distance = min(distances) if distances else None
    max_distance = max(distances) if distances else None
    scores: dict[str, float] = {}
    total = len(hits)

    for hit in hits:
        rank_score = _position_score(hit.rank, total)
        if (
            hit.raw_score is None
            or min_distance is None
            or max_distance is None
            or max_distance == min_distance
        ):
            distance_score = rank_score
        else:
            distance_score = 1.0 - ((hit.raw_score - min_distance) / (max_distance - min_distance))
        score = 0.7 * distance_score + 0.3 * rank_score
        scores[hit.post_id] = max(scores.get(hit.post_id, 0.0), score)
    return scores


def _position_score(rank: int, total: int) -> float:
    if total <= 1:
        return 1.0
    return max(0.0, 1.0 - ((rank - 1) / (total - 1)))


def _infer_query_weights(query: str) -> tuple[float, float]:
    fts_weight = 1.0
    vector_weight = 1.0
    compact = query.replace(" ", "")

    if _looks_exactish(query):
        fts_weight += 0.35
    if _looks_descriptive(query):
        vector_weight += 0.35
    if fts_query.has_cjk(query) and len(compact) >= 8:
        vector_weight += 0.20

    total = fts_weight + vector_weight
    return fts_weight / total, vector_weight / total


def _looks_exactish(query: str) -> bool:
    return bool(
        re.search(r"\d", query)
        or re.search(r"['\"`“”‘’]", query)
        or re.search(r"\b[a-zA-Z]+[-_]?\d+[a-zA-Z0-9_-]*\b", query)
    )


def _looks_descriptive(query: str) -> bool:
    cues = (
        "感觉",
        "觉得",
        "为什么",
        "最近",
        "那种",
        "状态",
        "情绪",
        "压力",
        "焦虑",
        "难受",
        "怎么办",
    )
    return any(cue in query for cue in cues)


def _best_hits_by_post(
    fts_hits: list[RetrievalHit],
    vector_hits: list[RetrievalHit],
) -> dict[str, list[RetrievalHit]]:
    best: dict[str, dict[str, RetrievalHit]] = {}
    for hit in fts_hits + vector_hits:
        source_hits = best.setdefault(hit.post_id, {})
        current = source_hits.get(hit.source)
        if current is None or hit.rank < current.rank:
            source_hits[hit.source] = hit
    return {post_id: list(source_hits.values()) for post_id, source_hits in best.items()}


def _build_reasons(
    post_id: str,
    best_hits: dict[str, list[RetrievalHit]],
    bonus_reasons: list[str],
) -> tuple[list[str], list[str]]:
    hits = sorted(best_hits.get(post_id, []), key=lambda hit: hit.rank)
    sources = [hit.source for hit in hits]
    reasons = [f"{hit.source}:rank={hit.rank}" for hit in hits]
    if any(source != "vector" for source in sources) and "vector" in sources:
        reasons.append("agreement")
    if "like_fallback" in sources:
        reasons.extend(["like_fallback", "low_trust"])
    reasons.extend(bonus_reasons)
    return sources, reasons


def _content_bonus(query: str, content: str) -> tuple[float, list[str]]:
    if not content:
        return 0.0, []

    score = 0.0
    reasons: list[str] = []
    clean = fts_query.sanitize_fts5(query)
    phrase = clean.lower()
    compact = clean.replace(" ", "")
    content_lower = content.lower()
    compact_content = content_lower.replace(" ", "")
    if len(compact) >= 2 and (phrase in content_lower or compact.lower() in compact_content):
        score += 0.10
        reasons.append("exact_phrase")

    terms = fts_query.query_terms(clean)
    if terms:
        matched = sum(1 for term in terms if term.lower() in content_lower)
        coverage = matched / len(terms)
        if coverage > 0:
            score += 0.08 * coverage
            reasons.append(f"coverage={coverage:.2f}")
    return score, reasons


def _read_candidate_contents(post_ids: list[str]) -> dict[str, str]:
    if not post_ids:
        return {}
    placeholders = ", ".join("?" for _ in post_ids)
    rows = db.query_all(
        f"""
        SELECT id, content
        FROM posts
        WHERE id IN ({placeholders})
        """,
        tuple(post_ids),
    )
    return {row["id"]: row["content"] for row in rows}

def _log_fts_query_built(
    *,
    query: str,
    clean: str,
    fts_keywords: list[str],
    keyword_candidates: list[str],
    deterministic_candidates: list[str],
    match: str,
    table: str,
    source: str,
    fallback_type: str | None,
    hit_count: int,
    trace_context: dict | None,
) -> None:
    logging_service.log_event(
        "fts_query_built",
        **(trace_context or {}),
        target="posts",
        raw_query=query,
        sanitized_query=clean,
        fts_keywords=fts_keywords,
        keyword_candidates=keyword_candidates,
        deterministic_candidates=deterministic_candidates,
        match=match,
        table=table,
        source=source,
        fallback_type=fallback_type,
        hit_count=hit_count,
    )


def _log_hybrid_retrieval_result(
    *,
    query: str,
    semantic_query: str | None,
    fts_keywords: list[str] | None,
    fts_hits: list[RetrievalHit],
    vector_hits: list[RetrievalHit],
    final_hits: list[HybridHit],
    trace_context: dict | None,
) -> None:
    logging_service.log_event(
        "hybrid_retrieval_result",
        **(trace_context or {}),
        raw_query=query,
        semantic_query=semantic_query or query,
        fts_keywords=fts_keywords or [],
        fts_hits=[_retrieval_hit_payload(hit) for hit in fts_hits],
        vector_hits=[_retrieval_hit_payload(hit) for hit in vector_hits],
        final_hits=[_hybrid_hit_payload(hit) for hit in final_hits],
    )


def _log_hybrid_doc_retrieval_result(
    *,
    query: str,
    semantic_query: str | None,
    fts_keywords: list[str] | None,
    fts_hits: list[RetrievalHit],
    vector_hits: list,
    final_hits: list[RetrievalDocHit],
    trace_context: dict | None,
) -> None:
    logging_service.log_event(
        "hybrid_doc_retrieval_result",
        **(trace_context or {}),
        raw_query=query,
        semantic_query=semantic_query or query,
        fts_keywords=fts_keywords or [],
        fts_hits=[_retrieval_hit_payload(hit) for hit in fts_hits],
        vector_hits=[_vector_doc_hit_payload(hit) for hit in vector_hits],
        final_hits=[_doc_hit_payload(hit) for hit in final_hits],
    )


def _vector_doc_hit_payload(hit) -> dict:
    return {
        "doc_id": str(getattr(hit, "doc_id", "")),
        "type": str(getattr(hit, "type", "")),
        "source_id": str(getattr(hit, "source_id", "")),
        "rank": getattr(hit, "rank", None),
        "distance": getattr(hit, "distance", None),
    }


def _doc_hit_payload(hit: RetrievalDocHit) -> dict:
    return {
        "doc_id": hit.doc_id,
        "type": hit.type,
        "source_id": hit.source_id,
        "score": hit.score,
        "rank": hit.rank,
        "distance": hit.distance,
        "sources": hit.sources,
        "reasons": hit.reasons,
    }


def _retrieval_hit_payload(hit: RetrievalHit) -> dict:
    return {
        "post_id": hit.post_id,
        "source": hit.source,
        "rank": hit.rank,
        "raw_score": hit.raw_score,
    }


def _hybrid_hit_payload(hit: HybridHit) -> dict:
    return {
        "post_id": hit.post_id,
        "score": hit.score,
        "fts_score": hit.fts_score,
        "vector_score": hit.vector_score,
        "sources": hit.sources,
        "reasons": hit.reasons,
    }
