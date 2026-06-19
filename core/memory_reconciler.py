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

from core import db, logging_service, memory_events_service as mes, memory_unit_service as mus, memory_view_service as mvs

# A new unit must clear a minimum importance to be worth remembering at all.
# The LLM already scores momentary trivia low (e.g. "用户正在上课" -> ~0.2); this
# deterministic floor enforces it regardless of the LLM's stochastic decision to
# emit. It is well below the core-portrait entry bar (memory_view_service
# MIN_IMPORTANCE 0.70): 0.30 just to exist as a unit, 0.70 to enter user.md.
MIN_ADD_IMPORTANCE = 0.30

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
    summary_text: str = ""
    preview_units: list[dict] = field(default_factory=list)  # populated in dry_run

    def _count(self, op: str) -> None:
        self.by_op[op] = self.by_op.get(op, 0) + 1


class ReconcileReviewError(RuntimeError):
    """The model did not provide a complete, valid challenged-unit decision set."""


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
    allowed_add_event_ids: set[int],
    review_event_ids_by_unit: dict[str, set[int]],
    required_review_unit_ids: set[str],
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
            target_id = str(op_obj.get("target_id") or "")
            allowed_ids = (
                review_event_ids_by_unit.get(target_id, set())
                if target_id in required_review_unit_ids
                else allowed_add_event_ids
            )
            for eid in event_ids:
                if eid not in allowed_ids:
                    raise mus.BoundaryError(f"op 引用了非当前有效 event：{eid}")

            if op == "add":
                if not event_ids:
                    raise ValueError("add 必须引用至少一条本批 evidence event")
                importance = float(op_obj.get("importance", 0.5))
                if importance < MIN_ADD_IMPORTANCE:
                    raise ValueError(
                        f"importance {importance:.2f} 低于阈值 {MIN_ADD_IMPORTANCE}（瞬时琐碎信息，不值得记忆）"
                    )
                mus.add_unit(
                    owner_scope=owner_scope,
                    visibility_scope=visibility_scope,
                    source_channel=channel,
                    type=str(op_obj.get("type") or "insight"),
                    content=str(op_obj.get("content") or ""),
                    confidence=float(op_obj.get("confidence", 0.6)),
                    evidence_event_ids=event_ids,
                    tier=str(op_obj.get("tier") or "contextual"),
                    importance=importance,
                    actor="reconciler",
                    reflection_id=reflection_id,
                    conn=conn,
                )
            elif op in {"retain", "confirm", "revise", "retract"}:
                self_unit = _require_unit_in_bucket(conn, target_id, owner_scope, visibility_scope)
                if op == "retain":
                    if target_id not in required_review_unit_ids:
                        raise ValueError("retain 只能用于待重判 challenged unit")
                    if event_ids:
                        raise ValueError("retain 不应引用 evidence")
                    mus.retain_unit(
                        target_id,
                        reflection_id=reflection_id,
                        conn=conn,
                    )
                elif op == "confirm":
                    if target_id in required_review_unit_ids and not event_ids:
                        raise ValueError("challenged unit 的 confirm 必须引用当前有效 evidence")
                    mus.confirm_unit(
                        target_id,
                        evidence_event_ids=event_ids,
                        confidence=op_obj.get("confidence"),
                        reflection_id=reflection_id,
                        conn=conn,
                    )
                elif op == "revise":
                    if target_id in required_review_unit_ids and not event_ids:
                        raise ValueError("challenged unit 的 revise 必须引用当前有效 evidence")
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
            if str(op_obj.get("target_id") or "") in required_review_unit_ids:
                raise ReconcileReviewError(str(exc)) from exc
            summary.skipped += 1
            summary.skipped_details.append({"op": op_obj.get("op"), "reason": str(exc)})


