"""Bridges memory_reconciler's op_producer seam to the reflection_router LLM.

memory_reconciler.reconcile_bucket calls op_producer(boundary, events,
active_units, tombstones) outside its write transaction. This module formats
those structured inputs into prompt text and returns the parsed op batch, so
the reconcile engine stays LLM-free and the prompt lives with the other
reflection prompts.
"""

from __future__ import annotations

from core.llm import reflection_router
from core.llm.types import LLMClient


class ReconcileProducerError(RuntimeError):
    """The LLM op-producer failed: timeout, API error, or unparseable JSON.

    Raised so reconcile_bucket aborts WITHOUT advancing the cursor — the batch
    of evidence stays unconsumed and gets retried. This is what separates a
    genuine failure from a successful "no memory worth keeping" empty result,
    which returns {"ops": []} and legitimately advances the cursor. Collapsing
    the two (the old ``result or {"ops": []}``) silently dropped evidence on
    every transient LLM error.
    """


def _soul_of(owner_scope: str) -> str | None:
    return owner_scope[len("soul:"):] if owner_scope.startswith("soul:") else None


def describe_scene(boundary: dict) -> str:
    """Natural-language scene instead of raw owner/visibility, so the model never
    mistakes the management label (e.g. owner=soul:X) for the unit's subject."""
    owner = str(boundary.get("owner_scope") or "")
    vis = str(boundary.get("visibility_scope") or "")
    soul = _soul_of(owner)
    if vis == "public" and owner == "global":
        return "用户的公开帖子。抽取关于【用户本人】的信念。"
    if vis.startswith("thread:") and soul:
        return (
            f"用户在 AI 人格【{soul}】的公开评论区与其互动。"
            f"抽取关于【用户】（以及用户与{soul}的关系、用户对{soul}的要求）的信念；"
            "特别留意稳定的称呼、互动约定、语气节奏、边界和默契；"
            f"绝不要描述{soul}自身的设定或性格。"
        )
    if vis.startswith("private:soul:") and soul:
        return (
            f"用户与 AI 人格【{soul}】的私聊。"
            f"抽取关于【用户】（以及用户与{soul}的关系、用户对{soul}的要求）的信念；"
            "特别留意稳定的称呼、互动约定、语气节奏、边界和默契；"
            f"绝不要描述{soul}自身的设定或性格。"
        )
    return f"owner={owner}, visibility={vis}。抽取关于【用户】的信念。"


def _format_events(events: list[dict]) -> str:
    if not events:
        return ""
    parts = []
    for event in events:
        snapshot = str(event.get("content_snapshot") or "").strip() or "（无内容/删除）"
        speaker = "用户" if event.get("author") in (None, "user") else f"AI:{event.get('author')}"
        text = (
            f"- event_id={event.get('id')} | 【{speaker}】{event.get('source_channel')}/{event.get('op')}\n"
            f"  {snapshot}"
        )
        context = event.get("conversation_context") or []
        if context:
            text += "\n  对话上下文（仅帮助理解互动，不能作为 evidence 引用）："
            for message in context:
                role = "用户" if message.get("role") == "user" else "SOUL"
                content = str(message.get("content") or "").strip()
                if content:
                    text += f"\n    - 【{role}】{content}"
        parts.append(text)
    return "\n".join(parts)


def _format_units(units: list[dict]) -> str:
    if not units:
        return ""
    parts = []
    for unit in units:
        text = (
            f"- unit_id={unit.get('id')} | type={unit.get('type')} "
            f"| status={unit.get('status')} | confidence={unit.get('confidence')} "
            f"| tier={unit.get('tier')}\n  {unit.get('content')}"
        )
        reasons = unit.get("review_reasons") or []
        evidence = unit.get("current_evidence") or []
        if reasons:
            text += f"\n  待重判原因: {', '.join(str(item) for item in reasons)}"
            text += "\n  当前仍有效 evidence:"
            if evidence:
                for item in evidence:
                    text += (
                        f"\n    - event_id={item.get('event_id')} "
                        f"{item.get('source_type')}/{item.get('source_id')}: "
                        f"{item.get('content')}"
                    )
            else:
                text += " （无）"
        parts.append(text)
    return "\n".join(parts)


