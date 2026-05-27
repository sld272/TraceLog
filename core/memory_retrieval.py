"""Boundary-aware retrieval over observation narratives."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Any

from core import db


@dataclass(frozen=True)
class RetrievalScope:
    channel: str
    soul_name: str | None = None
    post_id: str | None = None


@dataclass(frozen=True)
class EvidenceSnippet:
    source_type: str
    source_id: str
    excerpt: str
    evidence_access: str


@dataclass(frozen=True)
class MemoryHit:
    id: int
    type: str
    title: str
    summary: str | None
    narrative: str
    visibility_scope: str
    scope_post_id: str | None
    scope_soul_name: str | None
    importance: float
    confidence: float
    observed_at: float
    score: float
    sources: list[str]
    evidence_snippets: list[EvidenceSnippet]
    disclosure_level: str


def search_public_post_memory(query: str, related_post_ids: list[str], limit: int = 5) -> str:
    """Return formatted global observation memory for public post replies."""
    hits = _search_memory(
        query=query,
        related_post_ids=related_post_ids,
        allowed_scopes=[("global", None)],
        retrieval_scope=RetrievalScope(channel="public_post"),
        limit=limit,
    )
    return _format_memory_context(hits)


def search_chat_memory(query: str, soul_name: str, related_post_ids: list[str], limit: int = 5) -> str:
    """Return formatted global + current SOUL scoped memory for private chat."""
    hits = _search_memory(
        query=query,
        related_post_ids=related_post_ids,
        allowed_scopes=[("global", None), ("soul_scoped", soul_name)],
        retrieval_scope=RetrievalScope(channel="chat", soul_name=soul_name),
        limit=limit,
    )
    return _format_memory_context(hits)


def search_comment_memory(query: str, post_id: str, related_post_ids: list[str], limit: int = 5) -> str:
    """Return formatted global + same-post visible memory for comment threads."""
    hits = _search_memory(
        query=query,
        related_post_ids=related_post_ids,
        allowed_scopes=[("global", None), ("post_visible", post_id)],
        retrieval_scope=RetrievalScope(channel="comment_thread", post_id=post_id),
        limit=limit,
    )
    return _format_memory_context(hits)


def _search_memory(
    *,
    query: str,
    related_post_ids: list[str],
    allowed_scopes: list[tuple[str, str | None]],
    retrieval_scope: RetrievalScope,
    limit: int,
) -> list[MemoryHit]:
    if limit <= 0:
        return []
    candidates: dict[int, dict[str, Any]] = {}
    for rank, row in enumerate(_fts_observation_rows(query, allowed_scopes, max(limit * 4, 10)), start=1):
        _merge_candidate(candidates, row, source="fts", base_score=_position_score(rank, max(limit * 4, 10)))
    for rank, row in enumerate(_indirect_observation_rows(related_post_ids), start=1):
        _merge_candidate(candidates, row, source="post_semantic", base_score=0.72 * _position_score(rank, max(len(related_post_ids), 1)))

    hits = [_candidate_to_hit(candidate) for candidate in candidates.values()]
    hits.sort(
        key=lambda hit: (
            hit.score,
            hit.importance,
            hit.confidence,
            hit.observed_at,
            -hit.id,
        ),
        reverse=True,
    )
    return _apply_progressive_disclosure(hits[:limit], retrieval_scope)


def _fts_observation_rows(
    query: str,
    allowed_scopes: list[tuple[str, str | None]],
    limit: int,
) -> list[Any]:
    match = _build_match_query(query)
    if not match:
        return []
    scope_sql, params = _scope_filter_sql(allowed_scopes)
    sql = f"""
        SELECT observations.*
        FROM observations_fts
        JOIN observations ON observations.id = observations_fts.rowid
        WHERE observations_fts MATCH ?
          AND observations.status = 'active'
          AND observations.visibility_scope != 'private_blocked'
          AND ({scope_sql})
        ORDER BY rank
        LIMIT ?
    """
    try:
        return db.query_all(sql, (match, *params, limit))
    except sqlite3.Error:
        return []


def _indirect_observation_rows(related_post_ids: list[str]) -> list[Any]:
    post_ids = _ordered_unique([post_id for post_id in related_post_ids if isinstance(post_id, str) and post_id.strip()])
    if not post_ids:
        return []
    placeholders = ", ".join("?" for _ in post_ids)
    rows = db.query_all(
        f"""
        SELECT observations.*, observation_sources.source_id AS matched_source_id
        FROM observations
        JOIN observation_sources
          ON observation_sources.observation_id = observations.id
        WHERE observations.status = 'active'
          AND observations.visibility_scope = 'global'
          AND observation_sources.source_type = 'post'
          AND observation_sources.source_id IN ({placeholders})
        """,
        tuple(post_ids),
    )
    order = {post_id: index for index, post_id in enumerate(post_ids)}
    ranked_rows: dict[int, tuple[int, Any]] = {}
    for row in rows:
        observation_id = int(row["id"])
        rank = order.get(row["matched_source_id"], len(order))
        existing = ranked_rows.get(observation_id)
        if existing is None or rank < existing[0]:
            ranked_rows[observation_id] = (rank, row)
    return [
        row
        for _, row in sorted(
            ranked_rows.values(),
            key=lambda item: (item[0], int(item[1]["id"])),
        )
    ]


def _scope_filter_sql(allowed_scopes: list[tuple[str, str | None]]) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    for visibility_scope, scope_value in allowed_scopes:
        if visibility_scope == "global":
            clauses.append("observations.visibility_scope = 'global'")
        elif visibility_scope == "soul_scoped":
            clauses.append("(observations.visibility_scope = 'soul_scoped' AND observations.scope_soul_name = ?)")
            params.append(scope_value)
        elif visibility_scope == "post_visible":
            clauses.append("(observations.visibility_scope = 'post_visible' AND observations.scope_post_id = ?)")
            params.append(scope_value)
    if not clauses:
        return "0", []
    return " OR ".join(clauses), params


def _merge_candidate(candidates: dict[int, dict[str, Any]], row, *, source: str, base_score: float) -> None:
    observation_id = int(row["id"])
    candidate = candidates.get(observation_id)
    if candidate is None:
        candidate = {
            "row": row,
            "base_score": 0.0,
            "sources": [],
        }
        candidates[observation_id] = candidate
    candidate["base_score"] = max(float(candidate["base_score"]), base_score)
    if source not in candidate["sources"]:
        candidate["sources"].append(source)


def _candidate_to_hit(candidate: dict[str, Any]) -> MemoryHit:
    row = candidate["row"]
    sources = candidate["sources"]
    base = float(candidate["base_score"])
    agreement_bonus = 0.10 if len(sources) > 1 else 0.0
    importance = float(row["importance"])
    confidence = float(row["confidence"])
    score = min(base + agreement_bonus + (importance * 0.10) + (confidence * 0.08), 1.0)
    return MemoryHit(
        id=int(row["id"]),
        type=row["type"],
        title=row["title"],
        summary=row["summary"],
        narrative=row["narrative"],
        visibility_scope=row["visibility_scope"],
        scope_post_id=row["scope_post_id"],
        scope_soul_name=row["scope_soul_name"],
        importance=importance,
        confidence=confidence,
        observed_at=float(row["observed_at"]),
        score=round(score, 6),
        sources=list(sources),
        evidence_snippets=[],
        disclosure_level="L1",
    )


def _apply_progressive_disclosure(hits: list[MemoryHit], retrieval_scope: RetrievalScope) -> list[MemoryHit]:
    disclosed: list[MemoryHit] = []
    l2_count = 0
    for hit in hits:
        snippets: list[EvidenceSnippet] = []
        level = "L1"
        if l2_count < 2:
            snippets = _allowed_evidence_snippets(hit, retrieval_scope)
            if snippets:
                level = "L2"
                l2_count += 1
        disclosed.append(
            MemoryHit(
                id=hit.id,
                type=hit.type,
                title=hit.title,
                summary=hit.summary,
                narrative=hit.narrative,
                visibility_scope=hit.visibility_scope,
                scope_post_id=hit.scope_post_id,
                scope_soul_name=hit.scope_soul_name,
                importance=hit.importance,
                confidence=hit.confidence,
                observed_at=hit.observed_at,
                score=hit.score,
                sources=hit.sources,
                evidence_snippets=snippets,
                disclosure_level=level,
            )
        )
    return disclosed


def _allowed_evidence_snippets(hit: MemoryHit, retrieval_scope: RetrievalScope) -> list[EvidenceSnippet]:
    rows = db.query_all(
        """
        SELECT source_type, source_id, excerpt, evidence_access
        FROM observation_sources
        WHERE observation_id = ?
        ORDER BY source_type, source_id
        """,
        (hit.id,),
    )
    snippets: list[EvidenceSnippet] = []
    for row in rows:
        excerpt = row["excerpt"]
        if not isinstance(excerpt, str) or not excerpt.strip():
            continue
        if not _can_expand_evidence(hit, row["evidence_access"], retrieval_scope):
            continue
        snippets.append(
            EvidenceSnippet(
                source_type=row["source_type"],
                source_id=row["source_id"],
                excerpt=_truncate_excerpt(excerpt.strip()),
                evidence_access=row["evidence_access"],
            )
        )
    return snippets


def _can_expand_evidence(hit: MemoryHit, evidence_access: str, retrieval_scope: RetrievalScope) -> bool:
    if evidence_access == "all":
        return True
    if evidence_access == "post_visible":
        return (
            retrieval_scope.channel == "comment_thread"
            and hit.scope_post_id is not None
            and hit.scope_post_id == retrieval_scope.post_id
        )
    if evidence_access == "source_soul_only":
        return (
            retrieval_scope.channel == "chat"
            and hit.scope_soul_name is not None
            and hit.scope_soul_name == retrieval_scope.soul_name
        )
    return False


def _format_memory_context(hits: list[MemoryHit]) -> str:
    if not hits:
        return ""
    lines = ["# 相关记忆"]
    for hit in hits:
        scope = _scope_label(hit)
        summary = f"；{hit.summary}" if hit.summary else ""
        lines.append(
            f"- [{hit.id}] {hit.disclosure_level} ({hit.type}/{scope}) {hit.title}{summary}：{hit.narrative}"
        )
        for snippet in hit.evidence_snippets:
            lines.append(
                f"  evidence({snippet.source_type}:{snippet.source_id}): {snippet.excerpt}"
            )
    return "\n".join(lines)


def _scope_label(hit: MemoryHit) -> str:
    if hit.visibility_scope == "global":
        return "global"
    if hit.visibility_scope == "soul_scoped":
        return f"soul:{hit.scope_soul_name}"
    if hit.visibility_scope == "post_visible":
        return f"post:{hit.scope_post_id}"
    return hit.visibility_scope


def _position_score(rank: int, total: int) -> float:
    if total <= 1:
        return 1.0
    return max(0.0, 1.0 - ((rank - 1) / (total - 1)))


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _build_match_query(query: str) -> str:
    clean = _sanitize_fts5(query)
    if not clean:
        return ""
    terms = _query_terms(clean)
    if not terms:
        terms = [clean]
    quoted_terms = []
    for term in terms:
        escaped = term.replace('"', '""')
        quoted_terms.append(f'"{escaped}"')
    return " OR ".join(quoted_terms[:8])


def _sanitize_fts5(query: str) -> str:
    text = re.sub(r'["\'`()*:^{}[\]]+', " ", query)
    return " ".join(text.split())


def _truncate_excerpt(excerpt: str, limit: int = 160) -> str:
    text = " ".join(excerpt.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _query_terms(query: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[A-Za-z0-9_+-]+|[\u4e00-\u9fff]{2,}", query)
        if len(term.strip()) >= 2
    ]
