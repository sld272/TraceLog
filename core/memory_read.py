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
    fts_query,
    goal_service,
    logging_service,
    memory_events_service as mes,
    memory_scope_policy as policy,
    memory_unit_service as mus,
    memory_view_service as mvs,
    soul_relationship_memory as srm,
)

@dataclass(frozen=True)
class MemorySection:
    """Assembled memory-v2 block plus the citations it leaned on."""

    text: str
    cited_memory: list[dict] = field(default_factory=list)


def memory_section_with_citations(
    channel: str,
    reply_soul: str | None,
    query: str,
    *,
    excluded_sources: set[tuple[str, str]] | None = None,
    semantic_query: str | None = None,
    keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> MemorySection:
    """Single entry used by every reply path for memory-v2 prompt assembly.

    Returns both the prompt text and the cited belief units + raw freshness so
    every reply path (post first-reply, comment, chat) surfaces the same 引用记忆
    panel from one code path. ``semantic_query``/``keywords`` are the query-rewrite
    outputs steering unit retrieval; absent them retrieval falls back to the raw
    query."""
    prompt = build_memory_section(
        channel,
        reply_soul,
        query,
        excluded_sources=excluded_sources,
        semantic_query=semantic_query,
        keywords=keywords,
        trace_context=trace_context,
    )
    return MemorySection(
        text=prompt.text,
        cited_memory=cited_memory(prompt.used_unit_ids, prompt.used_freshness),
    )


def memory_section_for(
    channel: str,
    reply_soul: str | None,
    query: str,
    *,
    excluded_sources: set[tuple[str, str]] | None = None,
    semantic_query: str | None = None,
    keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> str:
    """Text-only memory block (no citations). Kept for the first-reply path."""
    return memory_section_with_citations(
        channel,
        reply_soul,
        query,
        excluded_sources=excluded_sources,
        semantic_query=semantic_query,
        keywords=keywords,
        trace_context=trace_context,
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
# Cosine-similarity floor for a semantic (ANN) hit to count. Chroma always returns
# the k nearest neighbours even when the nearest is barely related; without a floor
# every one passes the relevance gate and fills [相关记忆] with noise. similarity =
# 1 - cosine_distance; 0.30 (distance <= 0.70) is conservative — it drops clearly
# unrelated units while keeping paraphrase recall. Tune against real distances.
SEMANTIC_SIM_FLOOR = 0.30
# Weight on the semantic similarity vs the FTS rank score in the blended unit
# ranking. Coupled with SEMANTIC_SIM_FLOOR (which sets the floor of sem_score's
# contribution); tune the pair together, not in isolation.
SEMANTIC_RANK_WEIGHT = 2.0
# states live in the state block; portrait members live in the portrait — both
# excluded here so relevant-memory retrieval surfaces the mid-tier beliefs.
# relationship units are owner-scoped to one persona and are injected ONLY through
# that persona's relationship narrative; excluding them here keeps a (soul:X,public)
# relationship belief from leaking into another soul's cross-persona retrieval
# (owner becomes the access boundary even though visibility is public).
_RETRIEVE_EXCLUDED_TYPES = ("state", "relationship")

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
    # source_type/source_id let attribution key on provenance (post vs which
    # soul's comment area) instead of the now-flattened visibility_scope.
    source_type: str = ""
    source_id: str = ""


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
    semantic_query: str | None = None,
    keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> list[MemoryItem]:
    """Query-relevant beliefs in the admissible scopes, excluding state units
    (state block) and portrait members.

    Two parallel recall channels, intersected with a scope-filtered candidate set:
    FTS5 over unit content (``keywords`` from query-rewrite, else the raw query)
    and semantic ANN over the unit vector index (``semantic_query`` from rewrite,
    else the raw query). A unit is kept iff it matches EITHER channel; scored by a
    blend of both ranks, then importance, then recency. Scope/discretion is
    enforced by the SQL candidate set, so neither channel can widen visibility."""
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

    now = db.now_ts()
    sem_hits = _semantic_unit_hits(semantic_query or query)
    semantic = {h.unit_id: h.sim for h in sem_hits if h.passed}
    fts = _fts_unit_ranks(query, keywords)

    scored: dict[str, tuple] = {}

    def score(r: sqlite3.Row) -> tuple:
        rid = str(r["id"])
        fts_rank = fts.get(rid)
        fts_score = (1.0 / (1 + fts_rank)) if fts_rank is not None else 0.0
        sem_score = semantic.get(rid, 0.0)  # cosine similarity, already floored
        combined = fts_score + SEMANTIC_RANK_WEIGHT * sem_score
        ranking = (combined, float(r["importance"]), _recency_weight(r["last_confirmed"], now))
        scored[rid] = ranking
        return ranking

    # relevance gate: keep units matching the FTS keyword channel OR the semantic
    # channel. Semantic recall catches paraphrases keyword search misses; FTS
    # catches exact terms the embedding blurs. Units matching neither are not
    # injected as importance-ranked filler.
    kept = [r for r in rows if str(r["id"]) in fts or str(r["id"]) in semantic]
    kept = [
        r for r in kept
        if not goal_service.memory_content_duplicates_active_goal(str(r["content"]))
    ]
    ranked = sorted(kept, key=score, reverse=True)

    if logging_service.is_enabled_for("DEBUG"):
        _log_retrieval(
            channel, reply_soul, query, semantic_query, keywords,
            n_candidates=len(rows), fts=fts, semantic=semantic, sem_hits=sem_hits,
            ranked=ranked, scored=scored, k=k, trace_context=trace_context,
        )
    return [_row_to_item(r, channel, reply_soul) for r in ranked[:k]]


def _log_retrieval(
    channel: str,
    reply_soul: str | None,
    query: str,
    semantic_query: str | None,
    keywords: list[str] | None,
    *,
    n_candidates: int,
    fts: dict[str, int],
    semantic: dict[str, float],
    sem_hits: list[SemanticHit],
    ranked: list[sqlite3.Row],
    scored: dict[str, tuple],
    k: int,
    trace_context: dict | None = None,
) -> None:
    """Emit one structured 'memory_retrieval' DEBUG event tracing the full unit
    scoring: per-unit FTS rank, semantic similarity, which channel matched, the
    blended score, what cleared the relevance gate, what the top-k cut dropped,
    and the sub-floor ANN neighbours. Lets real-run recall quality and the
    SEMANTIC_SIM_FLOOR / SEMANTIC_RANK_WEIGHT pair be audited and tuned offline.
    DEBUG-gated, so it is silent under the default INFO level."""
    top_ids = {str(r["id"]) for r in ranked[:k]}
    units = []
    for r in ranked:
        rid = str(r["id"])
        in_fts = rid in fts
        in_sem = rid in semantic
        units.append(
            {
                "unit_id": rid,
                "type": str(r["type"]),
                "via": "both" if (in_fts and in_sem) else "fts" if in_fts else "semantic",
                "fts_rank": fts.get(rid),
                "sem_sim": round(semantic[rid], 4) if in_sem else None,
                "score": round(float(scored.get(rid, (0.0,))[0]), 4),
                "in_top_k": rid in top_ids,
            }
        )
    # ANN neighbours the floor rejected (related-but-cut) — the key tuning sample.
    floored = [
        {"unit_id": h.unit_id, "sim": round(h.sim, 4)}
        for h in sem_hits
        if not h.passed
    ]
    logging_service.log_event(
        "memory_retrieval",
        level="DEBUG",
        channel=channel,
        reply_soul=reply_soul,
        raw_query=query,
        semantic_query=semantic_query,
        keywords=keywords,
        sim_floor=SEMANTIC_SIM_FLOOR,
        sem_weight=SEMANTIC_RANK_WEIGHT,
        n_candidates=n_candidates,
        n_fts_hits=len(fts),
        n_semantic_neighbors=len(sem_hits),
        k=k,
        units=units,
        floored_neighbors=floored,
        trace=trace_context or {},
    )


@dataclass(frozen=True)
class SemanticHit:
    """One ANN neighbour of the query over the unit vector index, in ANN order."""
    unit_id: str
    sim: float           # cosine similarity = 1 - distance (ANN-order proxy if missing)
    passed: bool         # cleared SEMANTIC_SIM_FLOOR (or kept fail-open)
    distance_missing: bool


def _semantic_unit_hits(query: str) -> list[SemanticHit]:
    """All ANN neighbours for the query over the unit vector index, in ANN order,
    each tagged with cosine similarity (1 - distance) and whether it cleared
    SEMANTIC_SIM_FLOOR. Sub-floor neighbours are RETAINED here (passed=False) so
    callers can audit/log the rejected-but-near band for tuning — the floor is
    applied by _semantic_unit_sims, not here. A hit with no distance (rare Chroma
    fallback) is kept fail-open (passed=True, distance_missing=True) with an
    ANN-order proxy sim. Empty when the query is blank or the index is
    unavailable / not query-ready. Scope is NOT applied here; the caller
    intersects these with its scope-filtered SQL candidates."""
    if not str(query or "").strip():
        return []
    try:
        from core import vectorstore
        hits = vectorstore.query_documents(query, n_results=RETRIEVE_DEFAULT_K * 3, where={"type": "unit"})
    except Exception:
        return []
    out: list[SemanticHit] = []
    seen: set[str] = set()
    for hit in hits:
        meta = getattr(hit, "metadata", None) or {}
        uid = meta.get("unit_id")
        if not uid:
            doc_id = str(getattr(hit, "doc_id", ""))
            if doc_id.startswith("unit-"):
                uid = doc_id[len("unit-"):]
        if not uid:
            continue
        uid = str(uid)
        if uid in seen:  # keep the nearest doc for a unit (ANN order), drop the rest
            continue
        seen.add(uid)
        distance = getattr(hit, "distance", None)
        if distance is None:
            sim = 1.0 / (1 + int(getattr(hit, "rank", len(out) + 1)))
            out.append(SemanticHit(uid, sim, passed=True, distance_missing=True))
        else:
            sim = 1.0 - float(distance)
            out.append(SemanticHit(uid, sim, passed=sim >= SEMANTIC_SIM_FLOOR, distance_missing=False))
    return out


def _semantic_unit_sims(query: str) -> dict[str, float]:
    """unit_id -> cosine similarity for the query's ANN neighbours at or above
    SEMANTIC_SIM_FLOOR (fail-open hits with no distance kept too). Empty when the
    query is blank or the vector index is unavailable, in which case retrieval
    degrades to FTS-only. Scope is NOT applied here; the caller intersects these
    with its scope-filtered SQL candidates."""
    return {h.unit_id: h.sim for h in _semantic_unit_hits(query) if h.passed}


def _is_short_cjk(term: str) -> bool:
    """A CJK term under 3 chars: the trigram tokenizer produces no token for it, so
    it can never MATCH and must be served by LIKE instead (2-char words like 考研/
    安静 are extremely common)."""
    return fts_query.has_cjk(term) and len(term.replace(" ", "")) < 3


def _fts_unit_ranks(query: str, keywords: list[str] | None = None) -> dict[str, int]:
    """unit_id -> rank for the rewrite ``keywords`` (preferred) or the raw query,
    over the unit FTS index. Empty when nothing matches. Scope is NOT applied here;
    the caller intersects these with its scope-filtered SQL candidates."""
    keywords = keywords or []
    if keywords:
        # Expand each keyword through match_candidates, same as the raw path: a
        # keyword that packs several 2-char CJK words ("考研 规划") must be split so
        # those short words reach the LIKE fallback, else the whole phrase goes to
        # trigram MATCH (which tokenizes nothing under 3 chars) and recalls nothing.
        terms: list[str] = []
        for keyword in keywords:
            clean_kw = fts_query.sanitize_fts5(keyword)
            terms.extend(fts_query.match_candidates(clean_kw) or ([clean_kw] if clean_kw else []))
        return _fts_ranks_for_terms(fts_query.ordered_unique(terms)) if terms else {}
    clean = fts_query.sanitize_fts5(query)
    if not clean:
        return {}
    # match_candidates yields trigram windows + word terms (incl. short CJK ones).
    # A single CJK char yields none, so fall back to the whole query (LIKE below).
    return _fts_ranks_for_terms(fts_query.match_candidates(clean) or [clean])


def _fts_ranks_for_terms(terms: list[str]) -> dict[str, int]:
    """Split terms into trigram-tokenizable (CJK ≥3 / ASCII) and short CJK (<3,
    which the trigram tokenizer cannot index) — MATCH the former in one OR query,
    LIKE each of the latter so common 2-char Chinese words ("考研", "规划") are not
    lost even when they arrive embedded in a longer query. Union, with FTS hits
    ranked ahead of LIKE-only hits."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(ids: list[str]) -> None:
        for uid in ids:
            if uid not in seen:
                seen.add(uid)
                ordered.append(uid)

    tokenizable = [t for t in terms if not _is_short_cjk(t)]
    if tokenizable:
        add(_units_matching_fts(
            fts_query.quote_match_candidates(tokenizable),
            cjk=any(fts_query.has_cjk(t) for t in tokenizable),
        ))
    for term in terms:
        if _is_short_cjk(term):
            add(_units_matching_like(term))
    return {uid: index + 1 for index, uid in enumerate(ordered)}


def _units_matching_fts(match: str, *, cjk: bool) -> list[str]:
    """Unit ids matching an FTS5 MATCH expression, best-rank first. Trigram table
    for CJK, default tokenizer otherwise."""
    table = "memory_units_fts_trigram" if cjk else "memory_units_fts"
    try:
        rows = db.query_all(
            f"""
            SELECT u.id AS id
            FROM {table}
            JOIN memory_units u ON u.rowid = {table}.rowid
            WHERE {table} MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, RETRIEVE_DEFAULT_K * 3),
        )
    except sqlite3.Error:
        return []
    return [str(row["id"]) for row in rows]


def _units_matching_like(term: str) -> list[str]:
    """Short-CJK fallback: match the compact term as a LIKE substring against unit
    content, recency-ordered."""
    pattern = f"%{term.replace(' ', '')}%"
    rows = db.query_all(
        """
        SELECT id
        FROM memory_units
        WHERE content LIKE ?
        ORDER BY last_confirmed DESC, id DESC
        LIMIT ?
        """,
        (pattern, RETRIEVE_DEFAULT_K * 3),
    )
    return [str(row["id"]) for row in rows]


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


def _comment_target(comment_id: str) -> sqlite3.Row | None:
    """Resolve a comment_message/comment_relationship source_id to its post and
    soul. Comment evidence lives in the flat (global|soul, public) buckets, so the
    only way back to the originating post/thread is the comments table itself."""
    return db.query_one(
        "SELECT post_id, soul_name FROM comments WHERE id = ?",
        (int(comment_id),),
    )


def _comment_thread_lines(post_id: str, soul: str) -> list[str]:
    """One soul's comment exchange under a post, read straight from the comments
    table (the live store) rather than the memory event buckets, which are no
    longer thread-scoped."""
    rows = db.query_all(
        """
        SELECT role, content
        FROM comments
        WHERE post_id = ? AND soul_name = ?
        ORDER BY seq ASC, id ASC
        LIMIT ?
        """,
        (post_id, soul, RECALL_THREAD_EVENT_LIMIT),
    )
    lines: list[str] = []
    for row in rows:
        content = str(row["content"] or "").strip()
        if not content:
            continue
        speaker = "用户" if row["role"] == "user" else soul
        lines.append(f"{speaker}：{_clip(content, RECALL_MSG_MAX_CHARS)}")
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
        dialogue = _comment_thread_lines(post_id, soul)
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


def _recall_conversations(
    hits: list[MemoryItem],
    channel: str,
    reply_soul: str | None,
    terms: list[str],
    *,
    trace_context: dict | None = None,
) -> str:
    """Recall the full conversation(s) around the hit units' most-relevant
    evidence, deduped across hits, budgeted with smart truncation. Public posts
    and comment threads are public-scene (cross-soul readable); a private chat is
    only recalled for the reply soul itself and discretion-flagged in public."""
    post_hit_souls: dict[str, list[str]] = {}
    chat_souls: list[str] = []
    order: list[tuple[str, str]] = []
    links: list[dict] = []
    for item in hits:
        ev = _top_evidence_row(item.unit_id, terms)
        if ev is None:
            continue
        source_type = str(ev["source_type"])
        owner = str(ev["owner_scope"])
        if source_type in ("post", "post_vision"):
            # Post evidence: source_id is the post id.
            pid = str(ev["source_id"])
            links.append({"unit_id": str(item.unit_id), "via": "post", "post_id": pid})
            if pid not in post_hit_souls:
                post_hit_souls[pid] = []
                order.append(("post", pid))
        elif source_type in mes.COMMENT_SOURCE_TYPES:
            # Comment evidence: source_id is a comment id; resolve its post+soul so
            # the recall link records BOTH the comment and the post it hangs under.
            target = _comment_target(str(ev["source_id"]))
            if target is None:
                continue
            pid = str(target["post_id"])
            soul = str(target["soul_name"])
            links.append({
                "unit_id": str(item.unit_id),
                "via": "comment",
                "comment_id": str(ev["source_id"]),
                "post_id": pid,
                "soul": soul,
            })
            if pid not in post_hit_souls:
                post_hit_souls[pid] = []
                order.append(("post", pid))
            if soul and soul not in post_hit_souls[pid]:
                post_hit_souls[pid].append(soul)
        elif source_type == "chat_message":
            soul = owner[len("soul:"):] if owner.startswith("soul:") else None
            # retrieve already restricts private hits to the reply soul; guard.
            if reply_soul and soul == reply_soul and soul not in chat_souls:
                links.append({"unit_id": str(item.unit_id), "via": "chat", "soul": soul})
                chat_souls.append(soul)
                order.append(("chat", soul))

    discreet = channel in policy.PUBLIC_CHANNELS
    blocks: list[str] = []
    emitted: list[tuple[str, str]] = []
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
        emitted.append((kind, key))
        used += len(block)
    if blocks:
        text = "[相关对话原文]\n" + "\n\n".join(blocks)
        if truncated:
            text += "\n（更多相关对话已省略）"
    else:
        text = ""

    if logging_service.is_enabled_for("DEBUG"):
        logging_service.log_event(
            "memory_recall",
            level="DEBUG",
            channel=channel,
            reply_soul=reply_soul,
            links=links,
            emitted=[{"kind": kind, "key": key} for kind, key in emitted],
            truncated=truncated,
            trace=trace_context or {},
        )
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


def _attribution_for_source(source_type: str, source_id: str, reply_soul: str | None) -> str:
    """A short provenance tag keyed on the evidence's source_type, not its
    visibility_scope. After the comment bucketing flatten, comment user-facts and
    posts both live in (…, public), so visibility can no longer tell them apart —
    source_type can. For a comment we resolve which soul's area it was (via the
    comments table) so a soul never reads a line said in another soul's comment
    thread as something said to itself. Private chat carries no tag here;
    disclosure is governed by the discretion flag instead."""
    if source_type in ("post", "post_vision"):
        return "（公开帖子）"
    if source_type in mes.COMMENT_SOURCE_TYPES:
        target = _comment_target(source_id)
        soul = str(target["soul_name"]) if target is not None else None
        if soul and soul != reply_soul:
            return f"（用户在 {soul} 的评论区）"
        return "（评论区）"
    return ""


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
    trace_context: dict | None = None,
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
            # comment_relationship is the relationship lens's private copy of a
            # comment; its content already surfaces here as the canonical
            # comment_message, so skip it to avoid double-showing the same line.
            if str(event["source_type"]) == "comment_relationship":
                continue
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
                if str(event["source_type"]) == "comment_relationship":
                    continue
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
    kept_event_ids: set[int] = set()
    used_chars = 0
    for event, reviewing in ordered[:FRESHNESS_MAX_EVENTS]:
        snapshot = str(event["content_snapshot"]).strip()
        if items and used_chars + len(snapshot) > FRESHNESS_CHAR_BUDGET:
            truncated = True
            break
        used_chars += len(snapshot)
        kept_event_ids.add(int(event["id"]))
        vis = str(event["visibility_scope"])
        items.append(FreshnessItem(
            content=snapshot,
            source_channel=str(event["source_channel"]),
            occurred_at=float(event["occurred_at"]),
            owner_scope=str(event["owner_scope"]),
            visibility_scope=vis,
            needs_discretion=_discretion_for(vis, channel, reply_soul),
            reviewing=reviewing,
            source_type=str(event["source_type"]),
            source_id=str(event["source_id"]),
        ))

    if logging_service.is_enabled_for("DEBUG"):
        _log_freshness(
            channel, reply_soul, query, ordered, terms, kept_event_ids,
            n_candidates=len(candidates), truncated=truncated, trace_context=trace_context,
        )
    return items, truncated


def _log_freshness(
    channel: str,
    reply_soul: str | None,
    query: str,
    ordered: list[tuple[sqlite3.Row, bool]],
    terms: list[str],
    kept_event_ids: set[int],
    *,
    n_candidates: int,
    truncated: bool,
    trace_context: dict | None = None,
) -> None:
    """Emit one structured 'memory_freshness' DEBUG event for the raw-evidence seam.
    Freshness is NOT vector retrieval: it ranks pending events past the reconcile
    cursor by keyword overlap then recency, so the per-event signal here is the
    overlap COUNT (not a distance), plus whether the event was under review and
    whether the event/char budget kept it. Lets the seam's recall be audited
    offline. DEBUG-gated, silent under the default INFO level."""
    sample = []
    for event, reviewing in ordered[: FRESHNESS_MAX_EVENTS * 2]:
        eid = int(event["id"])
        sample.append(
            {
                "event_id": eid,
                "source_type": str(event["source_type"]),
                "source_id": str(event["source_id"]),
                "keyword_overlap": _keyword_overlap(str(event["content_snapshot"] or ""), terms),
                "reviewing": bool(reviewing),
                "occurred_at": float(event["occurred_at"]),
                "in_budget": eid in kept_event_ids,
            }
        )
    logging_service.log_event(
        "memory_freshness",
        level="DEBUG",
        channel=channel,
        reply_soul=reply_soul,
        raw_query=query,
        n_candidates=n_candidates,
        max_events=FRESHNESS_MAX_EVENTS,
        char_budget=FRESHNESS_CHAR_BUDGET,
        truncated=truncated,
        events=sample,
        trace=trace_context or {},
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


# --- prompt section assembly -----------------------------------------------

_DISCRETION_TAG = "「私密·谨慎」"
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class MemoryPrompt:
    text: str
    used_unit_ids: list[str] = field(default_factory=list)
    used_freshness: list["FreshnessItem"] = field(default_factory=list)
    has_discretion_items: bool = False


def _portrait_text(owner_scope: str, visibility_scope: str, view_type: str) -> str:
    return mvs.read_portrait_body(owner_scope, visibility_scope, view_type)


def build_memory_section(
    channel: str,
    reply_soul: str | None,
    query: str,
    *,
    excluded_sources: set[tuple[str, str]] | None = None,
    semantic_query: str | None = None,
    keywords: list[str] | None = None,
    trace_context: dict | None = None,
) -> MemoryPrompt:
    """Assemble the always-on + retrieved memory block for a reply prompt.

    Layers (design §4.5): baseline portrait -> current state -> relevant units,
    plus a precedence/discretion rule line. Private-but-admitted items are
    tagged so the model self-censors before public disclosure; forbidden memory
    never reaches here (filtered upstream by scope policy).

    ``semantic_query``/``keywords`` (query-rewrite outputs) steer ONLY unit
    retrieval — the abstracted belief layer. The raw ``query`` still drives recall
    and the freshness seam, which match raw user evidence and so prefer the user's
    own words over a rewritten paraphrase."""
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
    hits = retrieve_units(
        query, channel, reply_soul, semantic_query=semantic_query, keywords=keywords,
        trace_context=trace_context,
    )
    terms = _tokenize(query)
    if hits:
        lines = []
        for item in hits:
            tag = f" {_DISCRETION_TAG}" if item.needs_discretion else ""
            has_discretion = has_discretion or item.needs_discretion
            ev = _top_evidence_row(item.unit_id, terms)
            attribution = (
                _attribution_for_source(str(ev["source_type"]), str(ev["source_id"]), reply_soul)
                if ev is not None
                else ""
            )
            attr = f" {attribution}" if attribution else ""
            lines.append(f"- [{item.type}|置信{item.confidence:.1f}] {item.content}{attr}{tag}")
            used.append(item.unit_id)
        sections.append("[相关记忆]\n" + "\n".join(lines))

        # 3.5 faithful raw recall: the full conversation(s) around the hit units'
        #     most-relevant evidence (original post + relevant comment lines, or
        #     chat tail), deduped across hits, budgeted with smart truncation.
        recall = _recall_conversations(hits, channel, reply_soul, terms, trace_context=trace_context)
        if recall:
            sections.append(recall)

    # 4. freshness seam: recent raw evidence not yet reconciled into units, so
    # just-happened facts are available immediately.
    fresh_items, truncated = freshness_seam(
        channel,
        reply_soul,
        query=query,
        excluded_sources=excluded_sources,
        trace_context=trace_context,
    )
    if fresh_items:
        lines = []
        for fitem in fresh_items:
            tag = f" {_DISCRETION_TAG}" if fitem.needs_discretion else ""
            has_discretion = has_discretion or fitem.needs_discretion
            attribution = _attribution_for_source(fitem.source_type, fitem.source_id, reply_soul)
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
        used_freshness=list(fresh_items),
        has_discretion_items=has_discretion,
    )


def cited_units(unit_ids: list[str]) -> list[dict]:
    """Hydrate the memory units a reply actually used (current-state + relevant
    beliefs from build_memory_section.used_unit_ids) into compact citation items.
    Deduped, order-preserving; drops units that no longer exist or are inactive."""
    seen: set[str] = set()
    items: list[dict] = []
    for unit_id in unit_ids:
        if not unit_id or unit_id in seen:
            continue
        seen.add(unit_id)
        row = mus.get_unit(unit_id)
        if row is None or row["status"] != "active":
            continue
        items.append(
            {
                "kind": "unit",
                "unit_id": unit_id,
                "type": str(row["type"]),
                "content": str(row["content"]),
                "confidence": float(row["confidence"]),
            }
        )
    return items


def cited_memory(unit_ids: list[str], fresh_items: list) -> list[dict]:
    """The full memory a reply cited, for the 引用记忆 panel: belief UNITS plus the
    raw freshness evidence (kind='fresh') — recent facts not yet reconciled into a
    unit, which a reply can lean on directly via the [尚未稳定沉淀的原始证据] layer."""
    items = cited_units(unit_ids)
    for fresh in fresh_items:
        content = str(getattr(fresh, "content", "") or "").strip()
        if not content:
            continue
        items.append(
            {
                "kind": "fresh",
                "content": content,
                "channel": str(getattr(fresh, "source_channel", "") or ""),
            }
        )
    return items


def cited_memory_metadata_from(items: list[dict]) -> dict:
    """Versioned 引用记忆 metadata object from already-built citation items
    (see CommentContext.cited_memory / ChatContext.cited_memory)."""
    return {"version": 1, "items": list(items or [])}


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