def _dedupe_events(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    seen: set[int] = set()
    out: list[sqlite3.Row] = []
    for row in rows:
        event_id = int(row["id"])
        if event_id in seen:
            continue
        seen.add(event_id)
        out.append(row)
    return out


def _review_context(
    reviews: list[sqlite3.Row],
) -> tuple[dict[str, dict], set[str], set[str], dict[tuple[str, str], int]]:
    grouped: dict[str, dict] = {}
    deterministic_retracts: set[str] = set()
    required_decisions: set[str] = set()
    source_versions: dict[tuple[str, str], int] = {}
    for review in reviews:
        unit_id = str(review["unit_id"])
        item = grouped.setdefault(
            unit_id,
            {
                "review_ids": [],
                "reasons": set(),
                "trigger_event_ids": [],
                "current_evidence": mus.current_effective_evidence_for_unit(unit_id),
            },
        )
        item["review_ids"].append(int(review["id"]))
        item["reasons"].add(str(review["reason"]))
        item["trigger_event_ids"].append(int(review["trigger_event_id"]))
        key = (str(review["source_type"]), str(review["source_id"]))
        latest = mes.latest_source_event(*key)
        if latest is not None:
            source_versions[key] = int(latest["id"])
    for unit_id, item in grouped.items():
        current = _dedupe_events(item["current_evidence"])
        item["current_evidence"] = current
        for event in current:
            key = (str(event["source_type"]), str(event["source_id"]))
            latest = mes.latest_source_event(*key)
            if latest is not None:
                source_versions[key] = int(latest["id"])
        if item["reasons"] == {"delete"} and not current:
            deterministic_retracts.add(unit_id)
        else:
            required_decisions.add(unit_id)
    return grouped, deterministic_retracts, required_decisions, source_versions


def _validate_review_decisions(ops: list[dict], required_unit_ids: set[str]) -> None:
    decisions: dict[str, list[str]] = {unit_id: [] for unit_id in required_unit_ids}
    for op in ops:
        target = str(op.get("target_id") or "")
        if target not in required_unit_ids:
            continue
        kind = str(op.get("op") or "")
        if kind not in {"retain", "confirm", "revise", "retract"}:
            raise ReconcileReviewError(f"challenged unit {target} 收到非法决定：{kind}")
        decisions[target].append(kind)
    invalid = {
        unit_id: kinds
        for unit_id, kinds in decisions.items()
        if len(kinds) != 1
    }
    if invalid:
        details = ", ".join(f"{unit_id}={kinds or ['missing']}" for unit_id, kinds in invalid.items())
        raise ReconcileReviewError(f"challenged unit 决定不完整或重复：{details}")


def _assert_reconcile_snapshot(
    conn: sqlite3.Connection,
    *,
    owner_scope: str,
    visibility_scope: str,
    cursor: int,
    review_ids: set[int],
    review_ids_by_unit: dict[str, set[int]],
    source_versions: dict[tuple[str, str], int],
) -> None:
    row = conn.execute(
        "SELECT last_event_id FROM memory_reconcile_cursors "
        "WHERE owner_scope = ? AND visibility_scope = ?",
        (owner_scope, visibility_scope),
    ).fetchone()
    current_cursor = int(row["last_event_id"]) if row else 0
    if current_cursor != cursor:
        raise _ConcurrentReconcile()
    if review_ids:
        placeholders = ",".join("?" for _ in review_ids)
        rows = conn.execute(
            f"SELECT id FROM memory_unit_reconcile_queue "
            f"WHERE status = 'pending' AND id IN ({placeholders})",
            tuple(sorted(review_ids)),
        ).fetchall()
        if {int(item["id"]) for item in rows} != review_ids:
            raise _ConcurrentReconcile()
    for unit_id, expected_ids in review_ids_by_unit.items():
        rows = conn.execute(
            "SELECT id FROM memory_unit_reconcile_queue "
            "WHERE status = 'pending' AND unit_id = ? ORDER BY id",
            (unit_id,),
        ).fetchall()
        if {int(item["id"]) for item in rows} != expected_ids:
            raise _ConcurrentReconcile()
    for (source_type, source_id), expected_event_id in source_versions.items():
        latest = mes.latest_source_event(source_type, source_id, conn=conn)
        if latest is None or int(latest["id"]) != expected_event_id:
            raise _ConcurrentReconcile()


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


class _DryRunAbort(Exception):
    """Internal sentinel to roll back a dry-run reconcile transaction."""


class _ConcurrentReconcile(Exception):
    """Another runner advanced this bucket's cursor past our snapshot between
    reading it and committing; abort without re-applying to avoid duplicate
    units. That runner already consumed this evidence."""


def reconcile_bucket(
    owner_scope: str,
    visibility_scope: str,
    *,
    op_producer,
    reflection_type: str,
    trigger: str = "manual",
    limit: int = 200,
    dry_run: bool = False,
) -> ReconcileSummary | None:
    """Reconcile one bucket. ``op_producer`` is called OUTSIDE the transaction
    with (boundary, events, active_units, tombstones) and returns a list of op
    dicts. Returns None when there is nothing new to consume.

    When ``dry_run`` is True, ops are validated and applied inside a transaction
    that is then rolled back: nothing is persisted and the cursor does not
    advance, but the returned summary reflects what *would* happen. This powers
    the shadow / preview workflow before the live flip."""
    mus.validate_boundary(owner_scope, visibility_scope)
    cursor = mes.get_cursor(owner_scope, visibility_scope)
    events = mes.list_events_after(owner_scope, visibility_scope, cursor, limit=limit)
    reviews = mus.list_pending_reviews(owner_scope, visibility_scope)
    if not events and not reviews:
        return None

    all_event_ids = [int(e["id"]) for e in events]
    last_event_id = all_event_ids[-1] if all_event_ids else cursor

    # Only the latest revision of a source may generate a belief. Older create
    # or edit events can still be consumed, but are omitted when a newer
    # revision already exists (including beyond this bounded batch).
    current_events = mes.collapse_to_current_events(events)
    user_events = [
        e
        for e in current_events
        if e["author"] == "user"
        and e["op"] != "delete"
        and str(e["content_snapshot"] or "").strip()
    ]
    review_context, deterministic_retracts, required_decisions, source_versions = (
        _review_context(reviews)
    )
    existing_user_event_ids = {int(event["id"]) for event in user_events}
    for review in reviews:
        if review["reason"] != "edit":
            continue
        current = mes.current_effective_event(
            str(review["source_type"]),
            str(review["source_id"]),
        )
        if current is not None and int(current["id"]) not in existing_user_event_ids:
            user_events.append(current)
            existing_user_event_ids.add(int(current["id"]))
    user_events.sort(key=lambda event: int(event["id"]))
    for event in user_events:
        key = (str(event["source_type"]), str(event["source_id"]))
        latest = mes.latest_source_event(*key)
        if latest is not None:
            source_versions[key] = int(latest["id"])
    if not user_events and not reviews:
        if not dry_run:
            with db.immediate_transaction() as conn:
                mes.advance_cursor(conn, owner_scope, visibility_scope, last_event_id)
        logging_service.log_event(
            "memory_reconcile_no_user_evidence",
            owner_scope=owner_scope,
            visibility_scope=visibility_scope,
            event_count=len(all_event_ids),
        )
        return None

    event_ids = [int(e["id"]) for e in user_events]
    reconcile_units = mus.list_reconcile_units_in_bucket(owner_scope, visibility_scope)
    producer_units: list[dict] = []
    for unit in reconcile_units:
        unit_id = str(unit["id"])
        if unit_id in deterministic_retracts:
            continue
        item = dict(unit)
        if unit_id in review_context:
            context = review_context[unit_id]
            item["review_reasons"] = sorted(context["reasons"])
            item["review_trigger_event_ids"] = list(context["trigger_event_ids"])
            item["current_evidence"] = [
                {
                    "event_id": int(e["id"]),
                    "source_type": str(e["source_type"]),
                    "source_id": str(e["source_id"]),
                    "op": str(e["op"]),
                    "content": str(e["content_snapshot"] or ""),
                }
                for e in context["current_evidence"]
            ]
        producer_units.append(item)
    tombstones = _load_tombstones(owner_scope, visibility_scope)

    boundary = {"owner_scope": owner_scope, "visibility_scope": visibility_scope}
    needs_llm = bool(user_events or required_decisions)
    result = (
        op_producer(
            boundary=boundary,
            events=[dict(e) for e in user_events],
            active_units=producer_units,
            tombstones=tombstones,
        )
        if needs_llm
        else {"ops": [], "summary": "deterministic delete retraction"}
    ) or {}
    ops = result.get("ops") if isinstance(result, dict) else None
    if not isinstance(ops, list):
        ops = []
    summary_text = ""
    if isinstance(result, dict):
        summary_text = str(result.get("summary") or "")
    _validate_review_decisions(ops, required_decisions)

    review_event_ids_by_unit = {
        unit_id: {int(e["id"]) for e in item["current_evidence"]}
        for unit_id, item in review_context.items()
    }
    review_ids = {int(review["id"]) for review in reviews}
    review_ids_by_unit: dict[str, set[int]] = {}
    for review in reviews:
        review_ids_by_unit.setdefault(str(review["unit_id"]), set()).add(int(review["id"]))

    summary = ReconcileSummary(
        owner_scope=owner_scope,
        visibility_scope=visibility_scope,
        event_count=len(event_ids),
        last_event_id=last_event_id,
        summary_text=summary_text,
    )

    try:
        with db.immediate_transaction() as conn:
            if not dry_run:
                _assert_reconcile_snapshot(
                    conn,
                    owner_scope=owner_scope,
                    visibility_scope=visibility_scope,
                    cursor=cursor,
                    review_ids=review_ids,
                    review_ids_by_unit=review_ids_by_unit,
                    source_versions=source_versions,
                )
            reflection_id = None
            if not dry_run:
                reflection_id = _insert_reflection_row(
                    conn,
                    reflection_type=reflection_type,
                    owner_scope=owner_scope,
                    visibility_scope=visibility_scope,
                    trigger=trigger,
                    event_ids=all_event_ids,
                    summary_text=summary_text or f"reconcile {len(all_event_ids)} events",
                )
                summary.reflection_id = reflection_id
            for unit_id in sorted(deterministic_retracts):
                mus.retract_unit(
                    unit_id,
                    by="model",
                    reason="outdated",
                    reflection_id=reflection_id,
                    conn=conn,
                )
                summary.applied += 1
                summary._count("retract")
            apply_ops(
                conn,
                owner_scope=owner_scope,
                visibility_scope=visibility_scope,
                ops=ops,
                allowed_add_event_ids=set(event_ids),
                review_event_ids_by_unit=review_event_ids_by_unit,
                required_review_unit_ids=required_decisions,
                reflection_id=reflection_id,
                summary=summary,
            )
            mus.resolve_review_rows(conn, sorted(review_ids))
            if dry_run:
                preview_rows = conn.execute(
                    """
                    SELECT id, type, content, confidence, tier, importance, status
                    FROM memory_units
                    WHERE owner_scope = ? AND visibility_scope = ? AND status = 'active'
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (owner_scope, visibility_scope),
                ).fetchall()
                summary.preview_units = [dict(r) for r in preview_rows]
                raise _DryRunAbort
            mes.advance_cursor(conn, owner_scope, visibility_scope, last_event_id)
    except _DryRunAbort:
        pass
    except _ConcurrentReconcile:
        logging_service.log_event(
            "memory_reconcile_skipped_concurrent",
            owner_scope=owner_scope,
            visibility_scope=visibility_scope,
            at_cursor=cursor,
        )
        return None

    if not dry_run:
        # Keep in_md_slice current so a first-time bucket becomes eligible for a
        # view (buckets_needing_view keys off it), and mark an existing view
        # stale if its core set changed. Hash-gated, so a no-op reconcile leaves
        # a fresh view fresh. LLM re-synthesis runs in the reconcile job after
        # the whole pass.
        mvs.recompute_slice(owner_scope, visibility_scope)
        mvs.mark_stale_for_bucket(owner_scope, visibility_scope)

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
