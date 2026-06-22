"""Scope-filtered memory retrieval and prompt assembly.

The reply paths use these read functions:

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

import re
import sqlite3
from dataclasses import dataclass, field

from core import (
    db,
    goal_service,
    memory_events_service as mes,
    memory_scope_policy as policy,
    memory_unit_service as mus,
    memory_view_service as mvs,
    soul_relationship_memory as srm,
)

def memory_section_for(
    channel: str,
    reply_soul: str | None,
    query: str,
    *,
    excluded_sources: set[tuple[str, str]] | None = None,
) -> str:
    """Single entry used by every reply path for memory-v2 prompt assembly."""
    return build_memory_section(
        channel,
        reply_soul,
        query,
        excluded_sources=excluded_sources,
    ).text


def relationship_memory_for(reply_soul: str | None) -> str:
    """Current SOUL's complete relationship narrative for its system prompt."""
    if reply_soul is None:
        return ""
    body = srm.read_relationship_memory(reply_soul).strip()
    if not body:
        return ""
    return f"{body}\n\n[相处记忆使用规则]\n{srm.PUBLIC_USE_RULE}"

# current-state block
STATE_BLOCK_LIMIT = 5
STATE_WINDOW_DAYS = 7        # state units older than this are not "current"
DAY_SECONDS = 86400.0

# unit retrieval
RETRIEVE_DEFAULT_K = 8
# states live in the state block; portrait members live in the portrait — both
# excluded here so relevant-memory retrieval surfaces the mid-tier beliefs.
_RETRIEVE_EXCLUDED_TYPES = ("state",)

# freshness seam (units_and_freshness mode): the most recent raw evidence that
# reconcile has NOT yet folded into units, so "just happened" facts are usable
# immediately instead of waiting for the next reconcile pass.
FRESHNESS_MAX_EVENTS = 6
FRESHNESS_WINDOW_DAYS = 3
FRESHNESS_CHAR_BUDGET = 400

