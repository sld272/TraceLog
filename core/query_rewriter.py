"""Safe wrapper around LLM query rewrite."""

from __future__ import annotations

from dataclasses import dataclass

from core.llm import query_rewrite_router
from core.llm.types import LLMClient


MAX_KEYWORDS = 12
MAX_KEYWORD_CHARS = 16
MAX_SEMANTIC_QUERY_CHARS = 120
MIN_SEMANTIC_QUERY_CHARS = 4

# How many recent conversation turns to hand the rewrite as anaphora context, and
# the per-turn char clip.
REWRITE_CONTEXT_TURNS = 6
MAX_TURN_CHARS = 200


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
    *,
    recent_turns: list[dict] | None = None,
    trace_context: dict | None = None,
) -> RewrittenQuery:
    """Rewrite a retrieval query, falling back to raw query on any invalid result.

    ``recent_turns`` ([{role, content}]) gives the model the conversation context
    it needs to resolve anaphora/ellipsis in ``raw_query``."""
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
        recent_turns=recent_turns,
        trace_context=trace_context,
    )
    if data is None:
        return fallback

    return rewrite_from_fields(raw, data.get("semantic_query"), data.get("keywords"))


def rewrite_from_fields(raw_query: str, semantic_query, keywords) -> RewrittenQuery:
    """Build a RewrittenQuery from already-extracted (semantic_query, keywords),
    applying the same normalization/thresholds as ``rewrite_query``. Falls back to
    the raw query when the rewrite is too thin to help retrieval.

    Lets the merged turn-prep call reuse the rewrite half's cleaning without a
    second LLM round trip, while keeping ``rewrite_query``'s behavior identical."""
    raw = str(raw_query or "").strip()
    fallback = RewrittenQuery(raw_query=raw, semantic_query=raw, keywords=[], used_rewrite=False)
    semantic_query = _normalize_text(semantic_query, limit=MAX_SEMANTIC_QUERY_CHARS)
    keywords = _normalize_keywords(keywords)
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
    """Reply paths rewrite every turn (a thin/anaphoric message is exactly what
    needs rewriting), so the only skips are an empty query and slash-commands."""
    del channel
    raw = str(raw_query or "").strip()
    if not raw:
        return False
    if raw.startswith("/"):
        return False
    return True


def recent_turns(messages: list, *, limit: int = REWRITE_CONTEXT_TURNS) -> list[dict]:
    """[{role, content}] for the last `limit` non-empty turns — the anaphora
    context handed to the rewrite. Accepts any objects exposing .role/.content."""
    turns: list[dict] = []
    for message in messages[-limit:]:
        content = str(getattr(message, "content", "") or "").strip()
        if not content:
            continue
        turns.append({"role": getattr(message, "role", "user"), "content": content[:MAX_TURN_CHARS]})
    return turns


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
