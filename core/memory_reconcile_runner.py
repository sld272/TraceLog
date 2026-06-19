"""Orchestrates reconcile across all buckets with pending evidence.

This is the additive production entry point for the memory-v2 write path: it
finds every (owner, visibility) bucket with unconsumed events and reconciles
each with the real LLM op-producer. Nothing calls it automatically yet — it is
invoked manually (e.g. a preview/shadow script or, after the flip, the deep
reflection job) so the legacy pipeline stays untouched until cutover.

``dry_run=True`` validates + previews ops without persisting (see
memory_reconciler.reconcile_bucket), which is exactly what the shadow window and
a "show me what units you'd extract from my data" preview need.
"""

from __future__ import annotations

from core import logging_service, memory_events_service as mes, memory_reconciler as recon
from core.memory_reconcile_producer import make_llm_op_producer
from core.llm.types import LLMClient


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
    trace_context: dict | None = None,
) -> list[recon.ReconcileSummary]:
    """Reconcile every bucket with pending events. ``op_producer`` may be
    injected for testing; otherwise the real LLM producer is built from
    client/model."""
    producer = op_producer or make_llm_op_producer(client, model, trace_context=trace_context)
    summaries: list[recon.ReconcileSummary] = []
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
            continue
        if summary is not None:
            summaries.append(summary)
    return summaries
