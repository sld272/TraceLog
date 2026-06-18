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


def _format_events(events: list[dict]) -> str:
    if not events:
        return ""
    parts = []
    for event in events:
        snapshot = str(event.get("content_snapshot") or "").strip() or "（无内容/删除）"
        parts.append(
            f"- event_id={event.get('id')} | {event.get('source_channel')}/{event.get('op')} "
            f"| source={event.get('source_type')}:{event.get('source_id')}\n  {snapshot}"
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
        boundary_text = (
            f"owner_scope={boundary.get('owner_scope')}, "
            f"visibility_scope={boundary.get('visibility_scope')}"
        )
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
