"""Multi-SOUL post reply service."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from openai import OpenAI

from core import db
from core import router
from core.context_builder import BuiltContext
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
    client: OpenAI,
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
    soul: SoulContext,
    user_input: str,
    client: OpenAI,
    model: str,
    shared_context: str,
) -> SoulReplyResult:
    data = router.call_soul_post_reply(user_input, client, model, shared_context, soul)
    if data is None:
        return _failed_result(soul, "LLM call failed or returned invalid JSON")

    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return _failed_result(soul, "LLM response missing non-empty reply")

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
    db.execute(
        """
        INSERT INTO comments(post_id, soul_name, content, is_main, metadata, created_at)
        VALUES (?, ?, ?, 0, ?, ?)
        ON CONFLICT(post_id, soul_name) DO UPDATE SET
            content = excluded.content,
            is_main = excluded.is_main,
            metadata = excluded.metadata,
            created_at = excluded.created_at
        """,
        (
            post_id,
            result.soul_name,
            result.reply,
            json.dumps(metadata, ensure_ascii=False),
            now,
        ),
    )