# relevant-memory raw recall: a hit unit anchors a topic; we then recall the
# FULL conversation around its most-relevant evidence — original post + the reply
# soul's own comment line + the hit soul's comment line; for a private-chat hit,
# the recent chat tail. Targets are deduped across hits and rendered under a
# generous budget with message-boundary (smart) truncation. The most-relevant
# evidence is chosen within the unit's own evidence by keyword overlap then
# recency — no vector here (that already happened at the unit level).
RECALL_CHAR_BUDGET = 1200
RECALL_MSG_MAX_CHARS = 200
RECALL_THREAD_EVENT_LIMIT = 50
RECALL_CHAT_TAIL = 8


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
    reviewing: bool = False


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
    (state block) and portrait members. MVP scoring: keyword
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
          AND in_portrait = 0
          AND type NOT IN ({type_placeholders})
          AND {vis_sql}
        """,
        (*_RETRIEVE_EXCLUDED_TYPES, *params),
    )

    terms = _tokenize(query)
    now = db.now_ts()
    semantic = _semantic_unit_ranks(query)

    def score(r: sqlite3.Row) -> tuple:
        overlap = _keyword_overlap(str(r["content"]), terms)
        sem_rank = semantic.get(str(r["id"]))
        sem_score = (1.0 / (1 + sem_rank)) if sem_rank is not None else 0.0
        combined = overlap + 2.0 * sem_score
        return (combined, float(r["importance"]), _recency_weight(r["last_confirmed"], now))

    # relevance gate: keep units matching on keyword OR semantic similarity.
    # Semantic recall catches paraphrases keyword overlap misses; keyword catches
    # exact terms the embedding blurs. Units matching neither are not injected as
    # importance-ranked filler. Scope/discretion is still enforced by the SQL
    # candidate set above, so ANN results can never widen visibility.
    kept = [
        r for r in rows
        if _keyword_overlap(str(r["content"]), terms) > 0 or str(r["id"]) in semantic
    ]
    kept = [
        r for r in kept
        if not goal_service.memory_content_duplicates_active_goal(str(r["content"]))
    ]
    ranked = sorted(kept, key=score, reverse=True)
    return [_row_to_item(r, channel, reply_soul) for r in ranked[:k]]


def _semantic_unit_ranks(query: str) -> dict[str, int]:
    """unit_id -> ANN rank for the query, from the unit vector docs. Empty when
    the query is blank or the vector index is unavailable / not query-ready, in
    which case retrieval degrades to keyword-only. Scope is NOT applied here; the
    caller intersects these with its scope-filtered SQL candidates."""
    if not str(query or "").strip():
        return {}
    try:
        from core import vectorstore
        hits = vectorstore.query_documents(query, n_results=RETRIEVE_DEFAULT_K * 3, where={"type": "unit"})
    except Exception:
        return {}
    ranks: dict[str, int] = {}
    for hit in hits:
        meta = getattr(hit, "metadata", None) or {}
        uid = meta.get("unit_id")
        if not uid:
            doc_id = str(getattr(hit, "doc_id", ""))
            if doc_id.startswith("unit-"):
                uid = doc_id[len("unit-"):]
        if uid:
            ranks.setdefault(str(uid), int(getattr(hit, "rank", len(ranks) + 1)))
    return ranks


def _top_evidence_row(unit_id: str, terms: list[str]) -> sqlite3.Row | None:
    """The most relevant user-authored evidence row backing a unit, ranked by
    keyword overlap then recency within the unit's own (semantically homogeneous)
    evidence — no vector needed. Used to locate the conversation to recall."""
    best: sqlite3.Row | None = None
    best_key: tuple | None = None
    for row in mus.current_effective_evidence_for_unit(unit_id):
        if row["author"] not in (None, "user"):
            continue
        snapshot = str(row["content_snapshot"] or "").strip()
        if not snapshot:
            continue
        key = (_keyword_overlap(snapshot, terms), float(row["occurred_at"]))
        if best_key is None or key > best_key:
            best_key = key
            best = row
    return best


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _format_dialogue(events: list[sqlite3.Row], soul_label: str) -> list[str]:
    lines: list[str] = []
    for event in mes.collapse_to_current_events(events):
        snapshot = str(event["content_snapshot"] or "").strip()
        if not snapshot:
            continue
        speaker = "用户" if event["author"] in (None, "user") else soul_label
        lines.append(f"{speaker}：{_clip(snapshot, RECALL_MSG_MAX_CHARS)}")
    return lines


def _recall_post_block(post_id: str, hit_souls: list[str], reply_soul: str | None) -> str:
    """Original post + the reply soul's own comment line + each hit soul's line."""
    parts: list[str] = []
    snapshot = mes.latest_post_snapshot(post_id)
    if snapshot:
        parts.append(f"〈帖子 {post_id}〉{_clip(snapshot, RECALL_MSG_MAX_CHARS)}")
    souls: list[str] = []
    if reply_soul:
        souls.append(reply_soul)
    for soul in hit_souls:
        if soul and soul not in souls:
            souls.append(soul)
    for soul in souls:
        events = mes.list_current_events_in_bucket(
            f"soul:{soul}",
            f"thread:{post_id}",
            limit=RECALL_THREAD_EVENT_LIMIT,
        )
        dialogue = _format_dialogue(events, soul)
        if dialogue:
            parts.append(f"  与{soul}：\n" + "\n".join(f"    {line}" for line in dialogue))
    return "\n".join(parts) if parts else ""


def _recall_chat_block(soul: str, *, discreet: bool) -> str:
    events = mes.list_current_events_in_bucket(
        f"soul:{soul}",
        f"private:soul:{soul}",
        limit=200,
    )
    dialogue = _format_dialogue(events[-RECALL_CHAT_TAIL:], soul)
    if not dialogue:
        return ""
    header = f"〈与{soul}的私聊〉"
    if discreet:
        header += f" {_DISCRETION_TAG}（私聊内容，公开场合自行判断是否提及）"
    return header + "\n" + "\n".join(f"  {line}" for line in dialogue)


def _recall_conversations(hits: list[MemoryItem], channel: str, reply_soul: str | None, terms: list[str]) -> str:
    """Recall the full conversation(s) around the hit units' most-relevant
    evidence, deduped across hits, budgeted with smart truncation. Public posts
    and comment threads are public-scene (cross-soul readable); a private chat is
    only recalled for the reply soul itself and discretion-flagged in public."""
    post_hit_souls: dict[str, list[str]] = {}
    chat_souls: list[str] = []
    order: list[tuple[str, str]] = []
    for item in hits:
        ev = _top_evidence_row(item.unit_id, terms)
        if ev is None:
            continue
        vis = str(ev["visibility_scope"])
        owner = str(ev["owner_scope"])
        if vis == "public":
            pid = str(ev["source_id"])
            if pid not in post_hit_souls:
                post_hit_souls[pid] = []
                order.append(("post", pid))
        elif vis.startswith("thread:"):
            pid = vis[len("thread:"):]
            soul = owner[len("soul:"):] if owner.startswith("soul:") else None
            if pid not in post_hit_souls:
                post_hit_souls[pid] = []
                order.append(("post", pid))
            if soul and soul not in post_hit_souls[pid]:
                post_hit_souls[pid].append(soul)
        elif vis.startswith("private:soul:"):
            soul = vis[len("private:soul:"):]
            # retrieve already restricts private hits to the reply soul; guard.
            if reply_soul and soul == reply_soul and soul not in chat_souls:
                chat_souls.append(soul)
                order.append(("chat", soul))

    if not order:
        return ""
    discreet = channel in policy.PUBLIC_CHANNELS
    blocks: list[str] = []
    used = 0
    truncated = False
    for kind, key in order:
        if used >= RECALL_CHAR_BUDGET:
            truncated = True
            break
        block = (
            _recall_post_block(key, post_hit_souls[key], reply_soul)
            if kind == "post"
            else _recall_chat_block(key, discreet=discreet)
        )
        if not block:
            continue
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    text = "[相关对话原文]\n" + "\n\n".join(blocks)
    if truncated:
        text += "\n（更多相关对话已省略）"
    return text


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
    query: str = "",
    excluded_sources: set[tuple[str, str]] | None = None,
) -> tuple[list[FreshnessItem], bool]:
    """Recent user evidence past each admissible bucket's reconcile cursor — raw
    facts not yet folded into units. Most-recent first, gated by an age window
    and an event + char budget. Returns (items, truncated). Same scope rules as
    unit retrieval: public scene shared, own private admitted with discretion,
    other souls' private never returned."""
    now = db.now_ts() if now is None else now
    excluded_sources = excluded_sources or set()
    cutoff = now - FRESHNESS_WINDOW_DAYS * DAY_SECONDS
    plan = policy.admissible_visibility_filters(channel, reply_soul)

    candidates: dict[int, tuple[sqlite3.Row, bool]] = {}
    for owner_scope, visibility_scope in mes.buckets_with_pending_events():
        if not _vis_admissible(visibility_scope, plan):
            continue
        cursor = mes.get_cursor(owner_scope, visibility_scope)
        pending_events = mes.list_events_after(
            owner_scope, visibility_scope, cursor, limit=FRESHNESS_MAX_EVENTS * 4
        )
        for event in mes.collapse_to_current_events(pending_events):
            if (
                str(event["source_type"]),
                str(event["source_id"]),
            ) in excluded_sources:
                continue
            if event["author"] != "user":
                continue
            snapshot = str(event["content_snapshot"] or "").strip()
            if not snapshot:
                continue
            if float(event["occurred_at"]) < cutoff:
                continue
            candidates[int(event["id"])] = (event, False)

        for review in mus.list_pending_reviews(owner_scope, visibility_scope):
            unit_id = str(review["unit_id"])
            review_events = mus.current_effective_evidence_for_unit(unit_id)
            trigger_current = mes.current_effective_event(
                str(review["source_type"]),
                str(review["source_id"]),
            )
            if trigger_current is not None:
                review_events = [*review_events, trigger_current]
            for event in review_events:
                if (
                    str(event["source_type"]),
                    str(event["source_id"]),
                ) in excluded_sources:
                    continue
                if event["author"] != "user":
                    continue
                if not str(event["content_snapshot"] or "").strip():
                    continue
                candidates[int(event["id"])] = (event, True)

    terms = _tokenize(query)
    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            _keyword_overlap(str(item[0]["content_snapshot"] or ""), terms),
            1 if item[1] else 0,
            float(item[0]["occurred_at"]),
            int(item[0]["id"]),
        ),
        reverse=True,
    )
    truncated = len(ordered) > FRESHNESS_MAX_EVENTS
    items: list[FreshnessItem] = []
    used_chars = 0
    for event, reviewing in ordered[:FRESHNESS_MAX_EVENTS]:
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
            reviewing=reviewing,
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


