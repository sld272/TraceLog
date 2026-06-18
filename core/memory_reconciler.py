"""Per-bucket reconcile: consume evidence events -> apply memory unit ops.

This is the spine of the memory-v2 write path. Reconcile replaces the old
"rewrite the whole markdown" deep reflection with an event-driven, per
(owner_scope, visibility_scope) bucket loop:

    events since cursor + active units in bucket + tombstones
        --(op_producer; the LLM seam)-->  add/confirm/revise/retract ops
        --apply (boundary + batch-membership validated)-->  memory_units
        --advance cursor + log reflection + unit ops--  (one transaction)

The LLM call is injected as ``op_producer`` so this module stays deterministic
and unit-testable; Phase 3b plugs in the real reflection_router prompt. Per the
write invariants: the op_producer (LLM) runs OUTSIDE the transaction; unit ops
+ cursor advance commit together inside one short transaction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from core import db, logging_service, memory_events_service as mes, memory_unit_service as mus

# reflection.type values per bucket kind (kept distinct for the workbench).
RECONCILE_GLOBAL = "global_deep_reflection"
RECONCILE_THREAD = "thread_deep_reflection"
RECONCILE_SOUL_PRIVATE = "soul_deep_reflection"

# bucket visibility -> default source_channel for newly added units.
_CHANNEL_BY_VISIBILITY_PREFIX = (
    ("public", "post"),
    ("thread:", "comment"),
    ("private:soul:", "chat"),
)


def _channel_for_visibility(visibility_scope: str) -> str:
    for prefix, channel in _CHANNEL_BY_VISIBILITY_PREFIX:
        if visibility_scope == prefix or visibility_scope.startswith(prefix):
            return channel
    return "post"


@dataclass
class ReconcileSummary:
    owner_scope: str
    visibility_scope: str
    reflection_id: int | None = None
    event_count: int = 0
    last_event_id: int = 0
    applied: int = 0
    skipped: int = 0
    by_op: dict[str, int] = field(default_factory=dict)
    skipped_details: list[dict] = field(default_factory=list)

    def _count(self, op: str) -> None:
        self.by_op[op] = self.by_op.get(op, 0) + 1


def _load_tombstones(owner_scope: str, visibility_scope: str) -> list[dict]:
    """User/model retractions in this bucket, with reason — fed to the producer
    so it can suppress zombie re-derivation (branch E1)."""
    rows = db.query_all(
        """
        SELECT id, content, status, retraction_reason
        FROM memory_units
        WHERE owner_scope = ? AND visibility_scope = ?
          AND status IN ('retracted_by_user','retracted_by_model')
        ORDER BY updated_at DESC
        LIMIT 200
        """,
        (owner_scope, visibility_scope),
    )
    return [dict(r) for r in rows]


def _insert_reflection_row(
    conn: sqlite3.Connection,
    *,
    reflection_type: str,
    owner_scope: str,
    visibility_scope: str,
    trigger: str,
    event_ids: list[int],
    summary_text: str,
) -> int:
    ts = datetime.now().astimezone().isoformat()
    metadata = {
        "trigger": trigger,
        "op": "reconcile",
        "owner_scope": owner_scope,
        "visibility_scope": visibility_scope,
        "event_id_start": event_ids[0] if event_ids else None,
        "event_id_end": event_ids[-1] if event_ids else None,
        "event_count": len(event_ids),
    }
    cur = conn.execute(
        """
        INSERT INTO reflections(ts, type, scope_start, scope_end, content, related_posts, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            reflection_type,
            str(event_ids[0]) if event_ids else None,
            str(event_ids[-1]) if event_ids else None,
            summary_text,
            json.dumps(event_ids, ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False),
        ),
    )
    return db.require_lastrowid(cur, "reconcile reflection insert")


