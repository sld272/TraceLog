"""Shared FTS5 query construction helpers."""

from __future__ import annotations

import re

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

LOW_INFORMATION_BOUNDARY = set("我你他她它这那哪什怎是有没不吗呢吧啊呀的了过前")


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
        candidates.append(compact[:MAX_PHRASE_CHARS])
        candidates.extend(_cjk_window_candidates(clean, max_terms=max_terms * 4))

    candidates.extend(query_terms(clean))
    return ordered_unique([item for item in candidates if len(item.strip()) >= 2])[:max_terms]


def query_terms(query: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[A-Za-z0-9_+-]+|[\u4e00-\u9fff]{2,}", query)
        if len(term.strip()) >= 2
    ]


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def is_short_cjk(term: str) -> bool:
    """A CJK term under 3 chars: the trigram tokenizer emits no token for it, so it
    can never MATCH and needs a LIKE fallback (2-char words like \u8003\u7814/\u590d\u4e60)."""
    compact = term.replace(" ", "")
    return bool(compact) and has_cjk(compact) and len(compact) < 3


def _cjk_window_candidates(query: str, *, max_terms: int) -> list[str]:
    segments = re.findall(r"[\u4e00-\u9fff]+", query)
    by_size: dict[int, list[tuple[int, int, str]]] = {4: [], 3: [], 2: []}
    for segment in segments:
        for size in (4, 3, 2):
            if len(segment) < size:
                continue
            for index in range(0, len(segment) - size + 1):
                term = segment[index:index + size]
                if _is_low_information_cjk(term):
                    continue
                by_size[size].append((_cjk_candidate_score(term), index, term))

    ordered: list[str] = []
    quotas = {4: 2, 3: 3, 2: max(max_terms - 5, 0)}
    for size in (4, 3, 2):
        ranked = [
            term
            for _score, _index, term in sorted(
                by_size[size],
                key=lambda item: (-item[0], item[1]),
            )
        ]
        ordered.extend(ordered_unique(ranked)[:quotas[size]])
    return ordered_unique(ordered)[:max_terms]


def _cjk_candidate_score(term: str) -> int:
    score = len(term) * 10
    if term[0] in LOW_INFORMATION_BOUNDARY:
        score -= 8
    if term[-1] in LOW_INFORMATION_BOUNDARY:
        score -= 8
    if len(term) == 2:
        score += 4
    return score


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