# --- prompt section assembly -----------------------------------------------

_DISCRETION_TAG = "「私密·谨慎」"
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class MemoryPrompt:
    text: str
    used_unit_ids: list[str] = field(default_factory=list)
    has_discretion_items: bool = False


def _portrait_text(owner_scope: str, visibility_scope: str, view_type: str) -> str:
    return mvs.read_portrait_body(owner_scope, visibility_scope, view_type)


def build_memory_section(
    channel: str,
    reply_soul: str | None,
    query: str,
    *,
    excluded_sources: set[tuple[str, str]] | None = None,
) -> MemoryPrompt:
    """Assemble the always-on + retrieved memory block for a reply prompt.

    Layers (design §4.5): baseline portrait -> current state -> relevant units,
    plus a precedence/discretion rule line. Private-but-admitted items are
    tagged so the model self-censors before public disclosure; forbidden memory
    never reaches here (filtered upstream by scope policy)."""
    sections: list[str] = []
    used: list[str] = []
    has_discretion = False

    # 1. baseline identity portrait (always-on, query-independent)
    portrait = _portrait_text("global", "public", mvs.VIEW_USER_PORTRAIT)
    if portrait:
        sections.append(f"[基线认知]\n{portrait}")
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

    # 3. query-relevant beliefs (de-noised topic anchors)
    hits = retrieve_units(query, channel, reply_soul)
    terms = _tokenize(query)
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

        # 3.5 faithful raw recall: the full conversation(s) around the hit units'
        #     most-relevant evidence (original post + relevant comment lines, or
        #     chat tail), deduped across hits, budgeted with smart truncation.
        recall = _recall_conversations(hits, channel, reply_soul, terms)
        if recall:
            sections.append(recall)

    # 4. freshness seam: recent raw evidence not yet reconciled into units, so
    # just-happened facts are available immediately.
    fresh_items, truncated = freshness_seam(
        channel,
        reply_soul,
        query=query,
        excluded_sources=excluded_sources,
    )
    if fresh_items:
        lines = []
        for fitem in fresh_items:
            tag = f" {_DISCRETION_TAG}" if fitem.needs_discretion else ""
            has_discretion = has_discretion or fitem.needs_discretion
            attribution = _attribution_for(fitem.visibility_scope, fitem.owner_scope, reply_soul)
            attr = f"{attribution} " if attribution else ""
            state = "（unit 重判中）" if fitem.reviewing else "（新近未整理）"
            lines.append(f"- {state}{attr}{fitem.content}{tag}")
        note = "（仅最近部分）" if truncated else ""
        sections.append(f"[尚未稳定沉淀的原始证据]{note}\n" + "\n".join(lines))

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


