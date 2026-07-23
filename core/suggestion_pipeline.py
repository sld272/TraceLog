"""Best-effort LLM candidate extraction for reply paths."""

from __future__ import annotations

import os
from typing import Any

from core import goal_service, logging_service, suggestion_service
from core.llm import suggestion_router
from core.llm.types import LLMClient

GOAL_SUGGESTIONS_ENABLED_ENV = "GOAL_SUGGESTIONS_ENABLED"
SCHEDULE_SUGGESTIONS_ENABLED_ENV = "SCHEDULE_SUGGESTIONS_ENABLED"


def goal_suggestions_enabled() -> bool:
    value = os.environ.get(GOAL_SUGGESTIONS_ENABLED_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def schedule_suggestions_enabled() -> bool:
    value = os.environ.get(SCHEDULE_SUGGESTIONS_ENABLED_ENV, "1").strip().lower()
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
    """Extract and persist enabled suggestion kinds without breaking replies."""
    goal_enabled = goal_service.goal_tool_enabled() and goal_suggestions_enabled()
    schedule_enabled = schedule_suggestions_enabled()
    if not goal_enabled and not schedule_enabled:
        return []
    body = str(user_input or "").strip()
    if not body:
        return []
    try:
        candidates = suggestion_router.call_suggestion_router(
            client,
            model,
            user_input=body,
            context=context,
            trace_context=trace_context,
        )
    except Exception as exc:
        logging_service.log_event(
            "suggestion_extraction_failed",
            level="WARNING",
            kind="goal,schedule",
            evidence_ref=evidence_ref,
            error=str(exc),
            **(trace_context or {}),
        )
        return []

    suggestions: list[dict[str, Any]] = []
    if goal_enabled:
        suggestions.extend(
            _persist_goal_candidates(
                candidates.get("goals", []),
                evidence_ref=evidence_ref,
                trace_context=trace_context,
            )
        )
    if schedule_enabled:
        suggestions.extend(
            _persist_schedule_candidates(
                candidates.get("events", []),
                evidence_ref=evidence_ref,
                trace_context=trace_context,
            )
        )
    return suggestions


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
        candidates = suggestion_router.call_suggestion_router(
            client,
            model,
            user_input=body,
            context=context,
            trace_context=trace_context,
        )
        return _persist_goal_candidates(
            candidates.get("goals", []),
            evidence_ref=evidence_ref,
            trace_context=trace_context,
        )
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


def _persist_goal_candidates(
    candidates: list[dict[str, Any]],
    *,
    evidence_ref: str,
    trace_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    try:
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


def _persist_schedule_candidates(
    candidates: list[dict[str, Any]],
    *,
    evidence_ref: str,
    trace_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    try:
        suggestions: list[dict[str, Any]] = []
        for candidate in candidates:
            suggestion = suggestion_service.create_suggestion(
                "schedule",
                {
                    "subject": candidate["subject"],
                    "date": candidate["date"],
                    "start_time": candidate.get("start_time"),
                    "end_time": candidate.get("end_time"),
                    "all_day": candidate["all_day"],
                    "goal_id": None,
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
            kind="schedule",
            evidence_ref=evidence_ref,
            error=str(exc),
            **(trace_context or {}),
        )
        return []
