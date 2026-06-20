"""Bridges memory_view_service's synthesizer seam to the reflection_router LLM.

memory_view_service.synthesize_view accepts a ``synthesizer(units, char_budget)``
callable and falls back to its deterministic template on None/error. This module
formats core units into prompt text and calls the LLM synthesis prompt, keeping
memory_view_service LLM-free.
"""

from __future__ import annotations

import sqlite3

from core import memory_view_service as mvs, soul_relationship_memory as srm
from core.llm import reflection_router
from core.llm.types import LLMClient


def _format_units(units: list[sqlite3.Row], view_type: str) -> str:
    if not units:
        return ""
    parts = []
    for unit in units:
        scene = ""
        if view_type == mvs.VIEW_SOUL_RELATIONSHIP:
            visibility = str(unit["visibility_scope"])
            scene = "私聊" if visibility.startswith("private:soul:") else "公开评论"
            scene = f" | 场景={scene}"
        parts.append(
            f"- [{unit['type']}] {unit['content']} "
            f"(confidence={unit['confidence']}, tier={unit['tier']}{scene})"
        )
    return "\n".join(parts)


def make_llm_synthesizer(
    client: LLMClient,
    model: str,
    view_type: str,
    *,
    trace_context: dict | None = None,
):
    """Return a synthesizer(units, char_budget) -> str|None for synthesize_view."""

    def synthesizer(units: list[sqlite3.Row], char_budget: int):
        return reflection_router.call_view_synthesis(
            client,
            model,
            units_text=_format_units(units, view_type),
            char_budget=char_budget,
            view_type=view_type,
            trace_context=trace_context,
        )

    return synthesizer


def refresh_views_after_reconcile(
    client: LLMClient,
    model: str,
    *,
    trace_context: dict | None = None,
) -> list[mvs.SynthesizedView]:
    """Re-synthesize every stale or missing view after a reconcile pass.

    Hash-gated by buckets_needing_view: a bucket whose core set is unchanged
    stays 'fresh' and is skipped, so the LLM synthesis stays low-frequency.
    Synthesis errors fall back to the deterministic template inside
    synthesize_view, so one bad LLM call never aborts the batch."""
    results: list[mvs.SynthesizedView] = []
    for owner_scope, visibility_scope, view_type in mvs.buckets_needing_view():
        synthesizer = make_llm_synthesizer(client, model, view_type, trace_context=trace_context)
        results.append(
            mvs.synthesize_view(owner_scope, visibility_scope, view_type, synthesizer=synthesizer)
        )
    for soul_name in srm.souls_needing_view():
        synthesizer = make_llm_synthesizer(
            client,
            model,
            mvs.VIEW_SOUL_RELATIONSHIP,
            trace_context={
                **(trace_context or {}),
                "soul_name": soul_name,
            },
        )
        results.append(
            srm.refresh_relationship_memory(
                soul_name,
                synthesizer=synthesizer,
            )
        )
    return results
