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
            f"绝不要描述{soul}自身的设定或性格。"
        )
    if vis.startswith("private:soul:") and soul:
        return (
            f"用户与 AI 人格【{soul}】的私聊。"
            f"抽取关于【用户】（以及用户与{soul}的关系、用户对{soul}的要求）的信念；"
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
        parts.append(
            f"- event_id={event.get('id')} | 【{speaker}】{event.get('source_channel')}/{event.get('op')}\n"
            f"  {snapshot}"
        )
    return "\n".join(parts)


def _format_units(units: list[dict]) -> str:
    if not units:
        return ""
    parts = []
    for unit in units:
        parts.append(
            f"- unit_id={unit.get('id')} | type={unit.get('type')} "
            f"| confidence={unit.get('confidence')} | tier={unit.get('tier')}\n  {unit.get('content')}"
        )
    return "\n".join(parts)


def _format_tombstones(tombstones: list[dict]) -> str:
    if not tombstones:
        return ""
    parts = []
    for tomb in tombstones:
        parts.append(f"- reason={tomb.get('retraction_reason')} | {tomb.get('content')}")
    return "\n".join(parts)


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
        return result or {"ops": [], "summary": ""}

    return producer
