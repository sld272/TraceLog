"""Orchestrates reconcile across all buckets with pending evidence.

This is the production entry point for the memory-v2 write path: it finds each
(owner, visibility) bucket with unconsumed events and reconciles a bounded pass
with the real LLM op-producer. The background job invokes it automatically in
reconcile write mode; the workspace script also exposes it for preview/manual
operation.

``dry_run=True`` validates + previews ops without persisting (see
memory_reconciler.reconcile_bucket), which is exactly what the shadow window and
a "show me what units you'd extract from my data" preview need.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import (
    legacy_relationship_migration as lrm,
    logging_service,
    memory_events_service as mes,
    memory_reconciler as recon,
    memory_unit_service as mus,
)
from core.memory_reconcile_producer import (
    make_legacy_relationship_judge,
    make_llm_op_producer,
    make_relink_judge,
)
from core.llm.types import LLMClient


@dataclass(frozen=True)
class ReconcileBucketFailure:
    owner_scope: str
    visibility_scope: str
    error: str


@dataclass(frozen=True)
class RelinkFailure:
    unit_id: str
    error: str


@dataclass(frozen=True)
class RelinkRunResult:
    applied: int
    failures: list[RelinkFailure]


@dataclass(frozen=True)
class LegacyMigrationFailure:
    unit_id: str
    error: str


@dataclass(frozen=True)
class LegacyMigrationRunResult:
    applied: int
    deferred: int
    failures: list[LegacyMigrationFailure]


def run_pending_legacy_migrations(
    client: LLMClient,
    model: str,
    *,
    judge=None,
    trace_context: dict | None = None,
    limit: int = 50,
) -> LegacyMigrationRunResult:
    """Verify hidden legacy relationship candidates against raw user evidence."""
    judge = judge or make_legacy_relationship_judge(
        client,
        model,
        trace_context=trace_context,
    )
    applied = 0
    deferred = 0
    failures: list[LegacyMigrationFailure] = []
    for candidate in lrm.list_due_candidates(limit=limit):
        unit_id = str(candidate["id"])
        evidence, max_event_id = lrm.evidence_for_candidate(candidate)
        if not evidence:
            lrm.apply_decision(
                unit_id,
                expected_updated_at=float(candidate["updated_at"]),
                decision="defer",
                max_event_id=max_event_id,
            )
            deferred += 1
            continue
        try:
            result = judge(candidate=dict(candidate), evidence=evidence)
            decision = str(result.get("decision") or "")
            event_ids = [int(item) for item in (result.get("evidence_event_ids") or [])]
            shown_ids = {int(item["id"]) for item in evidence}
            if any(event_id not in shown_ids for event_id in event_ids):
                raise ValueError("legacy migration judge 引用了未展示的 evidence")
            created = lrm.apply_decision(
                unit_id,
                expected_updated_at=float(candidate["updated_at"]),
                decision=decision,
                max_event_id=max_event_id,
                evidence_event_ids=event_ids,
                content=str(result.get("content") or ""),
                confidence=float(result.get("confidence", 0.85)),
                importance=float(result.get("importance", 0.8)),
            )
        except Exception as exc:
            logging_service.log_event(
                "legacy_relationship_migration_failed",
                unit_id=unit_id,
                error=str(exc),
            )
            failures.append(
                LegacyMigrationFailure(unit_id=unit_id, error=str(exc))
            )
            continue
        if decision == "defer" or created is None and decision not in {"retract"}:
            deferred += 1
        else:
            applied += 1
    return LegacyMigrationRunResult(
        applied=applied,
        deferred=deferred,
        failures=failures,
    )


def run_pending_relinks(
    client: LLMClient,
    model: str,
    *,
    judge=None,
    trace_context: dict | None = None,
) -> RelinkRunResult:
    """Process pending post-edit re-link reviews.

    For each user-edited unit, ask the narrow judge which of its review_pending
    links still support the new content, then apply keep/drop atomically. A judge
    failure leaves that review pending (no data lost); a unit changed mid-judge
    discards the stale result (see ``apply_relink``). Anything the judge does not
    explicitly keep is dropped, so every candidate link is resolved."""
    judge = judge or make_relink_judge(client, model, trace_context=trace_context)
    applied = 0
    failures: list[RelinkFailure] = []
    for review in mus.list_pending_relinks():
        relink_id = int(review["relink_id"])
        unit_id = str(review["unit_id"])
        version = float(review["updated_at"])
        candidates = mus.pending_review_evidence_for_unit(unit_id)
        candidate_ids = {int(c["event_id"]) for c in candidates}
        if not candidate_ids:
            mus.apply_relink(
                relink_id, unit_id, expected_version=version,
                keep_event_ids=[], drop_event_ids=[],
            )
            continue
        try:
            result = judge(
                content=str(review["content"]),
                evidence=[dict(c) for c in candidates],
            )
        except Exception as exc:
            logging_service.log_event(
                "memory_relink_failed", unit_id=unit_id, error=str(exc)
            )
            failures.append(RelinkFailure(unit_id=unit_id, error=str(exc)))
            continue
        keep = {int(x) for x in (result.get("keep_event_ids") or [])} & candidate_ids
        drop = candidate_ids - keep
        if mus.apply_relink(
            relink_id, unit_id, expected_version=version,
            keep_event_ids=sorted(keep), drop_event_ids=sorted(drop),
        ):
            applied += 1
    return RelinkRunResult(applied=applied, failures=failures)


@dataclass(frozen=True)
class ReconcileRunResult:
    summaries: list[recon.ReconcileSummary]
    failures: list[ReconcileBucketFailure]
    has_pending_after_run: bool
    relink_failures: list[RelinkFailure] = field(default_factory=list)
    migration_failures: list[LegacyMigrationFailure] = field(default_factory=list)


def reflection_type_for_visibility(visibility_scope: str) -> str:
    if visibility_scope == "public":
        return recon.RECONCILE_GLOBAL
    if visibility_scope.startswith("thread:"):
        return recon.RECONCILE_THREAD
    if visibility_scope.startswith("private:soul:"):
        return recon.RECONCILE_SOUL_PRIVATE
    return recon.RECONCILE_GLOBAL


def run_pending_reconcile(
    client: LLMClient,
    model: str,
    *,
    dry_run: bool = False,
    trigger: str = "manual",
    limit_per_bucket: int = 200,
    op_producer=None,
    relink_judge=None,
    migration_judge=None,
    trace_context: dict | None = None,
) -> ReconcileRunResult:
    """Reconcile every bucket with pending events. ``op_producer`` may be
    injected for testing; otherwise the real LLM producer is built from
    client/model.

    Bucket failures are collected instead of aborting the pass so healthy
    buckets still make progress. Callers must treat a non-empty ``failures`` as
    a failed run. ``has_pending_after_run`` reports bounded-batch backlog for
    live runs; dry-runs intentionally leave every cursor untouched and
    therefore always report False."""
    producer = op_producer or make_llm_op_producer(client, model, trace_context=trace_context)
    summaries: list[recon.ReconcileSummary] = []
    failures: list[ReconcileBucketFailure] = []
    for owner_scope, visibility_scope in mes.buckets_with_pending_events():
        try:
            summary = recon.reconcile_bucket(
                owner_scope,
                visibility_scope,
                op_producer=producer,
                reflection_type=reflection_type_for_visibility(visibility_scope),
                trigger=trigger,
                limit=limit_per_bucket,
                dry_run=dry_run,
            )
        except Exception as exc:
            # One bucket's LLM failure must not abort reconcile for the others.
            # The failed bucket's cursor is left unadvanced (see producer error
            # semantics), so its evidence is retried on the next run.
            logging_service.log_event(
                "memory_reconcile_bucket_failed",
                owner_scope=owner_scope,
                visibility_scope=visibility_scope,
                error=str(exc),
            )
            failures.append(
                ReconcileBucketFailure(
                    owner_scope=owner_scope,
                    visibility_scope=visibility_scope,
                    error=str(exc),
                )
            )
            continue
        if summary is not None:
            summaries.append(summary)
    # Legacy relationship migration runs AFTER bucket reconcile. By then the
    # bucket has consumed its evidence events and the migration judge sees the
    # same current evidence set the producer did, so a confirm/revise cannot
    # race a duplicate unit from the bucket pass over the same evidence. A
    # migration failure does not unwind already-committed buckets: the candidate
    # stays pending and is retried next pass.
    migration_failures: list[LegacyMigrationFailure] = []
    if not dry_run:
        try:
            migration_failures = list(
                run_pending_legacy_migrations(
                    client,
                    model,
                    judge=migration_judge,
                    trace_context=trace_context,
                ).failures
            )
        except Exception as exc:
            logging_service.log_event(
                "legacy_relationship_migration_pass_failed",
                error=str(exc),
            )
            migration_failures = [
                LegacyMigrationFailure(unit_id="*", error=str(exc))
            ]
    # Post-edit re-link runs in the same background pass (separate from the
    # bucket loop). Skipped in dry-run. Per-unit judge failures are reported so
    # the caller can fail/retry the job, and any still-pending re-link counts as
    # backlog so a continuation job is enqueued — a relink failure must never be
    # silently swallowed (the API promises this pass will run).
    relink_failures: list[RelinkFailure] = []
    if not dry_run:
        try:
            relink_failures = list(
                run_pending_relinks(
                    client, model, judge=relink_judge, trace_context=trace_context
                ).failures
            )
        except Exception as exc:
            logging_service.log_event("memory_relink_pass_failed", error=str(exc))
            relink_failures = [RelinkFailure(unit_id="*", error=str(exc))]
    pending_relinks = bool(mus.list_pending_relinks()) if not dry_run else False
    pending_migrations = lrm.has_due_candidates() if not dry_run else False
    has_pending = (
        False
        if dry_run
        else (
            bool(mes.buckets_with_pending_events(limit_buckets=1))
            or pending_relinks
            or pending_migrations
        )
    )
    return ReconcileRunResult(
        summaries=summaries,
        failures=failures,
        has_pending_after_run=has_pending,
        relink_failures=relink_failures,
        migration_failures=migration_failures,
    )
