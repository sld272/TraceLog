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

import os
import re
import sqlite3
from dataclasses import dataclass, field

from core import db, memory_events_service as mes, memory_scope_policy as policy, memory_unit_service as mus, memory_view_service as mvs

# read-mode flag (design §7.2). legacy = pre-v2 behavior, no unit reading.
READ_MODE_ENV = "MEMORY_V2_READ_MODE"
_READ_MODES = ("legacy", "units", "units_and_freshness")


def read_mode() -> str:
    mode = os.environ.get(READ_MODE_ENV, "legacy").strip().lower()
    return mode if mode in _READ_MODES else "legacy"


def memory_reading_enabled() -> bool:
    return read_mode() != "legacy"


# write-mode flag (design §7.2). legacy = pre-v2 light/deep reflection rewriting
# markdown; reconcile = event-driven unit reconcile is the write path. Kept here
# beside the read-mode flag so all memory-v2 toggles live in one place.
WRITE_MODE_ENV = "MEMORY_V2_WRITE_MODE"
_WRITE_MODES = ("legacy", "reconcile")


def write_mode() -> str:
    mode = os.environ.get(WRITE_MODE_ENV, "legacy").strip().lower()
    return mode if mode in _WRITE_MODES else "legacy"


def reconcile_write_enabled() -> bool:
    return write_mode() == "reconcile"


def memory_section_for(channel: str, reply_soul: str | None, query: str) -> str:
    """Single entry the reply paths call. Returns '' in legacy mode (zero change)
    or when there is no memory to inject."""
    if not memory_reading_enabled():
        return ""
    return build_memory_section(channel, reply_soul, query).text

# current-state block
STATE_BLOCK_LIMIT = 5
STATE_WINDOW_DAYS = 7        # state units older than this are not "current"
DAY_SECONDS = 86400.0

# unit retrieval
RETRIEVE_DEFAULT_K = 8
# states live in the state block; in_md_slice units live in the portrait — both
# excluded here so relevant-memory retrieval surfaces the mid-tier beliefs.
_RETRIEVE_EXCLUDED_TYPES = ("state",)

# freshness seam (units_and_freshness mode): the most recent raw evidence that
# reconcile has NOT yet folded into units, so "just happened" facts are usable
# immediately instead of waiting for the next reconcile pass.
FRESHNESS_MAX_EVENTS = 6
FRESHNESS_WINDOW_DAYS = 3
FRESHNESS_CHAR_BUDGET = 400


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


@dataclass(frozen=True)
class FreshnessItem:
    content: str
    source_channel: str
    occurred_at: float
    owner_scope: str
    visibility_scope: str
    needs_discretion: bool


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

    scored = sorted(((score(r), r) for r in rows), key=lambda x: x[0], reverse=True)
    # minimum-hit gate: a unit that shares no keyword with the query is not
    # injected as importance-ranked filler — that surfaced off-topic memory and
    # let it crowd out the current subject. The always-on portrait + state block
    # still carry identity. (Full fix: a dedicated unit vector index; keyword
    # overlap is the MVP relevance signal.)
    hits = [r for (s, r) in scored if s[0] > 0]
    return [_row_to_item(r, channel, reply_soul) for r in hits[:k]]


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


def _attribution_for(visibility_scope: str, owner_scope: str, reply_soul: str | None) -> str:
    """A short provenance tag so a soul knows whose public conversation an item
    came from, and never mistakes another soul's comment thread for something
    said to itself. Own private memory is flagged via discretion, not here."""
    if visibility_scope.startswith("thread:"):
        soul = owner_scope[len("soul:"):] if owner_scope.startswith("soul:") else None
        if soul and soul != reply_soul:
            return f"（用户在 {soul} 的评论区）"
        return "（评论区）"
    if visibility_scope == "public":
        return "（公开帖子）"
    return ""


def _attribution(item: MemoryItem, reply_soul: str | None) -> str:
    return _attribution_for(item.visibility_scope, item.owner_scope, reply_soul)


def _vis_admissible(visibility_scope: str, plan: dict) -> bool:
    if visibility_scope == "public" or visibility_scope.startswith("thread:"):
        return bool(plan.get("public"))
    return plan.get("private_self") is not None and visibility_scope == plan["private_self"]


