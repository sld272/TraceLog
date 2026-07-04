"""Orchestrates reconcile across all buckets with pending evidence.

This is the production entry point for the memory-v2 write path: it finds each
(owner, visibility) bucket with unconsumed events and reconciles a bounded pass
with the real LLM op-producer. The background job invokes it automatically;
the workspace script also exposes it for preview/manual operation.

``dry_run=True`` validates + previews ops without persisting (see
memory_reconciler.reconcile_bucket), which is exactly what the shadow window and
a "show me what units you'd extract from my data" preview need.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import (
    logging_service,
    memory_events_service as mes,
    memory_linker,
    memory_reconciler as recon,
    memory_reflection,
    memory_unit_service as mus,
)
from core.memory_reconcile_producer import (
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


def backfill_tombstone_claims(
    client: LLMClient,
    model: str,
    *,
    normalizer=None,
    trace_context: dict | None = None,
) -> int:
    """Best-effort: give retracted units lacking a normalized_claim one (P2).

    One batched light LLM call canonicalizes up to 30 tombstones per run; the
    claims feed the reconcile prompt (paraphrase-proof suppression) and, for
    false tombstones, the vector index picks them up on its next declarative
    rebuild (expected_docs_from_sqlite). Failures leave the rows for the next
    run — never fails the reconcile job. Returns how many claims were stored."""
    rows = mus.list_tombstones_missing_claim()
    if not rows:
        return 0
    items = [{"unit_id": str(r["id"]), "content": str(r["content"])} for r in rows]
    if normalizer is not None:
        claims = normalizer(items)
    else:
        from core.llm import memory_router

        claims = memory_router.call_memory_normalize_claims(
            client, model, items=items, trace_context=trace_context
        )
    if not claims:
        return 0
    known_ids = {item["unit_id"] for item in items}
    stored = 0
    for unit_id, claim in claims.items():
        if unit_id not in known_ids:
            continue
        mus.set_normalized_claim(unit_id, claim)
        stored += 1
    return stored


@dataclass(frozen=True)
class ReconcileRunResult:
    summaries: list[recon.ReconcileSummary]
    failures: list[ReconcileBucketFailure]
    has_pending_after_run: bool
    relink_failures: list[RelinkFailure] = field(default_factory=list)


def run_type_for_visibility(visibility_scope: str) -> str:
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
                run_type=run_type_for_visibility(visibility_scope),
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
    # Piggyback deterministic deep reflection on the live pass: each persona whose
    # beliefs were just brought current gets its stale states retired (>30d
    # unconfirmed -> dormant) and its durable beliefs sedimented into core. Owner-
    # level and best-effort — a reflection failure must never fail the reconcile
    # job. Skipped in dry-run (it mutates). Decay being time-based, riding the
    # reconcile trigger is fine: an owner with no activity injects nothing anyway.
    if not dry_run:
        for owner_scope in sorted({summary.owner_scope for summary in summaries}):
            try:
                memory_reflection.reflect_persona(owner_scope)
            except Exception as exc:
                logging_service.log_event(
                    "memory_reflection_failed", owner_scope=owner_scope, error=str(exc)
                )
    # Tombstone claim backfill also rides the live pass, best-effort: retracts
    # from this run (and any older stragglers) get their canonical claim so the
    # next reconcile's suppression is paraphrase-proof.
    if not dry_run:
        try:
            backfill_tombstone_claims(client, model, trace_context=trace_context)
        except Exception as exc:
            logging_service.log_event("memory_tombstone_claim_backfill_failed", error=str(exc))
    # Cross-bucket linker (P1) rides the tail too: units touched by this run get
    # their same_fact/contradicts/context_variant links judged, and cross-bucket
    # contradictions land an attribution-free contested mark on the more-public
    # side. Best-effort — link metadata must never fail the reconcile job.
    if not dry_run:
        try:
            memory_linker.run_linker_pass(client, model, trace_context=trace_context)
        except Exception as exc:
            logging_service.log_event("memory_linker_pass_failed", error=str(exc))

    pending_relinks = bool(mus.list_pending_relinks()) if not dry_run else False
    has_pending = (
        False
        if dry_run
        else (
            bool(mes.buckets_with_pending_events(limit_buckets=1))
            or pending_relinks
        )
    )
    return ReconcileRunResult(
        summaries=summaries,
        failures=failures,
        has_pending_after_run=has_pending,
        relink_failures=relink_failures,
    )
