"""Per-bucket reconcile: consume evidence events -> apply memory unit ops.

This is the spine of the memory-v2 write path: an event-driven, per
(owner_scope, visibility_scope) bucket loop.

    events since cursor + active units in bucket + tombstones
        --(op_producer; the LLM seam)-->  add/confirm/revise/retract ops
        --apply (boundary + batch-membership validated)-->  memory_units
        --advance cursor + log reconcile run + unit ops--  (one transaction)

The LLM call is injected as ``op_producer`` so this module stays deterministic
and unit-testable. Per the write invariants: the op_producer (LLM) runs
OUTSIDE the transaction; unit ops
+ cursor advance commit together inside one short transaction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from core import db, logging_service, memory_events_service as mes, memory_unit_service as mus, memory_view_service as mvs

# A new unit must clear a minimum importance to be worth remembering at all.
# The LLM already scores momentary trivia low (e.g. "用户正在上课" -> ~0.2); this
# deterministic floor enforces it regardless of the LLM's stochastic decision to
# emit. It is well below the core-portrait entry bar (memory_view_service
# MIN_IMPORTANCE 0.70): 0.30 just to exist, 0.70 to enter the portrait.
MIN_ADD_IMPORTANCE = 0.30

# P4: LLM confidence/importance snap to five anchored levels. Free floats are
# not comparable across models (one model's 0.8 is another's 0.6); the prompt
# asks for a level pick and this snap enforces it even when the model
# improvises. The MIN_ADD_IMPORTANCE floor is checked on the RAW value first so
# trivia at 0.2 cannot ride the 0.3 anchor in.
SCORE_ANCHORS = (0.3, 0.5, 0.7, 0.85, 0.95)


def snap_score(value: float) -> float:
    return min(SCORE_ANCHORS, key=lambda anchor: abs(anchor - float(value)))


def _snap_optional(value) -> float | None:
    return None if value is None else snap_score(float(value))

# Run types per bucket kind (kept distinct for the workbench).
RECONCILE_GLOBAL = "global_reconcile"
RECONCILE_THREAD = "thread_reconcile"
RECONCILE_SOUL_PRIVATE = "private_reconcile"

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


# P2 tombstone double-insurance: prompt-level suppression asks the model not to
# re-derive a retracted-as-false belief; this code-side guard catches the ones
# it re-derives anyway. Exact match compares against content AND
# normalized_claim; the vector check catches paraphrases. Cosine similarity at
# or above this bar to a false tombstone in the same bucket blocks the add.
TOMBSTONE_BLOCK_SIM = 0.86


def _tombstone_blocks_add(
    conn: sqlite3.Connection,
    owner_scope: str,
    visibility_scope: str,
    content: str,
) -> str | None:
    """Return a human-readable block reason if a false tombstone suppresses this
    add, else None. The vector leg is best-effort/fail-open — prompt-level
    suppression still applies when the index is unavailable."""
    text = str(content or "").strip()
    if not text:
        return None
    # exact leg: same-bucket false tombstones block regardless of retractor;
    # user retractions block owner-wide ("别再记这件事" is not bucket-scoped)
    row = conn.execute(
        """
        SELECT id FROM memory_units
        WHERE status IN ('retracted_by_user','retracted_by_model')
          AND retraction_reason = 'false'
          AND (TRIM(content) = ? OR TRIM(COALESCE(normalized_claim, '')) = ?)
          AND (
                (owner_scope = ? AND visibility_scope = ?)
                OR status = 'retracted_by_user'
              )
        LIMIT 1
        """,
        (text, text, owner_scope, visibility_scope),
    ).fetchone()
    if row is not None:
        return f"与已删除(false)的记忆完全同文，tombstone 拦截（{row['id']}）"
    try:
        from core import vectorstore

        hits = vectorstore.query_documents(text, n_results=3, where={"type": "tombstone"})
    except Exception:
        return None
    for hit in hits:
        distance = getattr(hit, "distance", None)
        if distance is None:
            continue
        sim = 1.0 - float(distance)
        if sim < TOMBSTONE_BLOCK_SIM:
            continue
        meta = getattr(hit, "metadata", None) or {}
        if str(meta.get("reason")) != "false":
            continue
        same_bucket = (
            str(meta.get("owner_scope")) == owner_scope
            and str(meta.get("visibility_scope")) == visibility_scope
        )
        if not same_bucket:
            tomb = conn.execute(
                "SELECT status FROM memory_units WHERE id = ?",
                (str(meta.get("unit_id") or ""),),
            ).fetchone()
            if tomb is None or tomb["status"] != "retracted_by_user":
                continue  # model retractions stay bucket-local
        return (
            f"与已删除(false)的记忆同义（sim={sim:.2f}），tombstone 拦截"
            f"（{meta.get('unit_id')}）"
        )
    return None


@dataclass
class ReconcileSummary:
    owner_scope: str
    visibility_scope: str
    reconcile_run_id: int | None = None
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
    """Retractions fed to the producer to suppress zombie re-derivation (E1).

    Three refinements over the original "this bucket's last 200 rows":

    * user retractions suppress EVERYWHERE — the user means "别再记这件事",
      not "这个桶别记". Cross-bucket rows are fed by normalized_claim ONLY, so
      a private bucket's raw wording never enters another bucket's prompt;
      rows without a claim yet stay bucket-local until the backfill lands.
      Model retractions remain bucket-local (the model's judgment is
      bucket-scoped by construction).
    * an outdated tombstone expires once a same-bucket ACTIVE unit re-states
      the same claim — the belief legitimately re-formed, the gravestone is
      done. false tombstones never expire.
    * under the cap, false tombstones outrank outdated ones — permanence
      matters more than recency when something must be cut."""
    rows = db.query_all(
        """
        SELECT id, content, status, retraction_reason, normalized_claim,
               owner_scope, visibility_scope
        FROM memory_units AS tomb
        WHERE status IN ('retracted_by_user','retracted_by_model')
          AND (
                (owner_scope = ? AND visibility_scope = ?)
                OR (
                    status = 'retracted_by_user'
                    AND TRIM(COALESCE(normalized_claim, '')) != ''
                )
              )
          AND NOT (
                retraction_reason = 'outdated'
                AND EXISTS (
                    SELECT 1 FROM memory_units live
                    WHERE live.status = 'active'
                      AND live.owner_scope = tomb.owner_scope
                      AND live.visibility_scope = tomb.visibility_scope
                      AND (
                            TRIM(live.content) = TRIM(COALESCE(tomb.normalized_claim, tomb.content))
                         OR TRIM(COALESCE(live.normalized_claim, '')) != ''
                            AND TRIM(live.normalized_claim) = TRIM(COALESCE(tomb.normalized_claim, tomb.content))
                      )
                )
              )
        ORDER BY (retraction_reason = 'false') DESC, updated_at DESC
        LIMIT 200
        """,
        (owner_scope, visibility_scope),
    )
    out: list[dict] = []
    for r in rows:
        item = dict(r)
        if not (
            item["owner_scope"] == owner_scope
            and item["visibility_scope"] == visibility_scope
        ):
            # cross-bucket row: expose the claim only, never the raw wording
            item["content"] = item["normalized_claim"]
        item.pop("owner_scope", None)
        item.pop("visibility_scope", None)
        out.append(item)
    return out


def _insert_reconcile_run(
    conn: sqlite3.Connection,
    *,
    run_type: str,
    owner_scope: str,
    visibility_scope: str,
    trigger: str,
    event_ids: list[int],
    summary_text: str,
) -> int:
    created_at = db.now_ts()
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
        INSERT INTO memory_reconcile_runs(
            run_type, owner_scope, visibility_scope, trigger,
            event_id_start, event_id_end, event_count, summary,
            metadata_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_type,
            owner_scope,
            visibility_scope,
            trigger,
            event_ids[0] if event_ids else None,
            event_ids[-1] if event_ids else None,
            len(event_ids),
            summary_text,
            json.dumps(metadata, ensure_ascii=False),
            created_at,
        ),
    )
    return db.require_lastrowid(cur, "memory reconcile run insert")


def apply_ops(
    conn: sqlite3.Connection,
    *,
    owner_scope: str,
    visibility_scope: str,
    ops: list[dict],
    allowed_add_event_ids: set[int],
    review_event_ids_by_unit: dict[str, set[int]],
    required_review_unit_ids: set[str],
    reconcile_run_id: int | None,
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
                block_reason = _tombstone_blocks_add(
                    conn, owner_scope, visibility_scope, str(op_obj.get("content") or "")
                )
                if block_reason:
                    raise ValueError(block_reason)
                mus.add_unit(
                    owner_scope=owner_scope,
                    visibility_scope=visibility_scope,
                    source_channel=channel,
                    type=str(op_obj.get("type") or "insight"),
                    content=str(op_obj.get("content") or ""),
                    confidence=snap_score(float(op_obj.get("confidence", 0.6))),
                    evidence_event_ids=event_ids,
                    tier=str(op_obj.get("tier") or "contextual"),
                    importance=snap_score(importance),
                    actor="reconciler",
                    reconcile_run_id=reconcile_run_id,
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
                        reconcile_run_id=reconcile_run_id,
                        conn=conn,
                    )
                elif op == "confirm":
                    if target_id in required_review_unit_ids and not event_ids:
                        raise ValueError("challenged unit 的 confirm 必须引用当前有效 evidence")
                    mus.confirm_unit(
                        target_id,
                        evidence_event_ids=event_ids,
                        confidence=_snap_optional(op_obj.get("confidence")),
                        reconcile_run_id=reconcile_run_id,
                        conn=conn,
                    )
                elif op == "revise":
                    if target_id in required_review_unit_ids and not event_ids:
                        raise ValueError("challenged unit 的 revise 必须引用当前有效 evidence")
                    mus.revise_unit(
                        target_id,
                        content=str(op_obj.get("content") or ""),
                        evidence_event_ids=event_ids,
                        confidence=_snap_optional(op_obj.get("confidence")),
                        type=op_obj.get("type"),
                        tier=op_obj.get("tier"),
                        importance=_snap_optional(op_obj.get("importance")),
                        reconcile_run_id=reconcile_run_id,
                        conn=conn,
                    )
                else:  # retract
                    reason = op_obj.get("reason")
                    mus.retract_unit(
                        target_id,
                        by="model",
                        reason=reason if reason in {"false", "outdated"} else None,
                        reconcile_run_id=reconcile_run_id,
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
    run_type: str,
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
    producer_events: list[dict] = []
    for event in user_events:
        item = dict(event)
        item["conversation_context"] = mes.conversation_context_for_event(event)
        producer_events.append(item)
    needs_llm = bool(user_events or required_decisions)
    result = (
        op_producer(
            boundary=boundary,
            events=producer_events,
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
            reconcile_run_id = None
            if not dry_run:
                reconcile_run_id = _insert_reconcile_run(
                    conn,
                    run_type=run_type,
                    owner_scope=owner_scope,
                    visibility_scope=visibility_scope,
                    trigger=trigger,
                    event_ids=all_event_ids,
                    summary_text=summary_text or f"reconcile {len(all_event_ids)} events",
                )
                summary.reconcile_run_id = reconcile_run_id
            for unit_id in sorted(deterministic_retracts):
                du = conn.execute(
                    "SELECT source FROM memory_units WHERE id = ?", (unit_id,)
                ).fetchone()
                if du is not None and du["source"] == "user_authored":
                    # A user-authored belief stands on the user's own assertion,
                    # not on the deleted source — keep it instead of auto-retracting.
                    # It can still be overturned later by *new* contradicting
                    # evidence through normal reconcile.
                    mus.retain_unit(
                        unit_id, reconcile_run_id=reconcile_run_id, conn=conn
                    )
                    summary.applied += 1
                    summary._count("retain")
                else:
                    mus.retract_unit(
                        unit_id,
                        by="model",
                        reason="outdated",
                        reconcile_run_id=reconcile_run_id,
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
                reconcile_run_id=reconcile_run_id,
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
        # Keep portrait membership current so a first-time bucket becomes eligible for a
        # per-bucket view refresh, and mark an existing view
        # stale if its core set changed. Hash-gated, so a no-op reconcile leaves
        # a fresh view fresh. LLM re-synthesis runs in the reconcile job after
        # the whole pass.
        mvs.recompute_portrait_membership(owner_scope, visibility_scope)
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
