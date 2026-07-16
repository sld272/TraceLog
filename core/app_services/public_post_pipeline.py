"""Public post API pipeline orchestration and job handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core import attachment_service, context_builder, db, memory_events_service, memory_reconcile_runner, memory_view_producer, record_service, reply_service, suggestion_pipeline, vector_index_service, vision_service
from core.app_services import event_service, job_service
from core.llm.types import LLMClient

# Memory reconcile is a global background job that merely carries the triggering
# post_id in its payload. It runs after replies are generated and must not keep
# the post's "正在回复 / TA 们正在思考" spinner up, so it is excluded from the
# post's visible pipeline status. Its own failures are handled by the reconcile
# requeue path (job_service.mark_memory_reconcile_failed_or_retry), not surfaced
# as a post-level failure banner.
_BACKGROUND_JOB_TYPES = frozenset({job_service.TYPE_RUN_MEMORY_RECONCILE})


def _foreground_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reply-facing jobs whose progress the post's spinner should reflect."""
    return [job for job in jobs if job["type"] not in _BACKGROUND_JOB_TYPES]


@dataclass(frozen=True)
class CreatedPost:
    post_id: str
    job_ids: list[int]


@dataclass(frozen=True)
class PublicPostReplyContext:
    llm_content: str
    built_context: context_builder.BuiltContext


class MemoryReconcileRunError(RuntimeError):
    """A bucket or post-edit re-link review failed, so the reconcile job is
    retried instead of reported done."""

    def __init__(
        self,
        failures: list[memory_reconcile_runner.ReconcileBucketFailure],
        relink_failures: list[memory_reconcile_runner.RelinkFailure] | None = None,
    ) -> None:
        self.failures = list(failures)
        self.relink_failures = list(relink_failures or [])
        details = "; ".join(
            f"{failure.owner_scope}/{failure.visibility_scope}: {failure.error}"
            for failure in self.failures
        )
        relink_details = "; ".join(
            f"unit {failure.unit_id}: {failure.error}" for failure in self.relink_failures
        )
        super().__init__(
            f"memory reconcile failed for {len(self.failures)} bucket(s) "
            f"and {len(self.relink_failures)} re-link(s): "
            f"{'; '.join(part for part in (details, relink_details) if part)}"
        )


def create_post(content: str, attachment_ids: list[str] | None = None) -> CreatedPost:
    """Persist one public post and enqueue its API background pipeline."""
    body = content.strip()
    attachment_ids = attachment_service.validate_attachment_ids(attachment_ids)
    if not body and not attachment_ids:
        raise ValueError("content 不能为空")
    if len(body) > 20_000:
        raise ValueError("content 不能超过 20000 字符")

    post_id = record_service.save_post(body, index_immediately=False, track_embedding=bool(body))
    attachment_service.attach_to_post(post_id, attachment_ids)
    event_service.append_post_event(post_id, "post_created", {"post_id": post_id})

    job_ids = []
    if body:
        job_ids.append(job_service.enqueue(job_service.TYPE_INDEX_POST_EMBEDDING, {"post_id": post_id}))
    job_ids.append(job_service.enqueue(job_service.TYPE_GENERATE_POST_REPLIES, {"post_id": post_id, "content": body}))
    if body or attachment_ids:
        reconcile_id = job_service.enqueue_memory_reconcile_once(
            {"trigger": "post", "post_id": post_id}
        )
        if reconcile_id is not None:
            job_ids.append(reconcile_id)
    return CreatedPost(post_id=post_id, job_ids=job_ids)


def execute_job(job: dict[str, Any], client: LLMClient, model: str) -> None:
    """Execute one claimed job."""
    job_type = job["type"]
    payload = job.get("payload") or {}
    job_id = int(job["id"])
    if job_type == job_service.TYPE_INDEX_POST_EMBEDDING:
        _run_index_post_embedding(job_id, payload)
    elif job_type == job_service.TYPE_GENERATE_POST_REPLIES:
        _run_generate_post_replies(job_id, payload, client, model)
    elif job_type == job_service.TYPE_RUN_MEMORY_RECONCILE:
        _run_memory_reconcile(job_id, client, model)
    else:
        raise ValueError(f"unsupported job type: {job_type}")


