"""Bridges memory_reconciler's op-producer seam to the memory LLM router.

memory_reconciler.reconcile_bucket calls op_producer(boundary, events,
active_units, tombstones) outside its write transaction. This module formats
those structured inputs into prompt text and returns the parsed op batch, so
the reconcile engine stays LLM-free and the prompt lives with the other
memory prompts.
"""

from __future__ import annotations

from datetime import datetime

from core import time_normalizer
from core.llm import memory_router
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
        return (
            "用户的公开帖子与公开评论。抽取关于【用户本人】的信念"
            "（身份、偏好、目标、持续处境）；某个人格专属的称呼或约定不是用户事实，跳过。"
        )
    if vis == "public" and soul:
        # Route A: the persona's public relationship lens over the user's comments
        # with this soul. Only the relationship texture beyond the public baseline.
        return (
            f"用户在 AI 人格【{soul}】的公开评论区与其互动。"
            f"只抽取【用户与{soul}的相处】在公开基线之上的增量——稳定称呼、互动约定、"
            f"回应偏好、语气节奏、边界与默契，以及用户对{soul}的专属要求；"
            f"用户的客观事实（身份/偏好/处境）交给主记忆，这里不要重复；"
            f"绝不要描述{soul}自身的设定或性格。"
        )
    if vis.startswith("private:soul:") and soul:
        return (
            f"用户与 AI 人格【{soul}】的私聊。"
            f"抽取【用户与{soul}的相处】以及用户在私聊中额外透露的信息——"
            "稳定称呼、互动约定、语气节奏、边界、默契，以及私下才说的处境；"
            f"绝不要描述{soul}自身的设定或性格。"
        )
    return f"owner={owner}, visibility={vis}。抽取关于【用户】的信念。"


def _event_time_note(event: dict) -> str | None:
    """Resolve the event content's relative-time words against the event's OWN
    speech time (occurred_at), never the reconcile clock. Backlogged evidence is
    reconciled hours or days late, so anchoring "明天" on "now" lands it on the
    wrong day; the event timestamp is the only correct anchor here.
    """
    raw = str(event.get("content_snapshot") or "").strip()
    occurred_at = event.get("occurred_at")
    if not raw or occurred_at is None:
        return None
    try:
        anchor = datetime.fromtimestamp(float(occurred_at))
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return time_normalizer.annotation_note(raw, anchor=anchor)


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
        note = _event_time_note(event)
        if note:
            text += f"\n  〔时间标注：{note}〕"
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
        # prefer the normalized claim: canonical phrasing suppresses paraphrases
        # the original wording would miss.
        text = str(tomb.get("normalized_claim") or "").strip() or tomb.get("content")
        parts.append(f"- reason={tomb.get('retraction_reason')} | {text}")
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


def make_relink_judge(client: LLMClient, model: str, *, trace_context: dict | None = None):
    """Return a narrow judge for the post-edit re-link pass: given a unit's new
    content and its candidate evidence, decide which links still support it."""

    def judge(*, content: str, evidence: list[dict]) -> dict:
        result = memory_router.call_memory_relink(
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


def _format_consolidation_units(units: list[dict]) -> str:
    parts = []
    for unit in units:
        visibility = str(unit.get("visibility_scope") or "")
        layer = "private" if visibility.startswith("private:") else "public"
        parts.append(
            f"- unit_id={unit.get('id')} | visibility={layer} | type={unit.get('type')}\n"
            f"  {unit.get('content')}"
        )
    return "\n".join(parts)


def make_consolidation_producer(client: LLMClient, model: str, *, trace_context: dict | None = None):
    """Return a consolidation producer for memory_reflection.consolidate_persona:
    given an owner's active units, propose merge/retract ops."""

    def producer(*, owner_scope: str, units: list[dict]):
        result = memory_router.call_memory_consolidation(
            client,
            model,
            units_text=_format_consolidation_units(units),
            trace_context={**(trace_context or {}), "owner_scope": owner_scope},
        )
        if result is None:
            raise ReconcileProducerError(
                "memory_consolidation LLM call failed or returned unparseable JSON"
            )
        return result

    return producer


def make_llm_op_producer(client: LLMClient, model: str, *, trace_context: dict | None = None):
    """Return an op_producer closure suitable for reconcile_bucket."""

    def producer(*, boundary: dict, events: list[dict], active_units: list[dict], tombstones: list[dict]):
        boundary_text = describe_scene(boundary)
        ctx = dict(trace_context or {})
        ctx.update(boundary)
        ctx["event_count"] = len(events)
        result = memory_router.call_memory_reconcile(
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