def apply_ops(
    conn: sqlite3.Connection,
    *,
    owner_scope: str,
    visibility_scope: str,
    ops: list[dict],
    allowed_event_ids: set[int],
    reflection_id: int | None,
    summary: ReconcileSummary,
) -> None:
    """Apply a validated batch of ops inside the caller's transaction.

    Each op's evidence must be a subset of this batch's events, and any target
    unit must live in this bucket. Per-op validation failures are skipped and
    audited; only unexpected errors propagate (rolling back the whole batch so
    the cursor does not advance)."""
    channel = _channel_for_visibility(visibility_scope)

    for op_obj in ops:
        try:
            op = str(op_obj.get("op"))
            event_ids = [int(e) for e in (op_obj.get("evidence_event_ids") or [])]
            for eid in event_ids:
                if eid not in allowed_event_ids:
                    raise mus.BoundaryError(f"op 引用了非本批 event：{eid}")

            if op == "add":
                if not event_ids:
                    raise ValueError("add 必须引用至少一条本批 evidence event")
                mus.add_unit(
                    owner_scope=owner_scope,
                    visibility_scope=visibility_scope,
                    source_channel=channel,
                    type=str(op_obj.get("type") or "insight"),
                    content=str(op_obj.get("content") or ""),
                    confidence=float(op_obj.get("confidence", 0.6)),
                    evidence_event_ids=event_ids,
                    tier=str(op_obj.get("tier") or "contextual"),
                    importance=float(op_obj.get("importance", 0.5)),
                    actor="reconciler",
                    reflection_id=reflection_id,
                    conn=conn,
                )
            elif op in {"confirm", "revise", "retract"}:
                target_id = str(op_obj.get("target_id") or "")
                self_unit = _require_unit_in_bucket(conn, target_id, owner_scope, visibility_scope)
                if op == "confirm":
                    mus.confirm_unit(
                        target_id,
                        evidence_event_ids=event_ids,
                        confidence=op_obj.get("confidence"),
                        reflection_id=reflection_id,
                        conn=conn,
                    )
                elif op == "revise":
                    mus.revise_unit(
                        target_id,
                        content=str(op_obj.get("content") or ""),
                        evidence_event_ids=event_ids,
                        confidence=op_obj.get("confidence"),
                        type=op_obj.get("type"),
                        tier=op_obj.get("tier"),
                        importance=op_obj.get("importance"),
                        reflection_id=reflection_id,
                        conn=conn,
                    )
                else:  # retract
                    reason = op_obj.get("reason")
                    mus.retract_unit(
                        target_id,
                        by="model",
                        reason=reason if reason in {"false", "outdated"} else None,
                        reflection_id=reflection_id,
                        conn=conn,
                    )
                del self_unit
            else:
                raise ValueError(f"未知 op：{op}")

            summary.applied += 1
            summary._count(op)
        except (ValueError, mus.BoundaryError) as exc:
            summary.skipped += 1
            summary.skipped_details.append({"op": op_obj.get("op"), "reason": str(exc)})


def _require_unit_in_bucket(
    conn: sqlite3.Connection, unit_id: str, owner_scope: str, visibility_scope: str
) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM memory_units WHERE id = ?", (unit_id,)).fetchone()
    if row is None:
        raise ValueError(f"target unit 不存在：{unit_id}")
    if not mus.same_bucket(
        owner_scope, visibility_scope, row["owner_scope"], row["visibility_scope"]
    ):
        raise mus.BoundaryError(
            f"op target unit 跨 bucket：{unit_id} "
            f"({row['owner_scope']},{row['visibility_scope']}) vs ({owner_scope},{visibility_scope})"
        )
    return row


def reconcile_bucket(
    owner_scope: str,
    visibility_scope: str,
    *,
    op_producer,
    reflection_type: str,
    trigger: str = "manual",
    limit: int = 200,
) -> ReconcileSummary | None:
    """Reconcile one bucket. ``op_producer`` is called OUTSIDE the transaction
    with (boundary, events, active_units, tombstones) and returns a list of op
    dicts. Returns None when there is nothing new to consume."""
    mus.validate_boundary(owner_scope, visibility_scope)
    cursor = mes.get_cursor(owner_scope, visibility_scope)
    events = mes.list_events_after(owner_scope, visibility_scope, cursor, limit=limit)
    if not events:
        return None

    event_ids = [int(e["id"]) for e in events]
    active_units = mus.list_active_units_in_bucket(owner_scope, visibility_scope)
    tombstones = _load_tombstones(owner_scope, visibility_scope)

    boundary = {"owner_scope": owner_scope, "visibility_scope": visibility_scope}
    # LLM / producer runs outside the write transaction.
    result = op_producer(
        boundary=boundary,
        events=[dict(e) for e in events],
        active_units=[dict(u) for u in active_units],
        tombstones=tombstones,
    ) or {}
    ops = result.get("ops") if isinstance(result, dict) else None
    if not isinstance(ops, list):
        ops = []
    summary_text = ""
    if isinstance(result, dict):
        summary_text = str(result.get("summary") or "")

    summary = ReconcileSummary(
        owner_scope=owner_scope,
        visibility_scope=visibility_scope,
        event_count=len(event_ids),
        last_event_id=event_ids[-1],
    )

    with db.immediate_transaction() as conn:
        reflection_id = _insert_reflection_row(
            conn,
            reflection_type=reflection_type,
            owner_scope=owner_scope,
            visibility_scope=visibility_scope,
            trigger=trigger,
            event_ids=event_ids,
            summary_text=summary_text or f"reconcile {len(event_ids)} events",
        )
        summary.reflection_id = reflection_id
        apply_ops(
            conn,
            owner_scope=owner_scope,
            visibility_scope=visibility_scope,
            ops=ops,
            allowed_event_ids=set(event_ids),
            reflection_id=reflection_id,
            summary=summary,
        )
        mes.advance_cursor(conn, owner_scope, visibility_scope, event_ids[-1])

    logging_service.log_event(
        "memory_reconcile_bucket",
        owner_scope=owner_scope,
        visibility_scope=visibility_scope,
        event_count=summary.event_count,
        applied=summary.applied,
        skipped=summary.skipped,
        by_op=summary.by_op,
    )
    return summary
