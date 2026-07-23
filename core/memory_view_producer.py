"""Bridges memory_view_service's synthesizer seam to the memory LLM router.

memory_view_service.synthesize_view accepts a ``synthesizer(units, char_budget)``
callable and falls back to its deterministic template on None/error. This module
formats core units into prompt text and calls the LLM synthesis prompt, keeping
memory_view_service LLM-free.
"""

from __future__ import annotations

import sqlite3

from core import memory_view_service as mvs, soul_relationship_memory as srm
from core.llm import memory_router
from core.llm.types import LLMClient

# 闸1：数据量门槛。核心单元少于这么多时不调 LLM 合成，直接交给确定性模板——
# 证据稀薄时模型会虚构事件、抒情铺陈，不如逐条列举来得诚实。
MIN_UNITS_FOR_LLM = 4


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
        # [id=...] anchors each unit so the synthesis prompt can cite it and the
        # parser can verify every paragraph is grounded in a real, offered unit.
        parts.append(
            f"- [id={unit['id']}] [{unit['type']}] {unit['content']} "
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
        # 闸1：证据稀薄时不走 LLM，让 synthesize_view 回落确定性模板。
        if len(units) < MIN_UNITS_FOR_LLM:
            return None
        unit_contents = {
            str(unit["id"]): str(unit["content"] or "").strip()
            for unit in units
        }
        return memory_router.call_view_synthesis(
            client,
            model,
            units_text=_format_units(units, view_type),
            char_budget=char_budget,
            view_type=view_type,
            unit_contents=unit_contents,
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

    Each view module owns its own refresh enumeration. Hash-gated views whose
    selected unit set is unchanged stay fresh and are skipped. Synthesis errors
    fall back to deterministic templates."""
    results: list[mvs.SynthesizedView] = []
    for owner_scope, visibility_scope, view_type in mvs.per_bucket_views_needing_refresh():
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
