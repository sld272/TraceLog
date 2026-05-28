"""Safe wrapper around LLM query rewrite."""

from __future__ import annotations

from dataclasses import dataclass
import re

from core.llm import query_rewrite_router
from core.llm.types import LLMClient


MAX_KEYWORDS = 12
MAX_KEYWORD_CHARS = 16
MAX_SEMANTIC_QUERY_CHARS = 120
MIN_SEMANTIC_QUERY_CHARS = 4
MIN_REWRITE_COMPACT_CHARS = 6


@dataclass(frozen=True)
class RewrittenQuery:
    raw_query: str
    semantic_query: str
    keywords: list[str]
    used_rewrite: bool
    rewrite_skipped_by_gate: bool = False


def rewrite_query(
    client: LLMClient,
    model: str,
    raw_query: str,
    channel: str,
    trace_context: dict | None = None,
) -> RewrittenQuery:
    """Rewrite a retrieval query, falling back to raw query on any invalid result."""
    raw = str(raw_query or "").strip()
    fallback = RewrittenQuery(raw_query=raw, semantic_query=raw, keywords=[], used_rewrite=False)
    if not should_rewrite_query(raw, channel):
        return RewrittenQuery(
            raw_query=raw,
            semantic_query=raw,
            keywords=[],
            used_rewrite=False,
            rewrite_skipped_by_gate=True,
        )
    if not raw:
        return fallback

    data = query_rewrite_router.call_query_rewrite(
        client=client,
        model=model,
        raw_query=raw,
        channel=channel,
        trace_context=trace_context,
    )
    if data is None:
        return fallback

    semantic_query = _normalize_text(data.get("semantic_query"), limit=MAX_SEMANTIC_QUERY_CHARS)
    keywords = _normalize_keywords(data.get("keywords"))
    if len(semantic_query) < MIN_SEMANTIC_QUERY_CHARS and not keywords:
        return fallback
    if len(semantic_query) < MIN_SEMANTIC_QUERY_CHARS:
        semantic_query = raw
    return RewrittenQuery(
        raw_query=raw,
        semantic_query=semantic_query,
        keywords=keywords,
        used_rewrite=True,
    )


def should_rewrite_query(raw_query: str, channel: str) -> bool:
    del channel
    raw = str(raw_query or "").strip()
    if not raw:
        return False
    if raw.startswith("/"):
        return False
    compact = "".join(raw.split())
    if len(compact) < MIN_REWRITE_COMPACT_CHARS:
        return False
    if _is_short_ascii_token_query(raw):
        return False
    return True


def _normalize_keywords(value) -> list[str]:
    if not isinstance(value, list):
        return []
    keywords: list[str] = []
    for item in value:
        keyword = _normalize_text(item, limit=MAX_KEYWORD_CHARS)
        if len(keyword) < 2:
            continue
        if keyword not in keywords:
            keywords.append(keyword)
        if len(keywords) >= MAX_KEYWORDS:
            break
    return keywords


def _normalize_text(value, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(char for char in value if ord(char) >= 32 and char != "\x7f")
    text = " ".join(text.split())
    return text[:limit].strip()


def _is_short_ascii_token_query(query: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9_+-]+", query)
    if not tokens:
        return False
    joined = "".join(tokens)
    compact = "".join(query.split())
    if joined != compact:
        return False
    return len(tokens) <= 2
