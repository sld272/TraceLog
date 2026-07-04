"""Structured belief layer for memory v2: memory units, ops and evidence links.

A memory unit is a first-class cross-evidence belief (stable id, confidence,
evidence chain, status, time). This module owns the *write* primitives the
reconciler (later phase) and the workbench use to mutate units, plus read
helpers. Every mutating primitive:

  * enforces the (owner_scope, visibility_scope) boundary invariants,
  * verifies any linked evidence events live in the same boundary,
  * appends a row to ``memory_unit_ops`` (the audit / "what changed" log).

All primitives accept an optional ``conn`` so a reconcile batch can commit unit
ops + cursor advance in one transaction; when omitted they open their own.

The reconciler and workbench are the only writers of this layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

from core import db, memory_events_service as mes

# --- boundary vocabulary / validation --------------------------------------

OWNER_GLOBAL = "global"
VIS_PUBLIC = "public"

VALID_TYPES = frozenset(
    {"identity", "preference", "goal", "state", "relationship", "insight", "freeform"}
)


class BoundaryError(ValueError):
    """Raised when an owner/visibility pair is malformed or incoherent, or when
    a unit op would cross buckets."""


def _is_owner(scope: str) -> bool:
    return scope == OWNER_GLOBAL or scope.startswith("soul:")


def validate_owner_scope(owner_scope: str) -> None:
    if not _is_owner(owner_scope):
        raise BoundaryError(f"非法 owner_scope：{owner_scope}")


def validate_boundary(owner_scope: str, visibility_scope: str) -> None:
    if not _is_owner(owner_scope):
        raise BoundaryError(f"非法 owner_scope：{owner_scope}")
    if visibility_scope == VIS_PUBLIC:
        return  # public may be owned by global (user) or a soul (its public beliefs)
    if visibility_scope.startswith("thread:"):
        return  # thread membership may be owned by global or a soul
    if visibility_scope.startswith("private:soul:"):
        soul = visibility_scope[len("private:soul:"):]
        if owner_scope != f"soul:{soul}":
            raise BoundaryError(
                f"private 记忆必须归属同名 soul：owner={owner_scope} visibility={visibility_scope}"
            )
        return
    raise BoundaryError(f"非法 visibility_scope：{visibility_scope}")


def same_bucket(a_owner: str, a_vis: str, b_owner: str, b_vis: str) -> bool:
    return a_owner == b_owner and a_vis == b_vis


# --- ids & dataclass -------------------------------------------------------

def new_unit_id() -> str:
    """Time-sortable unit id: mu_<12 hex ms><10 hex random>."""
    return f"mu_{int(time.time() * 1000):012x}{os.urandom(5).hex()}"


@dataclass(frozen=True)
class MemoryUnit:
    id: str
    owner_scope: str
    visibility_scope: str
    type: str
    content: str
    confidence: float
    status: str
    source: str
    tier: str
    prompt_policy: str
    portrait_policy: str
    importance: float


# --- transaction plumbing --------------------------------------------------

@contextmanager
def _conn_ctx(conn: sqlite3.Connection | None):
    if conn is not None:
        yield conn
    else:
        with db.immediate_transaction() as owned:
            yield owned


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _get_unit_row(conn: sqlite3.Connection, unit_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memory_units WHERE id = ?", (unit_id,)).fetchone()


def _assert_events_in_boundary(
    conn: sqlite3.Connection,
    owner_scope: str,
    visibility_scope: str,
    event_ids: list[int],
) -> None:
    for event_id in event_ids:
        row = conn.execute(
            "SELECT owner_scope, visibility_scope FROM memory_ingest_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
        if row is None:
            raise BoundaryError(f"evidence event 不存在：{event_id}")
        if not same_bucket(
            owner_scope, visibility_scope, row["owner_scope"], row["visibility_scope"]
        ):
            raise BoundaryError(
                f"evidence event {event_id} 不在目标 bucket："
                f"unit=({owner_scope},{visibility_scope}) "
                f"event=({row['owner_scope']},{row['visibility_scope']})"
            )


def _link_evidence(
    conn: sqlite3.Connection,
    unit_id: str,
    event_ids: list[int],
    relation: str = "supports",
) -> None:
    now = db.now_ts()
    for event_id in event_ids:
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_unit_evidence(unit_id, event_id, relation, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (unit_id, int(event_id), relation, now),
        )


def _record_op(
    conn: sqlite3.Connection,
    *,
    unit_id: str,
    op: str,
    actor: str,
    before: dict | None,
    after: dict | None,
    related_unit_id: str | None = None,
    reconcile_run_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO memory_unit_ops(
            unit_id, related_unit_id, op, actor, before_json, after_json,
            reconcile_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            unit_id,
            related_unit_id,
            op,
            actor,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            reconcile_run_id,
            db.now_ts(),
        ),
    )


# --- write primitives ------------------------------------------------------

def add_unit(
    *,
    owner_scope: str,
    visibility_scope: str,
    source_channel: str,
    type: str,
    content: str,
    confidence: float = 0.6,
    evidence_event_ids: list[int] | None = None,
    tier: str = "contextual",
    importance: float = 0.5,
    sensitivity: str = "normal",
    source: str = "reflected",
    prompt_policy: str = "allow",
    portrait_policy: str = "auto",
    actor: str = "reconciler",
    reconcile_run_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    validate_boundary(owner_scope, visibility_scope)
    if type not in VALID_TYPES:
        raise ValueError(f"非法 unit type：{type}")
    body = content.strip()
    if not body:
        raise ValueError("unit content 不能为空")
    event_ids = [int(e) for e in (evidence_event_ids or [])]
    now = db.now_ts()
    unit_id = new_unit_id()

    with _conn_ctx(conn) as c:
        _assert_events_in_boundary(c, owner_scope, visibility_scope, event_ids)
        c.execute(
            """
            INSERT INTO memory_units(
                id, owner_scope, visibility_scope, source_channel, prompt_policy,
                type, content, confidence, source, status, tier, portrait_policy,
                importance, sensitivity, first_seen, last_confirmed,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit_id, owner_scope, visibility_scope, source_channel, prompt_policy,
                type, body, float(confidence), source, tier, portrait_policy,
                float(importance), sensitivity, now, now, now, now,
            ),
        )
        _link_evidence(c, unit_id, event_ids)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="add", actor=actor,
            before=None, after=after, reconcile_run_id=reconcile_run_id,
        )
        _mark_bucket_view_stale(c, owner_scope, visibility_scope, now)
    return unit_id


