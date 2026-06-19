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

No producer is wired yet — Phase 3 connects deep reflection to these. Phase 2
only establishes the layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

from core import db

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
    profile_policy: str
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
    reflection_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO memory_unit_ops(
            unit_id, related_unit_id, op, actor, before_json, after_json,
            reflection_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            unit_id,
            related_unit_id,
            op,
            actor,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            reflection_id,
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
    profile_policy: str = "auto",
    actor: str = "reconciler",
    reflection_id: int | None = None,
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
                type, content, confidence, source, status, tier, profile_policy,
                importance, sensitivity, first_seen, last_confirmed,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit_id, owner_scope, visibility_scope, source_channel, prompt_policy,
                type, body, float(confidence), source, tier, profile_policy,
                float(importance), sensitivity, now, now, now, now,
            ),
        )
        _link_evidence(c, unit_id, event_ids)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="add", actor=actor,
            before=None, after=after, reflection_id=reflection_id,
        )
    return unit_id


def confirm_unit(
    unit_id: str,
    *,
    evidence_event_ids: list[int] | None = None,
    confidence_delta: float = 0.05,
    confidence: float | None = None,
    actor: str = "reconciler",
    reflection_id: int | None = None,
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
        c.execute(
            "UPDATE memory_units SET confidence = ?, "
            "status = CASE WHEN status = 'challenged' THEN 'active' ELSE status END, "
            "retraction_reason = CASE WHEN status = 'challenged' THEN NULL ELSE retraction_reason END, "
            "last_confirmed = ?, updated_at = ? WHERE id = ?",
            (new_conf, now, now, unit_id),
        )
        _link_evidence(c, unit_id, event_ids)
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="confirm", actor=actor,
            before=before, after=after, reflection_id=reflection_id,
        )


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
    reflection_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """In-place content update (branch D1). History is preserved in the op log."""
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
        if row["source"] == "user_authored":
            raise BoundaryError("user_authored unit 不可被对账 revise（硬免疫）")
        before = _row_to_dict(row)
        _assert_events_in_boundary(c, row["owner_scope"], row["visibility_scope"], event_ids)
        c.execute(
            """
            UPDATE memory_units
            SET content = ?,
                confidence = COALESCE(?, confidence),
                type = COALESCE(?, type),
                tier = COALESCE(?, tier),
                importance = COALESCE(?, importance),
                status = CASE WHEN status = 'challenged' THEN 'active' ELSE status END,
                retraction_reason = CASE
                    WHEN status = 'challenged' THEN NULL
                    ELSE retraction_reason
                END,
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
            before=before, after=after, reflection_id=reflection_id,
        )


def retract_unit(
    unit_id: str,
    *,
    by: str = "model",
    reason: str | None = None,
    actor: str = "reconciler",
    reflection_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    if by not in {"model", "user", "migration"}:
        raise ValueError(f"非法 retract by：{by}")
    status = "retracted_by_user" if by == "user" else "retracted_by_model"
    if by == "user" and reason not in {None, "false", "outdated"}:
        raise ValueError(f"非法 retraction_reason：{reason}")
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        if by == "model" and row["source"] == "user_authored":
            raise BoundaryError("user_authored unit 不可被模型 retract（硬免疫）")
        before = _row_to_dict(row)
        c.execute(
            "UPDATE memory_units SET status = ?, retraction_reason = ?, updated_at = ? WHERE id = ?",
            (status, reason, now, unit_id),
        )
        after = _row_to_dict(_get_unit_row(c, unit_id))
        _record_op(
            c, unit_id=unit_id, op="retract", actor=actor,
            before=before, after=after, reflection_id=reflection_id,
        )


def supersede_unit(
    old_unit_id: str,
    new_unit_id_: str,
    *,
    actor: str = "reconciler",
    reflection_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Mark old unit superseded by a (contradiction) replacement (bi-temporal)."""
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        old = _get_unit_row(c, old_unit_id)
        new = _get_unit_row(c, new_unit_id_)
        if old is None or new is None:
            raise ValueError("supersede 的新旧 unit 必须都存在")
        if not same_bucket(
            old["owner_scope"], old["visibility_scope"],
            new["owner_scope"], new["visibility_scope"],
        ):
            raise BoundaryError("supersede 只能发生在同一 bucket 内")
        before = _row_to_dict(old)
        c.execute(
            "UPDATE memory_units SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
            (new_unit_id_, now, old_unit_id),
        )
        after = _row_to_dict(_get_unit_row(c, old_unit_id))
        _record_op(
            c, unit_id=old_unit_id, op="supersede", actor=actor,
            before=before, after=after, related_unit_id=new_unit_id_,
            reflection_id=reflection_id,
        )


def retain_unit(
    unit_id: str,
    *,
    actor: str = "reconciler",
    reflection_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Restore a challenged unit without changing its claim or evidence."""
    now = db.now_ts()
    with _conn_ctx(conn) as c:
        row = _get_unit_row(c, unit_id)
        if row is None:
            raise ValueError(f"unit 不存在：{unit_id}")
        if row["source"] == "user_authored":
            raise BoundaryError("user_authored unit 不参与自动重判")
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
            reflection_id=reflection_id,
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
    rows = conn.execute(
        """
        SELECT DISTINCT u.*
        FROM memory_units u
        JOIN memory_unit_evidence ue ON ue.unit_id = u.id
        JOIN memory_ingest_events e ON e.id = ue.event_id
        WHERE e.source_type = ? AND e.source_id = ?
          AND u.source IN ('reflected','migrated')
          AND u.status IN ('active','challenged')
        ORDER BY u.id
        """,
        (trigger["source_type"], trigger["source_id"]),
    ).fetchall()
    now = db.now_ts()
    challenged: list[str] = []
    for row in rows:
        unit_id = str(row["id"])
        if row["status"] == "active":
            before = _row_to_dict(row)
            conn.execute(
                "UPDATE memory_units SET status = 'challenged', in_md_slice = 0, updated_at = ? "
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
        conn.execute(
            "UPDATE memory_views SET status = 'stale', updated_at = ? "
            "WHERE owner_scope = ? AND visibility_scope = ? AND view_type = 'user_md'",
            (now, owner_scope, visibility_scope),
        )
    elif str(visibility_scope).startswith("private:soul:"):
        conn.execute(
            "UPDATE memory_views SET status = 'stale', updated_at = ? "
            "WHERE owner_scope = ? AND visibility_scope = ? "
            "AND view_type = 'soul_private_memory'",
            (now, owner_scope, visibility_scope),
        )


# --- read helpers ----------------------------------------------------------

def get_unit(unit_id: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM memory_units WHERE id = ?", (unit_id,))


def list_units(
    owner_scope: str | None = None,
    visibility_scope: str | None = None,
    *,
    status: str | None = "active",
    tier: str | None = None,
    prompt_policy: str | None = None,
    in_md_slice: int | None = None,
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
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    if prompt_policy is not None:
        clauses.append("prompt_policy = ?")
        params.append(prompt_policy)
    if in_md_slice is not None:
        clauses.append("in_md_slice = ?")
        params.append(int(in_md_slice))
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
        SELECT e.*, ue.relation
        FROM memory_unit_evidence ue
        JOIN memory_ingest_events e ON e.id = ue.event_id
        WHERE ue.unit_id = ?
        ORDER BY e.id ASC
        """,
        (unit_id,),
    )


def list_unit_ops(
    *,
    reflection_id: int | None = None,
    unit_id: str | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list = []
    if reflection_id is not None:
        clauses.append("reflection_id = ?")
        params.append(int(reflection_id))
    if unit_id is not None:
        clauses.append("unit_id = ?")
        params.append(unit_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    return db.query_all(
        f"SELECT * FROM memory_unit_ops{where} ORDER BY id ASC LIMIT ?",
        tuple(params),
    )
