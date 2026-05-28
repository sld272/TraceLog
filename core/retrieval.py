"""Hybrid retrieval over SQLite FTS5 and ChromaDB."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3

from core import db
from core import fts_query

LIKE_FALLBACK_TRUST = 0.85


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


def fts_search(query: str, k: int = 20) -> list[str]:
    """Return post ids ranked by SQLite FTS5, with a short-CJK fallback."""
    return [hit.post_id for hit in fts_search_scored(query, k)]


def fts_search_scored(query: str, k: int = 20) -> list[RetrievalHit]:
    """Return FTS hits with source, rank position, and raw FTS rank for debug."""
    clean = _sanitize_fts5(query)
    if not clean:
        return []

    if _has_cjk(clean):
        if len(clean.replace(" ", "")) < 3:
            return _like_search_scored(clean, k)
        table = "posts_fts_trigram"
        source = "fts_trigram"
    else:
        table = "posts_fts"
        source = "fts"

    match = _build_match_query(clean)
    sql = f"""
        SELECT posts.id, rank AS raw_rank
        FROM {table}
        JOIN posts ON posts.rowid = {table}.rowid
        WHERE {table} MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    try:
        return [
            RetrievalHit(
                post_id=row["id"],
                source=source,
                rank=index + 1,
                raw_score=float(row["raw_rank"]) if row["raw_rank"] is not None else None,
            )
            for index, row in enumerate(db.query_all(sql, (match, k)))
        ]
    except sqlite3.Error:
        return []


def vector_search(query: str, k: int = 20) -> list[str]:
    """Return post ids ranked by ChromaDB semantic search."""
    return [hit.post_id for hit in vector_search_scored(query, k)]


def vector_search_scored(query: str, k: int = 20) -> list[RetrievalHit]:
    """Return vector hits with rank and Chroma distance when available."""
    try:
        from core import vectorstore

        return [
            RetrievalHit(
                post_id=hit.post_id,
                source="vector",
                rank=hit.rank,
                raw_score=hit.distance,
            )
            for hit in vectorstore.query_post_hits(query, n_results=k)
        ]
    except Exception:
        return []


def hybrid_search(query: str, k: int = 3) -> list[str]:
    """Return top hybrid result ids for prompt context."""
    return [hit.post_id for hit in hybrid_search_scored(query, k=k, min_score=None)]


def hybrid_search_scored(
    query: str,
    k: int = 3,
    min_score: float | None = None,
    candidate_k: int = 20,
    allow_fallback: bool = True,
) -> list[HybridHit]:
    """Combine FTS5 and ChromaDB with dynamic weights and explainable scores."""
    fts_hits = fts_search_scored(query, k=candidate_k)
    vector_hits = vector_search_scored(query, k=candidate_k)
    if not fts_hits and not vector_hits:
        return []

    post_ids = _ordered_unique([hit.post_id for hit in fts_hits + vector_hits])
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
        return ordered[:k]

    filtered = [hit for hit in ordered if hit.score >= min_score]
    if filtered:
        return filtered[:k]
    if allow_fallback and ordered:
        return ordered[: min(k, 1)]
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
    if _has_cjk(query) and len(compact) >= 8:
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
    clean = _sanitize_fts5(query)
    phrase = clean.lower()
    compact = clean.replace(" ", "")
    content_lower = content.lower()
    compact_content = content_lower.replace(" ", "")
    if len(compact) >= 2 and (phrase in content_lower or compact.lower() in compact_content):
        score += 0.10
        reasons.append("exact_phrase")

    terms = _query_terms(clean)
    if terms:
        matched = sum(1 for term in terms if term.lower() in content_lower)
        coverage = matched / len(terms)
        if coverage > 0:
            score += 0.08 * coverage
            reasons.append(f"coverage={coverage:.2f}")
    return score, reasons


def _query_terms(query: str) -> list[str]:
    return fts_query.query_terms(query)


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


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _sanitize_fts5(query: str) -> str:
    return fts_query.sanitize_fts5(query)


def _build_match_query(query: str) -> str:
    return fts_query.build_match_query(query)


def _has_cjk(text: str) -> bool:
    return fts_query.has_cjk(text)