def confirm_unit(
    unit_id: str,
    *,
    evidence_event_ids: list[int] | None = None,
    confidence_delta: float = 0.05,
    confidence: float | None = None,
    actor: str = "reconciler",
    reconcile_run_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Re-evidence an existing unit: bump last_confirmed and confidence, link new
    evidence. Content is intentionally untouched (a pure confirm)."""
    event_ids = [int(e) for e in (evidence_event_ids or [])]
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        before = _row_to_dict(row)
        _assert_events_in_boundary(c, row["owner_scope"], row["visibility_scope"], event_ids)
        new_conf = (
            float(confidence)
            if confidence is not None
            else min(0.99, float(row["confidence"]) + confidence_delta)
        )
        # fresh same-bucket evidence re-establishes the belief: a cross-bucket
        # contested mark dissolves naturally (P1 "新鲜证据回流后矛盾自然消解").
        c.execute(
            "UPDATE memory_units SET confidence = ?, "
            "status = CASE WHEN status = 'challenged' THEN 'active' ELSE status END, "
            "retraction_reason = CASE WHEN status = 'challenged' THEN NULL ELSE retraction_reason END, "
            "contested_at = NULL, "
            "last_confirmed = ?, updated_at = ? WHERE id = ?",
            (new_conf, now, now, unit_id),
        )
        _link_evidence(c, unit_id, event_ids)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="confirm", actor=actor,
            before=before, after=after, reconcile_run_id=reconcile_run_id,
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)


def revise_unit(
    unit_id: str,
    *,
    content: str,
    evidence_event_ids: list[int] | None = None,
    confidence: float | None = None,
    type: str | None = None,
    tier: str | None = None,
    importance: float | None = None,
    actor: str = "reconciler",
    reconcile_run_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """In-place content update by the reconciler (branch D1). History is preserved
    in the op log. A model revise rewrites the claim from evidence, so the unit's
    source becomes ``reflected`` even if a user had authored the previous wording —
    it is no longer the user's words."""
    body = content.strip()
    if not body:
        raise ValueError("revise content 不能为空")
    if type is not None and type not in VALID_TYPES:
        raise ValueError(f"非法 unit type：{type}")
    event_ids = [int(e) for e in (evidence_event_ids or [])]
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        before = _row_to_dict(row)
        _assert_events_in_boundary(c, row["owner_scope"], row["visibility_scope"], event_ids)
        c.execute(
            """
            UPDATE memory_units
            SET content = ?,
                source = 'reflected',
                confidence = COALESCE(?, confidence),
                type = COALESCE(?, type),
                tier = COALESCE(?, tier),
                importance = COALESCE(?, importance),
                status = CASE WHEN status = 'challenged' THEN 'active' ELSE status END,
                retraction_reason = CASE
                    WHEN status = 'challenged' THEN NULL
                    ELSE retraction_reason
                END,
                contested_at = NULL,
                last_confirmed = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (body, confidence, type, tier, importance, now, now, unit_id),
        )
        _link_evidence(c, unit_id, event_ids, relation="revises")
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="revise", actor=actor,
            before=before, after=after, reconcile_run_id=reconcile_run_id,
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)


def retract_unit(
    unit_id: str,
    *,
    by: str = "model",
    reason: str | None = None,
    actor: str = "reconciler",
    reconcile_run_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    if by not in {"model", "user"}:
        raise ValueError(f"非法 retract by：{by}")
    status = "retracted_by_user" if by == "user" else "retracted_by_model"
    if by == "user" and reason not in {None, "false", "outdated"}:
        raise ValueError(f"非法 retraction_reason：{reason}")
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        if by == "user" and row["status"] not in {"active", "challenged"}:
            # the workbench deletes a live belief, not a historical/dead version;
            # retracting a superseded unit would orphan its superseded_by link.
            raise ValueError(
                f"只能删除 active/challenged 的记忆（当前 status={row['status']}）"
            )
        before = _row_to_dict(row)
        # A retracted belief must leave the portrait immediately. Dropping it from
        # the slice only governs the template fallback; the live view must also be
        # marked stale, otherwise read_portrait_body keeps serving the fresh
        # synthesized text that still contains the deleted belief.
        c.execute(
            "UPDATE memory_units SET status = ?, retraction_reason = ?, "
            "in_portrait = 0, updated_at = ? WHERE id = ?",
            (status, reason, now, unit_id),
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="retract", actor=actor,
            before=before, after=after, reconcile_run_id=reconcile_run_id,
        )


def visibility_rank(visibility_scope: str) -> int:
    """Sensitivity ordering for the iron law: private memory outranks public.

    A belief may be folded only into a survivor at least as private as itself —
    never the reverse — so consolidation can never expose private memory through
    a more-public surviving unit."""
    return 2 if visibility_scope.startswith("private:") else 1


def supersede_unit(
    old_unit_id: str,
    new_unit_id_: str,
    *,
    actor: str = "reconciler",
    reconcile_run_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Mark old unit superseded by a survivor (contradiction or merge).

    Cross-visibility within ONE owner is allowed (so deep reflection can merge a
    persona's public-layer belief into its private-layer one, decision 1A), but
    the iron law is enforced: the survivor must be at least as private as the
    unit it absorbs. Evidence is NOT moved (old keeps its own links), so no
    cross-bucket evidence relinking occurs."""
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        old = _get_unit_row(c, old_unit_id)
        new = _get_unit_row(c, new_unit_id_)
        if old is None or new is None:
            raise ValueError("supersede 的新旧 unit 必须都存在")
        if old["owner_scope"] != new["owner_scope"]:
            raise BoundaryError("supersede 只能发生在同一 owner（主体/人格）内")
        if visibility_rank(new["visibility_scope"]) < visibility_rank(old["visibility_scope"]):
            raise BoundaryError(
                "supersede 违反铁律：survivor 不能比被取代的 unit 更不私密"
            )
        before = _row_to_dict(old)
        c.execute(
            "UPDATE memory_units SET status = 'superseded', superseded_by = ?, "
            "in_portrait = 0, updated_at = ? WHERE id = ?",
            (new_unit_id_, now, old_unit_id),
        )
        after = _row_to_dict(_get_unit_row(c, old_unit_id))
        _record_op(
            c, unit_id=old_unit_id, op="supersede", actor=actor,
            before=before, after=after, related_unit_id=new_unit_id_,
            reconcile_run_id=reconcile_run_id,
        )
        _mark_bucket_view_stale(c, old["owner_scope"], old["visibility_scope"], now)


def retain_unit(
    unit_id: str,
    *,
    actor: str = "reconciler",
    reconcile_run_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Restore a challenged unit without changing its claim or evidence."""
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        if row["status"] != "challenged":
            raise ValueError("retain 只能恢复 challenged unit")
        before = _row_to_dict(row)
        c.execute(
            "UPDATE memory_units SET status = 'active', retraction_reason = NULL, updated_at = ? "
            "WHERE id = ?",
            (now, unit_id),
        )
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c,
            unit_id=unit_id,
            op="retain",
            actor=actor,
            before=before,
            after=after,
            reconcile_run_id=reconcile_run_id,
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)


# --- reflection (P2 deep-reflection) primitives ----------------------------

def decay_unit(
    unit_id: str,
    *,
    actor: str = "reflection",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Deep reflection retires a stale, non-core reflected belief to ``dormant``.

    A dormant unit leaves every read path and the reconcile comparison set (which
    only loads active/challenged), so it stops bloating prompts and growth —
    while staying as history that a future confirm can revive. Only callable on
    an active reflected unit; identity-floor (core) and user-authored beliefs are
    out of scope and rejected by the caller, never decayed here silently."""
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        if row["status"] != "active":
            raise ValueError("decay 只能作用于 active unit")
        before = _row_to_dict(row)
        c.execute(
            "UPDATE memory_units SET status = 'dormant', in_portrait = 0, updated_at = ? "
            "WHERE id = ?",
            (now, unit_id),
        )
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="decay", actor=actor, before=before, after=after
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)


def promote_unit_tier(
    unit_id: str,
    *,
    tier: str,
    actor: str = "reflection",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Deep reflection sediments a unit's tier (e.g. contextual -> core after it
    survives repeated confirms). Tier only; content and evidence are untouched.
    Recomputes portrait membership so a promotion can newly enter the portrait."""
    if tier not in {"core", "contextual", "episodic"}:
        raise ValueError(f"非法 tier：{tier}")
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        if row["status"] != "active":
            raise ValueError("promote 只能作用于 active unit")
        before = _row_to_dict(row)
        c.execute(
            "UPDATE memory_units SET tier = ?, updated_at = ? WHERE id = ?",
            (tier, now, unit_id),
        )
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="promote", actor=actor, before=before, after=after
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)


# --- cross-bucket links & contested marks (P1: link, never merge) ----------

LINK_RELATIONS = frozenset({"same_fact", "contradicts", "context_variant"})


def add_unit_link(
    a_unit_id: str,
    b_unit_id: str,
    relation: str,
    *,
    created_by: str = "linker",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record a relation between two units. Links are pure metadata: no content
    moves, no bucket boundary weakens. Pair order is normalized (a < b) so the
    UNIQUE constraint dedupes both directions."""
    if relation not in LINK_RELATIONS:
        raise ValueError(f"非法 link relation：{relation}")
    if a_unit_id == b_unit_id:
        raise ValueError("不能把 unit 链到自己")
    a, b = sorted((a_unit_id, b_unit_id))
    with _conn_ctx(conn) as c:
        for uid in (a, b):
            if _get_unit_row(c, uid) is None:
                raise ValueError(f"unit 不存在：{uid}")
        # a pair carries ONE relation: a later verdict (e.g. 回访把 contradicts
        # 改判 context_variant) replaces the earlier one instead of stacking.
        c.execute(
            "DELETE FROM memory_unit_links WHERE a_unit_id = ? AND b_unit_id = ?",
            (a, b),
        )
        c.execute(
            """
            INSERT INTO memory_unit_links(a_unit_id, b_unit_id, relation, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (a, b, relation, created_by, db.now_ts()),
        )


def links_for_units(
    unit_ids: list[str], *, relations: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    """Links where BOTH ends are in ``unit_ids`` (read-time folding operates on
    what was actually retrieved together)."""
    if not unit_ids:
        return []
    placeholders = ",".join("?" for _ in unit_ids)
    relation_sql = ""
    params: list = [*unit_ids, *unit_ids]
    if relations:
        relation_sql = f"AND relation IN ({','.join('?' for _ in relations)})"
        params.extend(relations)
    return db.query_all(
        f"""
        SELECT * FROM memory_unit_links
        WHERE a_unit_id IN ({placeholders})
          AND b_unit_id IN ({placeholders})
          {relation_sql}
        """,
        params,
    )


def linked_pair_exists(a_unit_id: str, b_unit_id: str) -> bool:
    a, b = sorted((a_unit_id, b_unit_id))
    return (
        db.query_one(
            "SELECT 1 FROM memory_unit_links WHERE a_unit_id = ? AND b_unit_id = ?",
            (a, b),
        )
        is not None
    )


def mark_contested(
    unit_id: str, *, conn: sqlite3.Connection | None = None
) -> None:
    """Attribution-free cross-bucket contradiction mark (P1). The unit leaves
    the assertive portrait and read paths hedge it; nothing records WHY on any
    surface the model or user sees. Cleared by confirm/revise (fresh same-bucket
    evidence) or an explicit clear (回访改判)."""
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        c.execute(
            "UPDATE memory_units SET contested_at = ?, updated_at = ? WHERE id = ?",
            (now, now, unit_id),
        )
        # a contested belief leaves the assertive portrait immediately (it can
        # still be retrieved, hedged); recompute so the change is not deferred.
        from core import memory_view_service as _mvs  # local import avoids cycle
        _mvs.recompute_portrait_membership(
            row["owner_scope"], row["visibility_scope"], conn=c
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)


def clear_contested(
    unit_id: str, *, conn: sqlite3.Connection | None = None
) -> None:
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        c.execute(
            "UPDATE memory_units SET contested_at = NULL, updated_at = ? WHERE id = ?",
            (now, unit_id),
        )
        from core import memory_view_service as _mvs  # local import avoids cycle
        _mvs.recompute_portrait_membership(
            row["owner_scope"], row["visibility_scope"], conn=c
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)


def set_normalized_claim(
    unit_id: str, claim: str, *, conn: sqlite3.Connection | None = None
) -> None:
    """Store the canonical assertion for a retracted unit (P2). The claim is the
    tombstone's matching key: it feeds the reconcile prompt instead of the raw
    content (wording changes can't dodge it) and backs the tombstone vector doc."""
    claim = str(claim or "").strip()
    if not claim:
        return
    with _conn_ctx(conn) as c:
        c.execute(
            "UPDATE memory_units SET normalized_claim = ?, updated_at = ? WHERE id = ?",
            (claim, db.now_ts(), unit_id),
        )


def list_tombstones_missing_claim(limit: int = 30) -> list[sqlite3.Row]:
    """Retracted units still lacking a normalized claim, oldest retract first —
    the reconcile runner backfills these best-effort each run."""
    return db.query_all(
        """
        SELECT id, content, owner_scope, visibility_scope, retraction_reason
        FROM memory_units
        WHERE status IN ('retracted_by_user', 'retracted_by_model')
          AND (normalized_claim IS NULL OR TRIM(normalized_claim) = '')
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (int(limit),),
    )


def count_confirm_ops(unit_id: str, *, conn: sqlite3.Connection | None = None) -> int:
    """How many times reconcile re-evidenced this unit — the survival signal deep
    reflection uses to decide a contextual belief has sedimented into core."""
    sql = "SELECT COUNT(*) AS n FROM memory_unit_ops WHERE unit_id = ? AND op = 'confirm'"
    if conn is not None:
        return int(conn.execute(sql, (unit_id,)).fetchone()["n"])
    return int(db.query_one(sql, (unit_id,))["n"])


# --- user-facing workbench primitives --------------------------------------

def update_unit(
    unit_id: str,
    *,
    content: str,
    confidence: float | None = None,
    type: str | None = None,
    tier: str | None = None,
    importance: float | None = None,
    actor: str = "user",
    conn: sqlite3.Connection | None = None,
) -> None:
    """User edits a unit in place from the workbench.

    A user edit differs from a model (reconcile) edit in exactly two ways: its
    confidence is raised (a user assertion is a strong signal), and its content
    comes from the user's own view rather than from evidence. Otherwise the unit
    stays an ordinary unit — fully reconcilable: new contradicting evidence can
    still revise/retract/supersede it via normal reconcile.

    The unit goes live immediately (stays ``active``). The existing evidence
    links may no longer correspond to the user's new wording, so they are marked
    ``review_pending`` (not counted as current support, do not trigger challenge)
    and a re-link review is enqueued; a narrow AI pass later keeps the ones that
    still support the new content and drops the rest — not a blunt drop. Only
    ``active``/``challenged`` units may be edited (the workbench edits the live
    belief, not a dead one)."""
    body = content.strip()
    if not body:
        raise ValueError("update content 不能为空")
    if type is not None and type not in VALID_TYPES:
        raise ValueError(f"非法 unit type：{type}")
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        if row["status"] not in {"active", "challenged", "pending"}:
            raise ValueError(
                f"只能编辑 active/challenged/pending 的记忆（当前 status={row['status']}）"
            )
        pre_edit_event_ids = [
            int(e["event_id"])
            for e in c.execute(
                "SELECT event_id FROM memory_unit_evidence WHERE unit_id = ?",
                (unit_id,),
            ).fetchall()
        ]
        before = dict(_row_to_dict(row) or {})
        before["evidence_event_ids"] = pre_edit_event_ids
        new_conf = (
            float(confidence)
            if confidence is not None
            else max(float(row["confidence"]), 0.9)
        )
        pending_candidate = row["status"] == "pending"
        next_tier = tier or ("core" if pending_candidate else None)
        next_importance = (
            importance
            if importance is not None
            else (max(float(row["importance"]), 0.70) if pending_candidate else None)
        )
        c.execute(
            """
            UPDATE memory_units
            SET content = ?,
                source = 'user_authored',
                confidence = ?,
                type = COALESCE(?, type),
                tier = COALESCE(?, tier),
                importance = COALESCE(?, importance),
                status = 'active',
                prompt_policy = CASE
                    WHEN status = 'pending' THEN 'allow'
                    ELSE prompt_policy
                END,
                superseded_by = NULL,
                retraction_reason = NULL,
                last_confirmed = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                body,
                new_conf,
                type,
                next_tier,
                next_importance,
                now,
                now,
                unit_id,
            ),
        )
        # existing links backed the previous wording -> await AI re-link
        c.execute(
            "UPDATE memory_unit_evidence SET review_pending = 1 WHERE unit_id = ?",
            (unit_id,),
        )
        # user taking control resolves any pending challenge on this unit
        c.execute(
            "UPDATE memory_unit_reconcile_queue SET status = 'resolved', resolved_at = ? "
            "WHERE unit_id = ? AND status = 'pending'",
            (now, unit_id),
        )
        # enqueue a re-link review (dedupe: keep only the latest per unit). Only
        # needed when there were links to re-judge. unit_version = this edit's
        # timestamp so a stale judge result cannot overwrite a newer edit.
        if pre_edit_event_ids:
            c.execute(
                "UPDATE memory_unit_relink_queue SET status = 'resolved', resolved_at = ? "
                "WHERE unit_id = ? AND status = 'pending'",
                (now, unit_id),
            )
            c.execute(
                "INSERT INTO memory_unit_relink_queue(unit_id, unit_version, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (unit_id, now, now),
            )
        # the edited belief's portrait eligibility may have changed (new
        # confidence/source/content); recompute the slice now so it takes effect
        # immediately rather than waiting for the next reconcile.
        from core import memory_view_service as _mvs  # local import avoids cycle
        _mvs.recompute_portrait_membership(
            row["owner_scope"], row["visibility_scope"], conn=c
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="user_edit", actor=actor,
            before=before, after=after,
        )


def set_prompt_policy(
    unit_id: str,
    *,
    prompt_policy: str,
    actor: str = "user",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Set whether a unit may be used in any reply prompt ('不要提到该记忆').

    ``no_prompt`` keeps the unit active and reconcilable but removes it from both
    retrieval and the portrait; it is orthogonal to ``portrait_policy``."""
    if prompt_policy not in {"allow", "no_prompt"}:
        raise ValueError(f"非法 prompt_policy：{prompt_policy}")
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        before = _row_to_dict(row)
        c.execute(
            "UPDATE memory_units SET prompt_policy = ?, updated_at = ? WHERE id = ?",
            (prompt_policy, now, unit_id),
        )
        # Portrait selection respects prompt_policy in both directions: no_prompt drops
        # it from the slice, allow restores it if it otherwise qualifies — so
        # re-allowing a memory brings it back without waiting for a reconcile.
        from core import memory_view_service as _mvs  # local import avoids cycle
        _mvs.recompute_portrait_membership(
            row["owner_scope"], row["visibility_scope"], conn=c
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="user_edit", actor=actor,
            before=before, after=after,
        )


def set_portrait_policy(
    unit_id: str,
    *,
    portrait_policy: str,
    actor: str = "user",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Set whether a unit is forced in/out of the portrait ('不进画像').

    ``force_exclude`` only keeps the unit out of the portrait; it can still be
    retrieved. Orthogonal to ``prompt_policy``."""
    if portrait_policy not in {"auto", "force_include", "force_exclude"}:
        raise ValueError(f"非法 portrait_policy：{portrait_policy}")
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        before = _row_to_dict(row)
        c.execute(
            "UPDATE memory_units SET portrait_policy = ?, updated_at = ? WHERE id = ?",
            (portrait_policy, now, unit_id),
        )
        # Portrait selection respects portrait_policy in both directions: force_exclude
        # drops it now, force_include / auto restore it if it qualifies — so
        # re-including a memory brings it back without waiting for a reconcile.
        from core import memory_view_service as _mvs  # local import avoids cycle
        _mvs.recompute_portrait_membership(
            row["owner_scope"], row["visibility_scope"], conn=c
        )
        _mark_bucket_view_stale(c, row["owner_scope"], row["visibility_scope"], now)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="user_edit", actor=actor,
            before=before, after=after,
        )


def challenge_units_for_source(
    conn: sqlite3.Connection,
    trigger_event_id: int,
    *,
    actor: str = "reconciler",
) -> list[str]:
    """Challenge reflected units backed by any revision of the mutated source."""
    trigger = conn.execute(
        "SELECT * FROM memory_ingest_events WHERE id = ?",
        (int(trigger_event_id),),
    ).fetchone()
    if trigger is None:
        raise ValueError(f"trigger event 不存在：{trigger_event_id}")
    if trigger["op"] not in {"edit", "delete"}:
        return []
    # A comment has two lenses (comment_message + comment_relationship) over the
    # SAME utterance; editing/deleting it must challenge both the user-fact unit
    # and the relationship unit, so match across the grouped source types.
    source_types = (
        mes.COMMENT_SOURCE_TYPES
        if trigger["source_type"] in mes.COMMENT_SOURCE_TYPES
        else (trigger["source_type"],)
    )
    placeholders = ",".join("?" for _ in source_types)
    rows = conn.execute(
        f"""
        SELECT DISTINCT u.*
        FROM memory_units u
        JOIN memory_unit_evidence ue ON ue.unit_id = u.id
        JOIN memory_ingest_events e ON e.id = ue.event_id
        WHERE e.source_type IN ({placeholders}) AND e.source_id = ?
          AND ue.review_pending = 0
          AND u.source IN ('reflected','user_authored')
          AND u.status IN ('active','challenged')
        ORDER BY u.id
        """,
        (*source_types, trigger["source_id"]),
    ).fetchall()
    now = db.now_ts()
    challenged: list[str] = []
    for row in rows:
        unit_id = str(row["id"])
        if row["status"] == "active":
            before = _row_to_dict(row)
            conn.execute(
                "UPDATE memory_units SET status = 'challenged', in_portrait = 0, updated_at = ? "
                "WHERE id = ?",
                (now, unit_id),
            )
            after = _row_to_dict(_get_unit_row(conn, unit_id))
            _record_op(
                conn,
                unit_id=unit_id,
                op="challenge",
                actor=actor,
                before=before,
                after=after,
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_unit_reconcile_queue(
                unit_id, trigger_event_id, reason, status, created_at
            ) VALUES (?, ?, ?, 'pending', ?)
            """,
            (unit_id, int(trigger_event_id), str(trigger["op"]), now),
        )
        _mark_bucket_view_stale(conn, row["owner_scope"], row["visibility_scope"], now)
        challenged.append(unit_id)
    return challenged


def _mark_bucket_view_stale(
    conn: sqlite3.Connection,
    owner_scope: str,
    visibility_scope: str,
    now: float,
) -> None:
    if owner_scope == "global" and visibility_scope == "public":
        from core import memory_view_service as _mvs
        view = conn.execute(
            "SELECT * FROM memory_views "
            "WHERE owner_scope = ? AND visibility_scope = ? AND view_type = 'user_portrait'",
            (owner_scope, visibility_scope),
        ).fetchone()
        if view is not None:
            _mvs.recompute_portrait_membership(
                owner_scope, visibility_scope, conn=conn
            )
            units = conn.execute(
                """
                SELECT *
                FROM memory_units
                WHERE owner_scope = ? AND visibility_scope = ?
                  AND in_portrait = 1 AND status = 'active'
                """,
                (owner_scope, visibility_scope),
            ).fetchall()
            if (
                _mvs.source_unit_set_hash(_mvs.order_units(units))
                != view["source_unit_set_hash"]
                or view["renderer_version"] != _mvs.RENDERER_VERSION
            ):
                conn.execute(
                    "UPDATE memory_views SET status = 'stale', updated_at = ? "
                    "WHERE id = ?",
                    (now, view["id"]),
                )
    from core import soul_relationship_memory as _srm
    _srm.mark_stale_if_changed_for_bucket(
        owner_scope,
        visibility_scope,
        conn=conn,
        now=now,
    )


# --- read helpers ----------------------------------------------------------

def get_unit(unit_id: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM memory_units WHERE id = ?", (unit_id,))


def list_units(
    owner_scope: str | None = None,
    visibility_scope: str | None = None,
    *,
    status: str | None = "active",
    type: str | None = None,
    tier: str | None = None,
    prompt_policy: str | None = None,
    in_portrait: int | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list = []
    if owner_scope is not None:
        clauses.append("owner_scope = ?")
        params.append(owner_scope)
    if visibility_scope is not None:
        clauses.append("visibility_scope = ?")
        params.append(visibility_scope)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if type is not None:
        clauses.append("type = ?")
        params.append(type)
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    if prompt_policy is not None:
        clauses.append("prompt_policy = ?")
        params.append(prompt_policy)
    if in_portrait is not None:
        clauses.append("in_portrait = ?")
        params.append(int(in_portrait))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    return db.query_all(
        f"SELECT * FROM memory_units{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
        tuple(params),
    )


def list_active_units_in_bucket(owner_scope: str, visibility_scope: str) -> list[sqlite3.Row]:
    """All active units in a bucket — the reconcile comparison set (branch A1)."""
    return db.query_all(
        """
        SELECT * FROM memory_units
        WHERE owner_scope = ? AND visibility_scope = ? AND status = 'active'
        ORDER BY id ASC
        """,
        (owner_scope, visibility_scope),
    )


def list_active_units_for_owner(owner_scope: str) -> list[sqlite3.Row]:
    """All active units of one owner across BOTH visibility layers — the
    comparison set for owner-level deep-reflection consolidation."""
    return db.query_all(
        """
        SELECT * FROM memory_units
        WHERE owner_scope = ? AND status = 'active'
        ORDER BY id ASC
        """,
        (owner_scope,),
    )


def list_reconcile_units_in_bucket(
    owner_scope: str,
    visibility_scope: str,
) -> list[sqlite3.Row]:
    return db.query_all(
        """
        SELECT * FROM memory_units
        WHERE owner_scope = ? AND visibility_scope = ?
          AND status IN ('active','challenged')
        ORDER BY id ASC
        """,
        (owner_scope, visibility_scope),
    )


def list_pending_reviews(
    owner_scope: str,
    visibility_scope: str,
) -> list[sqlite3.Row]:
    return db.query_all(
        """
        SELECT q.*, e.source_type, e.source_id, e.source_revision,
               e.op AS trigger_op, u.status AS unit_status
        FROM memory_unit_reconcile_queue q
        JOIN memory_units u ON u.id = q.unit_id
        JOIN memory_ingest_events e ON e.id = q.trigger_event_id
        WHERE q.status = 'pending'
          AND u.owner_scope = ? AND u.visibility_scope = ?
        ORDER BY q.id ASC
        """,
        (owner_scope, visibility_scope),
    )


# --- AI re-link pass (after a user edit) -----------------------------------

def list_pending_relinks() -> list[sqlite3.Row]:
    """Pending re-link reviews (one per recently user-edited unit), joined with
    the unit's current content + version for the narrow judge."""
    return db.query_all(
        """
        SELECT q.id AS relink_id, q.unit_id, q.unit_version,
               u.content, u.updated_at
        FROM memory_unit_relink_queue q
        JOIN memory_units u ON u.id = q.unit_id
        WHERE q.status = 'pending'
        ORDER BY q.id ASC
        """
    )


def pending_review_evidence_for_unit(unit_id: str) -> list[sqlite3.Row]:
    """Candidate links awaiting re-link judgment, with each event's snapshot."""
    return db.query_all(
        """
        SELECT ue.event_id, e.source_type, e.source_id, e.op,
               e.content_snapshot AS content
        FROM memory_unit_evidence ue
        JOIN memory_ingest_events e ON e.id = ue.event_id
        WHERE ue.unit_id = ? AND ue.review_pending = 1
        ORDER BY ue.event_id ASC
        """,
        (unit_id,),
    )


def apply_relink(
    relink_id: int,
    unit_id: str,
    *,
    expected_version: float,
    keep_event_ids: list[int],
    drop_event_ids: list[int],
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Atomically apply an AI re-link result. Returns False without changes when
    the result is stale — the queue row is no longer pending, or the unit changed
    since the judge started (a newer edit) — so a stale judgment can never
    overwrite newer content. ``keep`` clears the review flag (counts as support
    again); ``drop`` removes the link."""
    keep = {int(e) for e in keep_event_ids}
    drop = {int(e) for e in drop_event_ids}
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        qrow = c.execute(
            "SELECT status FROM memory_unit_relink_queue WHERE id = ?",
            (int(relink_id),),
        ).fetchone()
        if qrow is None or qrow["status"] != "pending":
            return False
        urow = _get_unit_row(c, unit_id)
        if urow is None or float(urow["updated_at"]) != float(expected_version):
            # unit changed since the judge started -> discard; the row stays
            # pending and is re-judged against current content next pass.
            return False
        before = _row_to_dict(urow)
        if keep:
            placeholders = ",".join("?" for _ in keep)
            c.execute(
                f"UPDATE memory_unit_evidence SET review_pending = 0 "
                f"WHERE unit_id = ? AND review_pending = 1 AND event_id IN ({placeholders})",
                (unit_id, *sorted(keep)),
            )
        if drop:
            placeholders = ",".join("?" for _ in drop)
            c.execute(
                f"DELETE FROM memory_unit_evidence "
                f"WHERE unit_id = ? AND review_pending = 1 AND event_id IN ({placeholders})",
                (unit_id, *sorted(drop)),
            )
        c.execute(
            "UPDATE memory_unit_relink_queue SET status = 'resolved', resolved_at = ? "
            "WHERE id = ?",
            (now, int(relink_id)),
        )
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="relink", actor="reconciler",
            before=before, after=after,
        )
        return True


def current_effective_evidence_for_unit(
    unit_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row]:
    sql = """
        WITH linked_sources AS (
            SELECT DISTINCT linked.source_type, linked.source_id
            FROM memory_unit_evidence ue
            JOIN memory_ingest_events linked ON linked.id = ue.event_id
            WHERE ue.unit_id = ?
              AND ue.review_pending = 0
        )
        SELECT latest.*, 'supports' AS relation
        FROM linked_sources source
        JOIN memory_ingest_events latest
          ON latest.source_type = source.source_type
         AND latest.source_id = source.source_id
        WHERE latest.id = (
              SELECT newest.id
              FROM memory_ingest_events newest
              WHERE newest.source_type = source.source_type
                AND newest.source_id = source.source_id
              ORDER BY newest.source_revision DESC, newest.id DESC
              LIMIT 1
          )
          AND latest.op != 'delete'
          AND latest.author = 'user'
          AND TRIM(COALESCE(latest.content_snapshot, '')) != ''
        ORDER BY latest.id ASC
    """
    if conn is not None:
        return conn.execute(sql, (unit_id,)).fetchall()
    return db.query_all(sql, (unit_id,))


def resolve_review_rows(
    conn: sqlite3.Connection,
    review_ids: list[int],
) -> None:
    if not review_ids:
        return
    placeholders = ",".join("?" for _ in review_ids)
    conn.execute(
        f"UPDATE memory_unit_reconcile_queue "
        f"SET status = 'resolved', resolved_at = ? "
        f"WHERE status = 'pending' AND id IN ({placeholders})",
        (db.now_ts(), *[int(item) for item in review_ids]),
    )


def get_unit_evidence(unit_id: str) -> list[sqlite3.Row]:
    return db.query_all(
        """
        SELECT e.*, ue.relation, ue.review_pending
        FROM memory_unit_evidence ue
        JOIN memory_ingest_events e ON e.id = ue.event_id
        WHERE ue.unit_id = ?
        ORDER BY e.id ASC
        """,
        (unit_id,),
    )


def list_unit_ops(
    *,
    reconcile_run_id: int | None = None,
    unit_id: str | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list = []
    if reconcile_run_id is not None:
        clauses.append("reconcile_run_id = ?")
        params.append(int(reconcile_run_id))
    if unit_id is not None:
        clauses.append("unit_id = ?")
        params.append(unit_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    # Take the most RECENT N (id DESC + LIMIT), then hand them back in chronological
    # (id ASC) order. A plain "ORDER BY id ASC LIMIT N" returns the OLDEST N, which
    # left the "recent memory changes" card frozen on the earliest ops forever.
    return db.query_all(
        f"SELECT * FROM (SELECT * FROM memory_unit_ops{where} "
        f"ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        tuple(params),
    )
