"""Shared FTS5 query construction helpers."""

from __future__ import annotations

import re

import jieba

# jieba prints "Building prefix dict..." to stderr on first use; silence it.
jieba.setLogLevel(60)

MAX_MATCH_TERMS = 16
MAX_PHRASE_CHARS = 32

LOW_INFORMATION_CJK = {
    "我之",
    "之前",
    "是不",
    "不是",
    "是不是",
    "说过",
    "有没有",
    "什么",
    "怎么",
    "这个",
    "那个",
}

def sanitize_fts5(query: str) -> str:
    text = re.sub(r'["\'`()*:^{}[\]]+', " ", query)
    return " ".join(text.split())


def build_match_query(query: str, *, max_terms: int = MAX_MATCH_TERMS) -> str:
    clean = sanitize_fts5(query)
    if not clean or max_terms <= 0:
        return ""

    candidates = match_candidates(clean, max_terms=max_terms)
    quoted_terms = []
    for term in candidates:
        escaped = term.replace('"', '""')
        quoted_terms.append(f'"{escaped}"')
    return " OR ".join(quoted_terms)


def quote_match_candidates(candidates: list[str]) -> str:
    quoted_terms = []
    for term in candidates:
        escaped = term.replace('"', '""')
        quoted_terms.append(f'"{escaped}"')
    return " OR ".join(quoted_terms)


def match_candidates(query: str, *, max_terms: int = MAX_MATCH_TERMS) -> list[str]:
    clean = sanitize_fts5(query)
    if not clean or max_terms <= 0:
        return []

    candidates: list[str] = []
    compact = clean.replace(" ", "")
    if has_cjk(clean) and len(compact) >= 2:
        # the whole CJK run, for a precise trigram match on contiguous content
        candidates.append(compact[:MAX_PHRASE_CHARS])

    candidates.extend(query_terms(clean))
    return ordered_unique([item for item in candidates if len(item.strip()) >= 2])[:max_terms]


def query_terms(query: str) -> list[str]:
    """Retrieval words for FTS MATCH / LIKE routing: jieba PRECISE mode keeps a real
    word like \u56fe\u4e66\u9986 whole but splits \u8003\u7814\u590d\u4e60 into \u8003\u7814/\u590d\u4e60 \u2014 exactly what the
    short-CJK LIKE fallback needs, without over-recalling \u56fe\u4e66/\u4e66\u9986."""
    return _segment(query, jieba.lcut)


def search_terms(query: str) -> list[str]:
    """Terms for keyword-overlap SCORING (freshness ordering / recall sentence
    pick): jieba SEARCH mode adds fine-grained sub-words on top of the main words,
    so a long term split across the content still overlaps. The extra granularity
    only ranks candidates, never gates recall, so its noise is harmless here \u2014
    unlike query_terms, which must stay precise for the LIKE routing."""
    return _segment(query, jieba.lcut_for_search)


def _segment(query: str, cut) -> list[str]:
    """Split into words via ``cut`` (a jieba cutter) for CJK runs and regex for
    ASCII/number runs; drop single chars and low-information words. Order-preserving,
    deduped."""
    terms: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9_+-]+|[\u4e00-\u9fff]+", str(query or "")):
        if has_cjk(chunk):
            terms.extend(cut(chunk))
        else:
            terms.append(chunk)
    return ordered_unique([
        term
        for term in terms
        if len(term.strip()) >= 2 and not _is_low_information_cjk(term)
    ])


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def is_short_cjk(term: str) -> bool:
    """A CJK term under 3 chars: the trigram tokenizer emits no token for it, so it
    can never MATCH and needs a LIKE fallback (2-char words like \u8003\u7814/\u590d\u4e60)."""
    compact = term.replace(" ", "")
    return bool(compact) and has_cjk(compact) and len(compact) < 3


def _is_low_information_cjk(term: str) -> bool:
    return any(stop in term for stop in LOW_INFORMATION_CJK)


def ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
