"""Public post API pipeline orchestration and job handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core import attachment_service, context_builder, db, query_rewriter, record_service, reflector, reply_service, retrieval, todo_service, tool_config_service, vision_service
from core.app_services import event_service, job_service
from core.llm.types import LLMClient

DEEP_REFLECTION_POST_THRESHOLD = 5


@dataclass(frozen=True)
class CreatedPost:
    post_id: str
    job_ids: list[int]


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
    if body and tool_config_service.is_tool_enabled("todo"):
        job_ids.append(job_service.enqueue(job_service.TYPE_RUN_TODO_TOOL, {"post_id": post_id}))
    if body or attachment_ids:
        job_ids.extend(
            [
                job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": post_id}),
                job_service.enqueue(job_service.TYPE_MAYBE_TRIGGER_GLOBAL_DEEP_REFLECTION, {"post_id": post_id}),
            ]
        )
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
    elif job_type == job_service.TYPE_RUN_TODO_TOOL:
        _run_todo_tool(job_id, payload, client, model)
    elif job_type == job_service.TYPE_RUN_LIGHT_REFLECTION:
        _run_light_reflection(job_id, payload, client, model)
    elif job_type == job_service.TYPE_MAYBE_TRIGGER_GLOBAL_DEEP_REFLECTION:
        _run_maybe_global_deep_reflection(job_id, payload, client, model)
    elif job_type == job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION:
        _run_trigger_global_deep_reflection(payload, client, model)
    elif job_type == job_service.TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS:
        _run_trigger_soul_deep_reflections(payload, client, model)
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
    rewritten_query = query_rewriter.rewrite_query(
        client,
        model,
        llm_content,
        "public_post",
        trace_context={"channel": "public_post", "post_id": post_id},
    )
    relevant_ids = retrieval.hybrid_search(
        llm_content,
        k=3,
        semantic_query=rewritten_query.semantic_query,
        fts_keywords=rewritten_query.keywords,
        trace_context={"channel": "public_post", "post_id": post_id},
    )
    built_context = context_builder.build_context(
        relevant_post_ids=relevant_ids,
        query=llm_content,
        fts_keywords=rewritten_query.keywords,
        client=client,
        model=model,
        trace_context={"channel": "public_post", "post_id": post_id},
    )

    if not built_context.enabled_souls:
        event_service.append_post_event(post_id, "reply_started", {"soul_count": 0}, job_id=job_id)
        event_service.append_post_event(post_id, "reply_succeeded", {"soul_count": 0}, job_id=job_id)
        return

    for soul in built_context.enabled_souls:
        event_service.append_post_event(post_id, "reply_started", {"soul_name": soul.name}, job_id=job_id)
    results = reply_service.fanout(post_id, llm_content, client, model, built_context)
    for result in results:
        event_type = "reply_succeeded" if result.ok else "reply_failed"
        event_service.append_post_event(
            post_id,
            event_type,
            {
                "soul_name": result.soul_name,
                "reply": result.reply,
                "error": result.error,
            },
            job_id=job_id,
        )


def _run_todo_tool(job_id: int, payload: dict[str, Any], client: LLMClient, model: str) -> None:
    post_id = _required_post_id(payload)
    event_service.append_post_event(post_id, "todo_started", {"post_id": post_id}, job_id=job_id)
    result = todo_service.run_for_post_safely(post_id, client, model)
    if result.error:
        event_service.append_post_event(post_id, "todo_failed", {"error": result.error}, job_id=job_id)
        raise RuntimeError(result.error)
    event_service.append_post_event(
        post_id,
        "todo_succeeded",
        {
            "applied": result.applied,
            "upserted": result.upserted,
            "deleted": result.deleted,
            "skipped": result.skipped,
        },
        job_id=job_id,
    )


def _run_light_reflection(job_id: int, payload: dict[str, Any], client: LLMClient, model: str) -> None:
    post_id = _required_post_id(payload)
    event_service.append_post_event(post_id, "light_reflection_started", {"post_id": post_id}, job_id=job_id)
    result = reflector.run_light_reflection_safely(post_id, client, model)
    if result is None:
        event_service.append_post_event(post_id, "light_reflection_failed", {"pending_retry": True}, job_id=job_id)
        raise RuntimeError("light reflection failed")
    event_service.append_post_event(
        post_id,
        "light_reflection_succeeded",
        {
            "entities": len(result.entities),
            "emotions": len(result.emotions),
            "events": len(result.events),
            "relations": len(result.relations),
            "importance": result.importance,
        },
        job_id=job_id,
    )


def _run_maybe_global_deep_reflection(job_id: int, payload: dict[str, Any], client: LLMClient, model: str) -> None:
    post_id = _required_post_id(payload)
    scope = reflector.preview_global_deep_reflection_scope(limit=DEEP_REFLECTION_POST_THRESHOLD)
    if len(scope.post_ids) < DEEP_REFLECTION_POST_THRESHOLD:
        event_service.append_post_event(
            post_id,
            "deep_reflection_succeeded",
            {"skipped": True, "pending_post_count": len(scope.post_ids)},
            job_id=job_id,
        )
        return

    event_service.append_post_event(
        post_id,
        "deep_reflection_queued",
        {"pending_post_count": len(scope.post_ids)},
        job_id=job_id,
    )
    try:
        result = reflector.trigger_global_deep_reflection(
            client,
            model,
            trigger="api_threshold",
            limit=100,
        )
    except Exception as exc:
        event_service.append_post_event(post_id, "deep_reflection_failed", {"error": str(exc)}, job_id=job_id)
        raise
    event_service.append_post_event(
        post_id,
        "deep_reflection_succeeded",
        {
            "skipped": result is None,
            "reflection_id": result.id if result is not None else None,
            "related_post_ids": result.related_post_ids if result is not None else [],
            "patch_summary": result.patch_summary if result is not None else None,
        },
        job_id=job_id,
    )


def maybe_emit_pipeline_done_for_job(job: dict[str, Any]) -> None:
    """Check if a completed job was the last one for its post; emit pipeline_done if so."""
    payload = job.get("payload") or {}
    post_id = payload.get("post_id")
    if not isinstance(post_id, str) or not post_id.strip():
        return
    _maybe_emit_pipeline_done(post_id.strip())


def summarize_pipeline_status(post_id: str) -> dict[str, Any]:
    """Summarize background pipeline state for one public post."""
    jobs = job_service.list_jobs_for_post(post_id)
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
    """Emit pipeline_done when no pending/running jobs remain for this post."""
    jobs = job_service.list_jobs_for_post(post_id)
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


def _run_trigger_global_deep_reflection(payload: dict[str, Any], client: LLMClient, model: str) -> None:
    limit = _payload_int(payload, "limit", 100)
    trigger = str(payload.get("trigger") or "api_manual")
    reflector.trigger_global_deep_reflection(
        client,
        model,
        trigger=trigger,
        limit=limit,
    )


def _run_trigger_soul_deep_reflections(payload: dict[str, Any], client: LLMClient, model: str) -> None:
    limit_per_soul = _payload_int(payload, "limit_per_soul", 100)
    trigger = str(payload.get("trigger") or "api_manual")
    reflector.trigger_soul_deep_reflections(
        client,
        model,
        trigger=trigger,
        limit_per_soul=limit_per_soul,
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


def _payload_int(payload: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 500))
