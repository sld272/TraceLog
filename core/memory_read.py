"""Phase 5 read model: scope-filtered memory retrieval for reply assembly.

Pure read functions (no hot-path wiring yet) that the reply contexts will use:

  * recent_state_block: the always-on "当前状态" block — recent `state` units in
    the admissible scopes, recency×importance ordered, within a per-type expiry
    window, budget-capped.
  * retrieve_units: query-relevant beliefs in the admissible scopes (excluding
    units already carried by the portrait or the state block, to avoid double
    injection).

Both run every candidate through memory_scope_policy: public-scene memory is
shared across souls; a soul's own private memory is admitted but flagged
needs_discretion in public scenes; other souls' private memory is never
returned. Units the policy forbids are filtered in SQL/▸code before they can
reach a prompt — never left to the model to self-censor.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core import db, memory_scope_policy as policy

# current-state block
STATE_BLOCK_LIMIT = 5
STATE_WINDOW_DAYS = 7        # state units older than this are not "current"
DAY_SECONDS = 86400.0

# unit retrieval
RETRIEVE_DEFAULT_K = 8
# states live in the state block; in_md_slice units live in the portrait — both
# excluded here so relevant-memory retrieval surfaces the mid-tier beliefs.
_RETRIEVE_EXCLUDED_TYPES = ("state",)


@dataclass(frozen=True)
class MemoryItem:
    unit_id: str
    type: str
    content: str
    confidence: float
    importance: float
    owner_scope: str
    visibility_scope: str
    needs_discretion: bool  # own-private memory surfaced in a public scene


def _allowed_visibility_sql(plan: dict) -> tuple[str, list]:
    """Build a WHERE fragment + params admitting public-scene visibility and,
    if the plan allows, the reply soul's own private scope."""
    clauses = ["(visibility_scope = 'public' OR visibility_scope LIKE 'thread:%')"]
    params: list = []
    if plan.get("private_self"):
        clauses[0] = "(" + clauses[0] + " OR visibility_scope = ?)"
        params.append(plan["private_self"])
    return "(" + clauses[0] + ")", params


def _discretion_for(visibility_scope: str, channel: str, reply_soul: str | None) -> bool:
    return policy.classify(visibility_scope, channel=channel, reply_soul=reply_soul).needs_discretion


def recent_state_block(
    channel: str,
    reply_soul: str | None,
    *,
    now: float | None = None,
    limit: int = STATE_BLOCK_LIMIT,
) -> list[MemoryItem]:
    """The always-on current-state block: recent active `state` units in the
    admissible scopes, within the expiry window, ranked by recency×importance."""
    now = db.now_ts() if now is None else now
    cutoff = now - STATE_WINDOW_DAYS * DAY_SECONDS
    plan = policy.admissible_visibility_filters(channel, reply_soul)
    vis_sql, params = _allowed_visibility_sql(plan)

    rows = db.query_all(
        f"""
        SELECT id, type, content, confidence, importance, owner_scope, visibility_scope,
               last_confirmed
        FROM memory_units
        WHERE type = 'state'
          AND status = 'active'
          AND prompt_policy = 'allow'
          AND last_confirmed >= ?
          AND {vis_sql}
        """,
        (cutoff, *params),
    )
    ranked = sorted(
        rows,
        key=lambda r: (_recency_weight(r["last_confirmed"], now) * float(r["importance"])),
        reverse=True,
    )
    items = [_row_to_item(r, channel, reply_soul) for r in ranked[:limit]]
    return items


def retrieve_units(
    query: str,
    channel: str,
    reply_soul: str | None,
    *,
    k: int = RETRIEVE_DEFAULT_K,
) -> list[MemoryItem]:
    """Query-relevant beliefs in the admissible scopes, excluding state units
    (state block) and portrait members (in_md_slice). MVP scoring: keyword
    overlap first, then importance, then recency — no vector index yet."""
    plan = policy.admissible_visibility_filters(channel, reply_soul)
    vis_sql, params = _allowed_visibility_sql(plan)
    type_placeholders = ",".join("?" for _ in _RETRIEVE_EXCLUDED_TYPES)

    rows = db.query_all(
        f"""
        SELECT id, type, content, confidence, importance, owner_scope, visibility_scope,
               last_confirmed
        FROM memory_units
        WHERE status = 'active'
          AND prompt_policy = 'allow'
          AND in_md_slice = 0
          AND type NOT IN ({type_placeholders})
          AND {vis_sql}
        """,
        (*_RETRIEVE_EXCLUDED_TYPES, *params),
    )

    terms = _tokenize(query)
    now = db.now_ts()

    def score(r: sqlite3.Row) -> tuple:
        overlap = _keyword_overlap(str(r["content"]), terms)
        return (overlap, float(r["importance"]), _recency_weight(r["last_confirmed"], now))

    ranked = sorted(rows, key=score, reverse=True)
    return [_row_to_item(r, channel, reply_soul) for r in ranked[:k]]


# --- helpers ---------------------------------------------------------------

def _row_to_item(row: sqlite3.Row, channel: str, reply_soul: str | None) -> MemoryItem:
    return MemoryItem(
        unit_id=row["id"],
        type=row["type"],
        content=row["content"],
        confidence=float(row["confidence"]),
        importance=float(row["importance"]),
        owner_scope=row["owner_scope"],
        visibility_scope=row["visibility_scope"],
        needs_discretion=_discretion_for(row["visibility_scope"], channel, reply_soul),
    )


def _recency_weight(last_confirmed: float, now: float) -> float:
    age_days = max(0.0, (now - float(last_confirmed)) / DAY_SECONDS)
    # gentle exponential-ish decay; 1.0 fresh, ~0.5 at one week
    return 1.0 / (1.0 + age_days / 7.0)


def _tokenize(query: str) -> list[str]:
    raw = "".join(c if c.isalnum() else " " for c in str(query or "")).split()
    # for CJK, also add 2-grams so substring-ish matches work without an FTS index
    grams: list[str] = []
    text = "".join(str(query or "").split())
    for token in raw:
        if len(token) >= 2:
            grams.append(token)
    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        if bigram.strip():
            grams.append(bigram)
    return list({g for g in grams if g})


def _keyword_overlap(content: str, terms: list[str]) -> int:
    if not terms:
        return 0
    return sum(1 for t in terms if t in content)