def _run_index_post_embedding(job_id: int, payload: dict[str, Any]) -> None:
    post_id = _required_post_id(payload)
    event_service.append_post_event(post_id, "embedding_started", {"post_id": post_id}, job_id=job_id)
    try:
        record_service.index_post_embedding(post_id)
    except Exception as exc:
        event_service.append_post_event(post_id, "embedding_failed", {"error": str(exc)}, job_id=job_id)
        raise
    event_service.append_post_event(post_id, "embedding_succeeded", {"post_id": post_id}, job_id=job_id)


def _run_generate_post_replies(job_id: int, payload: dict[str, Any], client: LLMClient, model: str) -> None:
    post_id = _required_post_id(payload)
    content = _post_content(post_id)
    attachments = attachment_service.list_post_attachments(post_id)
    summaries = vision_service.describe_attachments(attachments)
    llm_content = vision_service.content_with_summaries(content, attachments, summaries)
    vision_context = vision_service.format_summaries(summaries)
    if vision_context:
        record_service.index_post_vision_embedding(
            post_id,
            vision_context,
            [attachment.id for attachment in attachments],
        )
        with db.transaction() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM memory_ingest_events
                WHERE source_type = 'post_vision' AND source_id = ?
                LIMIT 1
                """,
                (post_id,),
            ).fetchone()
            if existing is None:
                memory_events_service.record_post_vision(
                    conn,
                    post_id=post_id,
                    content=vision_context,
                    occurred_at=db.now_ts(),
                )
    public_context = build_public_post_reply_context(
        post_id,
        llm_content,
        client,
        model,
        trace_context={"channel": "public_post", "post_id": post_id},
    )

    if not public_context.built_context.enabled_souls:
        event_service.append_post_event(post_id, "reply_started", {"soul_count": 0}, job_id=job_id)
        event_service.append_post_event(post_id, "reply_succeeded", {"soul_count": 0}, job_id=job_id)
        return

    for soul in public_context.built_context.enabled_souls:
        event_service.append_post_event(post_id, "reply_started", {"soul_name": soul.name}, job_id=job_id)
    results = reply_service.fanout(post_id, llm_content, client, model, public_context.built_context)
    suggestions = suggestion_pipeline.collect_reply_suggestions(
        user_input=content,
        evidence_ref=f"post:{post_id}",
        client=client,
        model=model,
        context="公开 post",
        trace_context={"channel": "public_post", "post_id": post_id},
    )
    first_success = next((result for result in results if result.ok), None)
    if first_success is not None:
        reply_service.attach_suggestions_to_root_comment(
            post_id,
            first_success.soul_name,
            suggestions,
        )
    for result in results:
        event_type = "reply_succeeded" if result.ok else "reply_failed"
        inline_suggestions = suggestions if first_success is not None and result.soul_name == first_success.soul_name else []
        event_service.append_post_event(
            post_id,
            event_type,
            {
                "soul_name": result.soul_name,
                "reply": result.reply,
                "error": result.error,
                "suggestions": inline_suggestions,
            },
            job_id=job_id,
        )
    failed_results = [result for result in results if not result.ok]
    if failed_results:
        names = "、".join(result.soul_name for result in failed_results)
        first_error = failed_results[0].error or "unknown error"
        raise RuntimeError(f"reply generation failed for {names}: {first_error}")


def build_public_post_reply_context(
    post_id: str,
    llm_content: str,
    client: LLMClient,
    model: str,
    *,
    trace_context: dict[str, Any] | None = None,
) -> PublicPostReplyContext:
    """Build the soul-agnostic shared context used by public post first replies.

    Per-soul memory-v2 is appended downstream in reply_service."""
    effective_trace_context = trace_context or {"channel": "public_post", "post_id": post_id}
    built_context = context_builder.build_context(
        query=llm_content,
        client=client,
        model=model,
        trace_context=effective_trace_context,
    )
    return PublicPostReplyContext(
        llm_content=llm_content,
        built_context=built_context,
    )


def maybe_emit_pipeline_done_for_job(job: dict[str, Any]) -> None:
    """Check if a completed job was the last one for its post; emit pipeline_done if so."""
    payload = job.get("payload") or {}
    post_id = payload.get("post_id")
    if not isinstance(post_id, str) or not post_id.strip():
        return
    _maybe_emit_pipeline_done(post_id.strip())


def summarize_pipeline_status(post_id: str) -> dict[str, Any]:
    """Summarize the reply pipeline state for one public post.

    Only reply-facing jobs count toward the visible state; the memory reconcile
    job runs asynchronously as background bookkeeping and is excluded so the
    spinner clears as soon as replies finish generating.
    """
    jobs = _foreground_jobs(job_service.list_jobs_for_post(post_id))
    pending_jobs = [job for job in jobs if job["status"] == job_service.STATUS_PENDING]
    running_jobs = [job for job in jobs if job["status"] == job_service.STATUS_RUNNING]
    retried_job_ids = {
        int((job.get("payload") or {}).get("retry_of_job_id"))
        for job in jobs
        if (job.get("payload") or {}).get("retry_of_job_id") is not None
    }
    failed_jobs = [
        job
        for job in jobs
        if job["status"] == job_service.STATUS_FAILED and int(job["id"]) not in retried_job_ids
    ]
    retrying_jobs = [
        job
        for job in pending_jobs
        if job.get("error") and int(job.get("attempts") or 0) > 0
    ]

    if failed_jobs:
        state = "failed"
    elif running_jobs or pending_jobs:
        state = "retrying" if retrying_jobs else "running"
    elif jobs:
        state = "done"
    else:
        state = "idle"

    return {
        "state": state,
        "pending_count": len(pending_jobs),
        "running_count": len(running_jobs),
        "retrying_count": len(retrying_jobs),
        "failed_jobs": [_job_summary(job) for job in failed_jobs],
    }


def _maybe_emit_pipeline_done(post_id: str) -> None:
    """Emit pipeline_done when no pending/running reply jobs remain for this post."""
    jobs = _foreground_jobs(job_service.list_jobs_for_post(post_id))
    if not jobs:
        return
    has_unfinished = any(
        job["status"] in (job_service.STATUS_PENDING, job_service.STATUS_RUNNING)
        for job in jobs
    )
    if has_unfinished:
        return
    # Avoid duplicate done events for the same quiet period, but allow a later
    # manual retry cycle to emit its own done event.
    existing_events = event_service.list_post_events(post_id)
    if existing_events and existing_events[-1]["event_type"] == "pipeline_done":
        return
    event_service.append_post_event(post_id, "pipeline_done", {"post_id": post_id})


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "type": job["type"],
        "status": job["status"],
        "attempts": job["attempts"],
        "max_attempts": job["max_attempts"],
        "error": job.get("error"),
        "retryable": job["status"] == job_service.STATUS_FAILED,
    }


def _run_memory_reconcile(job_id: int, client: LLMClient, model: str) -> None:
    """v2 write path: reconcile every bucket with unconsumed evidence into units,
    then refresh any stale or missing identity views from the updated units.

    Yields the (single) worker between buckets whenever an interactive job is
    waiting — user posts and AI replies must never queue behind memory
    maintenance. The continuation job enqueued below resumes the backlog."""
    result = memory_reconcile_runner.run_pending_reconcile(
        client,
        model,
        trigger="api",
        should_yield=job_service.has_pending_interactive_jobs,
    )
    if not result.yielded:
        memory_view_producer.refresh_views_after_reconcile(client, model)
        # Keep the unit vector docs in sync with the new/retracted units so
        # semantic retrieval sees them (hash-gated; unchanged docs are skipped).
        vector_index_service.rebuild_expected_docs()
        vector_index_service.process_outbox()
    if result.failures or result.relink_failures:
        raise MemoryReconcileRunError(
            result.failures,
            result.relink_failures,
        )
    if result.has_pending_after_run:
        job_service.enqueue_memory_reconcile_once(
            {"trigger": "continuation", "previous_job_id": job_id}
        )


def _required_post_id(payload: dict[str, Any]) -> str:
    post_id = payload.get("post_id")
    if not isinstance(post_id, str) or not post_id.strip():
        raise ValueError("job payload missing post_id")
    return post_id.strip()


def _post_content(post_id: str) -> str:
    row = db.query_one("SELECT content FROM posts WHERE id = ?", (post_id,))
    if row is None:
        raise ValueError(f"post 不存在：{post_id}")
    return str(row["content"])
