"""Hybrid retrieval over SQLite FTS5 and ChromaDB."""

from __future__ import annotations

import re
import sqlite3

from core import db

RRF_K = 60


def fts_search(query: str, k: int = 20) -> list[str]:
    """Return post ids ranked by SQLite FTS5, with a short-CJK fallback."""
    clean = _sanitize_fts5(query)
    if not clean:
        return []

    if _has_cjk(clean):
        if len(clean.replace(" ", "")) < 3:
            return _like_search(clean, k)
        table = "posts_fts_trigram"
    else:
        table = "posts_fts"

    match = _build_match_query(clean)
    sql = f"""
        SELECT posts.id
        FROM {table}
        JOIN posts ON posts.rowid = {table}.rowid
        WHERE {table} MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    try:
        return [row["id"] for row in db.query_all(sql, (match, k))]
    except sqlite3.Error:
        return []


def vector_search(query: str, k: int = 20) -> list[str]:
    """Return post ids ranked by ChromaDB semantic search."""
    try:
        from core import vectorstore

        return vectorstore.query_post_ids(query, n_results=k)
    except Exception:
        return []


def hybrid_search(query: str, k: int = 3) -> list[str]:
    """Combine FTS5 and ChromaDB results with reciprocal rank fusion."""
    fts_hits = fts_search(query, k=20)
    vector_hits = vector_search(query, k=20)
    scores: dict[str, float] = {}

    for rank, post_id in enumerate(fts_hits, start=1):
        scores[post_id] = scores.get(post_id, 0.0) + 1.0 / (RRF_K + rank)
    for rank, post_id in enumerate(vector_hits, start=1):
        scores[post_id] = scores.get(post_id, 0.0) + 1.0 / (RRF_K + rank)

    return [
        post_id
        for post_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k]
    ]


def _like_search(query: str, k: int) -> list[str]:
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
    return [row["id"] for row in rows]


def _sanitize_fts5(query: str) -> str:
    text = re.sub(r'["\'`()*:^{}[\]]+', " ", query)
    return " ".join(text.split())


def _build_match_query(query: str) -> str:
    if _has_cjk(query):
        escaped = query.replace('"', '""')
        return f'"{escaped}"'
    terms = [term for term in query.split() if term]
    if not terms:
        return ""
    quoted_terms = []
    for term in terms:
        escaped = term.replace('"', '""')
        quoted_terms.append(f'"{escaped}"')
    return " OR ".join(quoted_terms)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)
