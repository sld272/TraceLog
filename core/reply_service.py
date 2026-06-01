"""Multi-SOUL post reply service."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from core import db, logging_service, record_service
from core.context_builder import BuiltContext
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext


FAILED_REPLY = "这个 SOUL 暂时没有回复成功，稍后可以重试。"


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

    max_workers = max(1, len(souls))
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
            for soul in souls
        }
        for future in as_completed(future_to_soul):
            soul = future_to_soul[future]
            try:
                result = future.result()
            except Exception as exc:
                result = _failed_result(soul, str(exc))
            _save_comment(post_id, result, model)
            results.append(result)

    return sorted(results, key=lambda result: (result.sort_order, result.soul_name))


def _call_one_soul(
    post_id: str,
    soul: SoulContext,
    user_input: str,
    client: LLMClient,
    model: str,
    shared_context: str,
) -> SoulReplyResult:
    data = reply_router.call_soul_post_reply(
        user_input,
        client,
        model,
        shared_context,
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
            fallback_reply=FAILED_REPLY,
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
            fallback_reply=FAILED_REPLY,
        )
        return _failed_result(soul, error)

    return SoulReplyResult(
        soul_name=soul.name,
        sort_order=soul.sort_order,
        ok=True,
        reply=reply.strip(),
        error=None,
    )

def _failed_result(soul: SoulContext, error: str) -> SoulReplyResult:
    return SoulReplyResult(
        soul_name=soul.name,
        sort_order=soul.sort_order,
        ok=False,
        reply=FAILED_REPLY,
        error=error,
    )


def _save_comment(post_id: str, result: SoulReplyResult, model: str) -> None:
    metadata = {
        "status": "ok" if result.ok else "failed",
        "model": model,
        "error": result.error,
    }
    now = db.now_ts()
    with db.immediate_transaction() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM comments
            WHERE post_id = ? AND soul_name = ? AND seq = 0
            """,
            (post_id, result.soul_name),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, metadata, created_at)
                VALUES (?, ?, 'assistant', ?, 0, ?, ?)
                """,
                (post_id, result.soul_name, result.reply, json.dumps(metadata, ensure_ascii=False), now),
            )
            comment_id = db.require_lastrowid(cursor, "root comment insert")
        else:
            comment_id = int(existing["id"])
            conn.execute(
                """
                UPDATE comments
                SET role = 'assistant', content = ?, metadata = ?, created_at = ?
                WHERE id = ?
                """,
                (result.reply, json.dumps(metadata, ensure_ascii=False), now, comment_id),
            )
    record_service.index_comment_embedding(comment_id, post_id, result.soul_name, "assistant", 0, result.reply)
