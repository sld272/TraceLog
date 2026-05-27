"""Boundary-aware retrieval over observation narratives."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Any

from core import db


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


def search_public_post_memory(query: str, related_post_ids: list[str], limit: int = 5) -> str:
    """Return formatted global observation memory for public post replies."""
    hits = _search_memory(
        query=query,
        related_post_ids=related_post_ids,
        allowed_scopes=[("global", None)],
        limit=limit,
    )
    return _format_memory_context(hits)


def search_chat_memory(query: str, soul_name: str, related_post_ids: list[str], limit: int = 5) -> str:
    """Return formatted global + current SOUL scoped memory for private chat."""
    hits = _search_memory(
        query=query,
        related_post_ids=related_post_ids,
        allowed_scopes=[("global", None), ("soul_scoped", soul_name)],
        limit=limit,
    )
    return _format_memory_context(hits)


def search_comment_memory(query: str, post_id: str, related_post_ids: list[str], limit: int = 5) -> str:
    """Return formatted global + same-post visible memory for comment threads."""
    hits = _search_memory(
        query=query,
        related_post_ids=related_post_ids,
        allowed_scopes=[("global", None), ("post_visible", post_id)],
        limit=limit,
    )
    return _format_memory_context(hits)


def _search_memory(
    *,
    query: str,
    related_post_ids: list[str],
    allowed_scopes: list[tuple[str, str | None]],
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
    return hits[:limit]


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
        SELECT DISTINCT observations.*
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
    return sorted(
        rows,
        key=lambda row: min(
            order.get(source["source_id"], len(order))
            for source in db.query_all(
                """
                SELECT source_id
                FROM observation_sources
                WHERE observation_id = ?
                  AND source_type = 'post'
                """,
                (row["id"],),
            )
        ),
    )


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
    )


def _format_memory_context(hits: list[MemoryHit]) -> str:
    if not hits:
        return ""
    lines = ["# 相关记忆"]
    for hit in hits:
        scope = _scope_label(hit)
        summary = f"；{hit.summary}" if hit.summary else ""
        lines.append(
            f"- [{hit.id}] ({hit.type}/{scope}) {hit.title}{summary}：{hit.narrative}"
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


def _query_terms(query: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[A-Za-z0-9_+-]+|[\u4e00-\u9fff]{2,}", query)
        if len(term.strip()) >= 2
    ]
