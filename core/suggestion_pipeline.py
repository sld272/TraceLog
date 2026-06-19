"""Best-effort LLM candidate extraction for reply paths."""

from __future__ import annotations

import os
from typing import Any

from core import goal_service, logging_service, suggestion_service
from core.llm import goal_router, todo_router
from core.llm.types import LLMClient

GOAL_SUGGESTIONS_ENABLED_ENV = "GOAL_SUGGESTIONS_ENABLED"
TODO_SUGGESTIONS_ENABLED_ENV = "TODO_SUGGESTIONS_ENABLED"


def goal_suggestions_enabled() -> bool:
    value = os.environ.get(GOAL_SUGGESTIONS_ENABLED_ENV, "0").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def todo_suggestions_enabled() -> bool:
    value = os.environ.get(TODO_SUGGESTIONS_ENABLED_ENV, "0").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def collect_reply_suggestions(
    *,
    user_input: str,
    evidence_ref: str,
    client: LLMClient,
    model: str,
    context: str = "",
    trace_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run enabled suggestion extractors and return one inline list."""
    goals = collect_goal_suggestions(
        user_input=user_input,
        evidence_ref=evidence_ref,
        client=client,
        model=model,
        context=context,
        trace_context=trace_context,
    )
    todos = collect_todo_suggestions(
        user_input=user_input,
        evidence_ref=evidence_ref,
        client=client,
        model=model,
        context=context,
        trace_context=trace_context,
    )
    return [*goals, *todos]


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


def collect_todo_suggestions(
    *,
    user_input: str,
    evidence_ref: str,
    client: LLMClient,
    model: str,
    context: str = "",
    trace_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract todo changes from any reply input and persist them as suggestions."""
    if not todo_suggestions_enabled():
        return []
    body = str(user_input or "").strip()
    if not body:
        return []
    try:
        from core import todo_service

        active = todo_service.list_active_todos()
        active_text = (
            "\n".join(
                todo_service.format_todo_for_context(todo, include_status=True)
                for todo in active
            )
            or "（暂无）"
        )
        source_text = (
            "---\n"
            f"source: {evidence_ref}\n"
            f"context: {context or 'conversation'}\n"
            "---\n\n"
            f"{body}"
        )
        data = todo_router.call_todo_tool(
            client=client,
            model=model,
            post=source_text,
            active_todos=active_text,
            trace_context=trace_context,
        )
        if data is None:
            return []
        return persist_todo_candidates(data, evidence_ref=evidence_ref)
    except Exception as exc:
        logging_service.log_event(
            "suggestion_extraction_failed",
            level="WARNING",
            kind="todo",
            evidence_ref=evidence_ref,
            error=str(exc),
            **(trace_context or {}),
        )
        return []


def persist_todo_candidates(
    data: dict[str, Any],
    *,
    evidence_ref: str,
) -> list[dict[str, Any]]:
    """Convert TodoTool create/update/delete output into pending suggestions."""
    from core import todo_service

    existing = {todo["id"]: todo for todo in todo_service.load_todos()}
    suggestions: list[dict[str, Any]] = []
    for item in data.get("todos_to_upsert", []):
        if not isinstance(item, dict):
            continue
        todo_id = item.get("id")
        action = "update" if todo_id and str(todo_id) in existing else "create"
        payload = {
            "action": action,
            "todo_id": str(todo_id) if action == "update" else None,
            "task": item.get("task"),
            "date": item.get("date"),
            "start_time": item.get("start_time"),
            "end_time": item.get("end_time"),
            "status": item.get("status", "未完成"),
        }
        suggestion = suggestion_service.create_suggestion(
            "todo",
            payload,
            evidence_ref,
            0.8,
        )
        if suggestion is not None and suggestion["status"] == "pending":
            suggestions.append(suggestion)

    for item in data.get("todos_to_delete", []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        todo_id = str(item["id"])
        current = existing.get(todo_id)
        if current is None:
            continue
        suggestion = suggestion_service.create_suggestion(
            "todo",
            {
                "action": "delete",
                "todo_id": todo_id,
                "task": current["task"],
                "date": current.get("date"),
                "start_time": current.get("start_time"),
                "end_time": current.get("end_time"),
                "status": current.get("status"),
            },
            evidence_ref,
            0.8,
        )
        if suggestion is not None and suggestion["status"] == "pending":
            suggestions.append(suggestion)
    return suggestions