def list_goals() -> list[dict]:
    """Read active goals from GoalTool, the sole truth source."""
    return goal_service.list_goals(status="active")


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
    state: str
    review_pending: bool = False


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
    source: str
    source_channel: str
    in_portrait: bool
    prompt_policy: str
    portrait_policy: str
    evidence: list[EvidenceRef]


def unit_detail(unit_id: str) -> UnitDetail | None:
    """One unit plus the raw evidence events it was derived from — the bottom
    layer of the workbench's portrait -> unit -> evidence drill-down, and the
    'why does the system believe this' explanation. Returns None if no such
    unit."""
    row = mus.get_unit(unit_id)
    if row is None:
        return None
    evidence = []
    for e in mus.get_unit_evidence(unit_id):
        latest = mes.latest_source_event(str(e["source_type"]), str(e["source_id"]))
        state = "superseded"
        if latest is not None and latest["op"] == "delete":
            state = "deleted"
        elif latest is not None and int(latest["id"]) == int(e["id"]):
            state = "current"
        evidence.append(
            EvidenceRef(
                event_id=int(e["id"]),
                source_channel=str(e["source_channel"]),
                source_type=str(e["source_type"]),
                source_id=str(e["source_id"]),
                content=str(e["content_snapshot"] or ""),
                occurred_at=float(e["occurred_at"]),
                author=e["author"],
                state=state,
                review_pending=bool(e["review_pending"]),
            )
        )
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
        source=row["source"],
        source_channel=row["source_channel"],
        in_portrait=bool(row["in_portrait"]),
        prompt_policy=row["prompt_policy"],
        portrait_policy=row["portrait_policy"],
        evidence=evidence,
    )
