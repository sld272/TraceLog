"""Multi-SOUL post reply service."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from core import db, evidence_service, logging_service, memory_events_service, memory_read, record_service
from core.context_builder import BuiltContext
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext


@dataclass(frozen=True)
class SoulReplyResult:
    soul_name: str
    sort_order: int
    ok: bool
    reply: str
    error: str | None


def fanout(
    post_id: str,
    user_input: str,
    client: LLMClient,
    model: str,
    built_context: BuiltContext,
) -> list[SoulReplyResult]:
    """Call all enabled SOULs concurrently and persist their comments."""
    souls = sorted(built_context.enabled_souls, key=lambda soul: (soul.sort_order, soul.name))
    if not souls:
        return []
    completed_souls = _completed_root_reply_soul_names(post_id)
    pending_souls = [soul for soul in souls if soul.name not in completed_souls]
    if not pending_souls:
        return []

    max_workers = max(1, len(pending_souls))
    results: list[SoulReplyResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_soul = {
            executor.submit(
                _call_one_soul,
                post_id,
                soul,
                user_input,
                client,
                model,
                built_context.shared_context,
            ): soul
            for soul in pending_souls
        }
        for future in as_completed(future_to_soul):
            soul = future_to_soul[future]
            try:
                result = future.result()
            except Exception as exc:
                result = _failed_result(soul, str(exc))
            _save_comment(post_id, result, model, built_context)
            results.append(result)

    return sorted(results, key=lambda result: (result.sort_order, result.soul_name))


def _completed_root_reply_soul_names(post_id: str) -> set[str]:
    rows = db.query_all(
        """
        SELECT soul_name
        FROM comments
        WHERE post_id = ?
          AND role = 'assistant'
          AND seq = 0
          AND TRIM(COALESCE(content, '')) != ''
        """,
        (post_id,),
    )
    return {str(row["soul_name"]) for row in rows if row["soul_name"]}


def _call_one_soul(
    post_id: str,
    soul: SoulContext,
    user_input: str,
    client: LLMClient,
    model: str,
    shared_context: str,
) -> SoulReplyResult:
    soul_context = _with_memory_section(
        shared_context,
        "public_post",
        soul.name,
        user_input,
        excluded_sources={("post", post_id), ("post_vision", post_id)},
    )
    data = reply_router.call_soul_post_reply(
        user_input,
        client,
        model,
        soul_context,
        soul,
        trace_context={"post_id": post_id, "soul_name": soul.name},
    )
    if data is None:
        error = "LLM call failed or returned invalid JSON"
        logging_service.log_event(
            "reply_failed",
            level="WARNING",
            channel="public_post",
            post_id=post_id,
            soul_name=soul.name,
            error=error,
        )
        return _failed_result(soul, error)

    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        error = "LLM response missing non-empty reply"
        logging_service.log_event(
            "reply_failed",
            level="WARNING",
            channel="public_post",
            post_id=post_id,
            soul_name=soul.name,
            error=error,
        )
        return _failed_result(soul, error)

    return SoulReplyResult(
        soul_name=soul.name,
        sort_order=soul.sort_order,
        ok=True,
        reply=reply.strip(),
        error=None,
    )


def _with_memory_section(
    base_context: str,
    channel: str,
    soul_name: str,
    query: str,
    *,
    excluded_sources: set[tuple[str, str]] | None = None,
) -> str:
    """Append the per-soul scope-filtered memory-v2 block."""
    section = memory_read.memory_section_for(
        channel,
        soul_name,
        query,
        excluded_sources=excluded_sources,
    )
    if not section:
        return base_context
    return f"{base_context}\n\n---\n\n# 记忆\n\n{section}" if base_context else f"# 记忆\n\n{section}"


def _failed_result(soul: SoulContext, error: str) -> SoulReplyResult:
    return SoulReplyResult(
        soul_name=soul.name,
        sort_order=soul.sort_order,
        ok=False,
        reply="",
        error=error,
    )


def _save_comment(post_id: str, result: SoulReplyResult, model: str, built_context: BuiltContext) -> None:
    metadata = {
        "status": "ok" if result.ok else "failed",
        "model": model,
        "error": result.error,
    }
    if result.ok:
        metadata["evidence"] = evidence_service.post_id_evidence_metadata(built_context.relevant_post_ids)
    now = db.now_ts()
    with db.immediate_transaction() as conn:
        existing = conn.execute(
            """
            SELECT id, content
            FROM comments
            WHERE post_id = ? AND soul_name = ? AND seq = 0
            """,
            (post_id, result.soul_name),
        ).fetchone()
        if existing is None:
            if not result.ok:
                return
            cursor = conn.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, metadata, created_at)
                VALUES (?, ?, 'assistant', ?, 0, ?, ?)
                """,
                (post_id, result.soul_name, result.reply, json.dumps(metadata, ensure_ascii=False), now),
            )
            comment_id = db.require_lastrowid(cursor, "root comment insert")
        else:
            if not result.ok:
                return
            if str(existing["content"] or "").strip():
                return
            comment_id = int(existing["id"])
            conn.execute(
                """
                UPDATE comments
                SET role = 'assistant', content = ?, metadata = ?
                WHERE id = ?
                """,
                (result.reply, json.dumps(metadata, ensure_ascii=False), comment_id),
            )
        if str(result.reply or "").strip():
            memory_events_service.record_comment_mutation(
                conn,
                comment_id=comment_id,
                post_id=post_id,
                soul_name=result.soul_name,
                role="assistant",
                op="create",
                content=result.reply,
                occurred_at=now,
            )
    record_service.index_comment_embedding(comment_id, post_id, result.soul_name, "assistant", 0, result.reply)


def attach_suggestions_to_root_comment(
    post_id: str,
    soul_name: str,
    suggestions: list[dict],
) -> None:
    """Persist inline suggestions on one root reply without changing its content."""
    if not suggestions:
        return
    row = db.query_one(
        """
        SELECT id, metadata
        FROM comments
        WHERE post_id = ? AND soul_name = ? AND seq = 0
        """,
        (post_id, soul_name),
    )
    if row is None:
        return
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["suggestions"] = suggestions
    db.execute(
        "UPDATE comments SET metadata = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False), row["id"]),
    )