def freshness_seam(
    channel: str,
    reply_soul: str | None,
    *,
    now: float | None = None,
) -> tuple[list[FreshnessItem], bool]:
    """Recent user evidence past each admissible bucket's reconcile cursor — raw
    facts not yet folded into units. Most-recent first, gated by an age window
    and an event + char budget. Returns (items, truncated). Same scope rules as
    unit retrieval: public scene shared, own private admitted with discretion,
    other souls' private never returned."""
    now = db.now_ts() if now is None else now
    cutoff = now - FRESHNESS_WINDOW_DAYS * DAY_SECONDS
    plan = policy.admissible_visibility_filters(channel, reply_soul)

    candidates: list[sqlite3.Row] = []
    for owner_scope, visibility_scope in mes.buckets_with_pending_events():
        if not _vis_admissible(visibility_scope, plan):
            continue
        cursor = mes.get_cursor(owner_scope, visibility_scope)
        for event in mes.list_events_after(
            owner_scope, visibility_scope, cursor, limit=FRESHNESS_MAX_EVENTS * 4
        ):
            if event["author"] != "user":
                continue
            snapshot = str(event["content_snapshot"] or "").strip()
            if not snapshot:
                continue
            if float(event["occurred_at"]) < cutoff:
                continue
            candidates.append(event)

    candidates.sort(key=lambda e: (float(e["occurred_at"]), int(e["id"])), reverse=True)
    truncated = len(candidates) > FRESHNESS_MAX_EVENTS
    items: list[FreshnessItem] = []
    used_chars = 0
    for event in candidates[:FRESHNESS_MAX_EVENTS]:
        snapshot = str(event["content_snapshot"]).strip()
        if items and used_chars + len(snapshot) > FRESHNESS_CHAR_BUDGET:
            truncated = True
            break
        used_chars += len(snapshot)
        vis = str(event["visibility_scope"])
        items.append(FreshnessItem(
            content=snapshot,
            source_channel=str(event["source_channel"]),
            occurred_at=float(event["occurred_at"]),
            owner_scope=str(event["owner_scope"]),
            visibility_scope=vis,
            needs_discretion=_discretion_for(vis, channel, reply_soul),
        ))
    return items, truncated


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


# --- prompt section assembly (Phase 5b) ------------------------------------

_DISCRETION_TAG = "「私密·谨慎」"
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class MemoryPrompt:
    text: str
    used_unit_ids: list[str] = field(default_factory=list)
    has_discretion_items: bool = False


def _portrait_text(owner_scope: str, visibility_scope: str, view_type: str) -> str:
    view = mvs.get_view(owner_scope, visibility_scope, view_type)
    if view is None:
        return ""
    body = _HTML_COMMENT.sub("", str(view["content_md"] or "")).strip()
    return body


def build_memory_section(channel: str, reply_soul: str | None, query: str) -> MemoryPrompt:
    """Assemble the always-on + retrieved memory block for a reply prompt.

    Layers (design §4.5): baseline portrait -> current state -> relevant units,
    plus a precedence/discretion rule line. Private-but-admitted items are
    tagged so the model self-censors before public disclosure; forbidden memory
    never reaches here (filtered upstream by scope policy)."""
    sections: list[str] = []
    used: list[str] = []
    has_discretion = False

    # 1. baseline identity portrait (always-on, query-independent)
    portrait = _portrait_text("global", "public", mvs.VIEW_USER_MD)
    if portrait:
        sections.append(f"[基线认知]\n{portrait}")
    if reply_soul is not None and channel in policy.PRIVATE_CHANNELS:
        soul_portrait = _portrait_text(
            f"soul:{reply_soul}", f"private:soul:{reply_soul}", mvs.VIEW_SOUL_PRIVATE
        )
        if soul_portrait:
            sections.append(f"[{reply_soul}·私聊画像]\n{soul_portrait}")

    # 2. current-state block (always-on)
    state_items = recent_state_block(channel, reply_soul)
    if state_items:
        lines = []
        for item in state_items:
            tag = f" {_DISCRETION_TAG}" if item.needs_discretion else ""
            has_discretion = has_discretion or item.needs_discretion
            lines.append(f"- 近期：{item.content}{tag}")
            used.append(item.unit_id)
        sections.append("[当前状态]\n" + "\n".join(lines))

    # 3. query-relevant beliefs
    hits = retrieve_units(query, channel, reply_soul)
    if hits:
        lines = []
        for item in hits:
            tag = f" {_DISCRETION_TAG}" if item.needs_discretion else ""
            has_discretion = has_discretion or item.needs_discretion
            attribution = _attribution(item, reply_soul)
            attr = f" {attribution}" if attribution else ""
            lines.append(f"- [{item.type}|置信{item.confidence:.1f}] {item.content}{attr}{tag}")
            used.append(item.unit_id)
        sections.append("[相关记忆]\n" + "\n".join(lines))

    # 4. freshness seam (units_and_freshness only): recent raw evidence not yet
    #    reconciled into units, so just-happened facts are available immediately.
    if read_mode() == "units_and_freshness":
        fresh_items, truncated = freshness_seam(channel, reply_soul)
        if fresh_items:
            lines = []
            for fitem in fresh_items:
                tag = f" {_DISCRETION_TAG}" if fitem.needs_discretion else ""
                has_discretion = has_discretion or fitem.needs_discretion
                attribution = _attribution_for(fitem.visibility_scope, fitem.owner_scope, reply_soul)
                attr = f"{attribution} " if attribution else ""
                lines.append(f"- {attr}{fitem.content}{tag}")
            note = "（仅最近部分）" if truncated else ""
            sections.append(f"[最近动态·尚未整理]{note}\n" + "\n".join(lines))

    if not sections:
        return MemoryPrompt(text="", used_unit_ids=[], has_discretion_items=False)

    rules = [
        "[记忆使用规则]",
        "讲事实/细节以最新动态为准；讲框架、倾向、关系用上述记忆，低置信的软着说。",
    ]
    if has_discretion:
        rules.append(
            f"标记 {_DISCRETION_TAG} 的是只在私聊得知的内容：可参考，但公开场合需自行判断是否合适说出，默认不要主动透露。"
        )
    sections.append("\n".join(rules))

    return MemoryPrompt(
        text="\n\n".join(sections),
        used_unit_ids=used,
        has_discretion_items=has_discretion,
    )


