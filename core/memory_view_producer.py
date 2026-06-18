"""Bridges memory_view_service's synthesizer seam to the reflection_router LLM.

memory_view_service.synthesize_view accepts a ``synthesizer(units, char_budget)``
callable and falls back to its deterministic template on None/error. This module
formats core units into prompt text and calls the LLM synthesis prompt, keeping
memory_view_service LLM-free.
"""

from __future__ import annotations

import sqlite3

from core.llm import reflection_router
from core.llm.types import LLMClient


def _format_units(units: list[sqlite3.Row]) -> str:
    if not units:
        return ""
    parts = []
    for unit in units:
        parts.append(
            f"- [{unit['type']}] {unit['content']} "
            f"(confidence={unit['confidence']}, tier={unit['tier']})"
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
            units_text=_format_units(units),
            char_budget=char_budget,
            view_type=view_type,
            trace_context=trace_context,
        )

    return synthesizer