def _format_tombstones(tombstones: list[dict]) -> str:
    if not tombstones:
        return ""
    parts = []
    for tomb in tombstones:
        parts.append(f"- reason={tomb.get('retraction_reason')} | {tomb.get('content')}")
    return "\n".join(parts)


def _format_relink_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return ""
    parts = []
    for item in evidence:
        snapshot = str(item.get("content") or "").strip() or "（无内容/删除）"
        parts.append(
            f"- event_id={item.get('event_id')} | "
            f"{item.get('source_type')}/{item.get('source_id')} {item.get('op')}\n  {snapshot}"
        )
    return "\n".join(parts)


def _format_legacy_migration_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return ""
    parts: list[str] = []
    for item in evidence:
        snapshot = str(item.get("content_snapshot") or "").strip()[:600]
        if not snapshot:
            continue
        text = (
            f"- event_id={item.get('id')} | bucket={item.get('visibility_scope')} "
            f"| {item.get('source_channel')}\n  【用户】{snapshot}"
        )
        context = item.get("conversation_context") or []
        if context:
            text += "\n  对话上下文（仅帮助理解，不能作为 evidence 引用）："
            for message in context:
                role = "用户" if message.get("role") == "user" else "SOUL"
                body = str(message.get("content") or "").strip()[:300]
                if body:
                    text += f"\n    - 【{role}】{body}"
        parts.append(text)
    return "\n".join(parts)


def make_legacy_relationship_judge(
    client: LLMClient,
    model: str,
    *,
    trace_context: dict | None = None,
):
    def judge(*, candidate: dict, evidence: list[dict]) -> dict:
        ctx = dict(trace_context or {})
        ctx.update(
            {
                "unit_id": candidate.get("id"),
                "owner_scope": candidate.get("owner_scope"),
                "evidence_count": len(evidence),
            }
        )
        result = reflection_router.call_legacy_relationship_migration(
            client,
            model,
            candidate_text=str(candidate.get("content") or ""),
            evidence_text=_format_legacy_migration_evidence(evidence),
            trace_context=ctx,
        )
        if result is None:
            raise ReconcileProducerError(
                "legacy relationship migration LLM call failed or returned invalid JSON"
            )
        return result

    return judge


def make_relink_judge(client: LLMClient, model: str, *, trace_context: dict | None = None):
    """Return a narrow judge for the post-edit re-link pass: given a unit's new
    content and its candidate evidence, decide which links still support it."""

    def judge(*, content: str, evidence: list[dict]) -> dict:
        result = reflection_router.call_memory_relink(
            client,
            model,
            content=content,
            evidence_text=_format_relink_evidence(evidence),
            trace_context=dict(trace_context or {}),
        )
        if result is None:
            raise ReconcileProducerError(
                "memory_relink LLM call failed or returned unparseable JSON"
            )
        return result

    return judge


def make_llm_op_producer(client: LLMClient, model: str, *, trace_context: dict | None = None):
    """Return an op_producer closure suitable for reconcile_bucket."""

    def producer(*, boundary: dict, events: list[dict], active_units: list[dict], tombstones: list[dict]):
        boundary_text = describe_scene(boundary)
        ctx = dict(trace_context or {})
        ctx.update(boundary)
        ctx["event_count"] = len(events)
        result = reflection_router.call_memory_reconcile(
            client,
            model,
            boundary_text=boundary_text,
            events_text=_format_events(events),
            active_units_text=_format_units(active_units),
            tombstones_text=_format_tombstones(tombstones),
            trace_context=ctx,
        )
        if result is None:
            # Failure (timeout / API error / unparseable JSON). Do NOT degrade
            # to an empty op batch — that would advance the cursor and silently
            # drop this evidence. Raise so reconcile_bucket aborts and the batch
            # is retried later.
            raise ReconcileProducerError(
                "memory_reconcile LLM call failed or returned unparseable JSON"
            )
        return result

    return producer