def list_goals(
    owner_scope: str = "global",
    visibility_scope: str = "public",
) -> list[MemoryItem]:
    """Active goal units in a bucket, importance-ranked — the user's currently
    tracked goals, for display / the workbench.

    Goal lifecycle rides the unit status machine rather than a dedicated column:
    a goal that is achieved or abandoned is retracted by a later reconcile pass
    (status != 'active'), so 'active goal units' are exactly the goals still in
    play. An explicit achieved-vs-abandoned distinction would need a schema
    column and is deferred."""
    rows = db.query_all(
        """
        SELECT id, type, content, confidence, importance, owner_scope, visibility_scope,
               last_confirmed
        FROM memory_units
        WHERE type = 'goal' AND status = 'active'
          AND owner_scope = ? AND visibility_scope = ?
        ORDER BY importance DESC, last_confirmed DESC
        """,
        (owner_scope, visibility_scope),
    )
    return [_row_to_item(r, "public_post", None) for r in rows]


# --- evidence hydration (unit -> raw evidence, for the workbench) ----------

@dataclass(frozen=True)
class EvidenceRef:
    event_id: int
    source_channel: str
    source_type: str
    source_id: str
    content: str
    occurred_at: float
    author: str | None


@dataclass(frozen=True)
class UnitDetail:
    unit_id: str
    type: str
    content: str
    confidence: float
    importance: float
    tier: str
    status: str
    owner_scope: str
    visibility_scope: str
    in_md_slice: bool
    evidence: list[EvidenceRef]


def unit_detail(unit_id: str) -> UnitDetail | None:
    """One unit plus the raw evidence events it was derived from — the bottom
    layer of the workbench's portrait -> unit -> evidence drill-down, and the
    'why does the system believe this' explanation. Returns None if no such
    unit."""
    row = mus.get_unit(unit_id)
    if row is None:
        return None
    evidence = [
        EvidenceRef(
            event_id=int(e["id"]),
            source_channel=str(e["source_channel"]),
            source_type=str(e["source_type"]),
            source_id=str(e["source_id"]),
            content=str(e["content_snapshot"] or ""),
            occurred_at=float(e["occurred_at"]),
            author=e["author"],
        )
        for e in mus.get_unit_evidence(unit_id)
    ]
    return UnitDetail(
        unit_id=row["id"],
        type=row["type"],
        content=row["content"],
        confidence=float(row["confidence"]),
        importance=float(row["importance"]),
        tier=row["tier"],
        status=row["status"],
        owner_scope=row["owner_scope"],
        visibility_scope=row["visibility_scope"],
        in_md_slice=bool(row["in_md_slice"]),
        evidence=evidence,
    )
