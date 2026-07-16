"""Best-effort LLM candidate extraction for reply paths."""

from __future__ import annotations

import os
from typing import Any

from core import goal_service, logging_service, suggestion_service
from core.llm import goal_router
from core.llm.types import LLMClient

GOAL_SUGGESTIONS_ENABLED_ENV = "GOAL_SUGGESTIONS_ENABLED"


def goal_suggestions_enabled() -> bool:
    value = os.environ.get(GOAL_SUGGESTIONS_ENABLED_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def collect_reply_suggestions(
    *,
    user_input: str,
    evidence_ref: str,
    client: LLMClient,
    model: str,
    context: str = "",
    trace_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract goal suggestions for a reply without breaking the reply flow."""
    return collect_goal_suggestions(
        user_input=user_input,
        evidence_ref=evidence_ref,
        client=client,
        model=model,
        context=context,
        trace_context=trace_context,
    )


def collect_goal_suggestions(
    *,
    user_input: str,
    evidence_ref: str,
    client: LLMClient,
    model: str,
    context: str = "",
    trace_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract and persist goal suggestions without breaking the reply flow."""
    if not goal_service.goal_tool_enabled() or not goal_suggestions_enabled():
        return []
    body = str(user_input or "").strip()
    if not body:
        return []
    try:
        candidates = goal_router.call_goal_router(
            client,
            model,
            user_input=body,
            context=context,
            trace_context=trace_context,
        )
        suggestions: list[dict[str, Any]] = []
        for candidate in candidates:
            suggestion = suggestion_service.create_suggestion(
                "goal",
                {
                    "title": candidate["title"],
                    "detail": candidate.get("detail"),
                    "horizon": candidate["horizon"],
                    "focus": candidate["horizon"] == "short",
                },
                evidence_ref,
                candidate.get("confidence", 0.6),
            )
            if suggestion is not None and suggestion["status"] == "pending":
                suggestions.append(suggestion)
        return suggestions
    except Exception as exc:
        logging_service.log_event(
            "suggestion_extraction_failed",
            level="WARNING",
            kind="goal",
            evidence_ref=evidence_ref,
            error=str(exc),
            **(trace_context or {}),
        )
        return []
