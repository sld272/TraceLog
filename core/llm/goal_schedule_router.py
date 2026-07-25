"""Batch LLM classification for schedule-event-to-goal relationships."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from core.llm import secondary_model
from core.llm.common import call_json_completion, clean_json_content
from core.llm.types import LLMClient


GOAL_SCHEDULE_ROUTER_PROMPT = """\
你是 TraceLog 拾迹的日程目标关联判定器。请判断每条日程的 subject 是否明确服务于某个进行中的目标。

输入会一次提供多个 events 和全部 active goals。你必须为每个 event_id 恰好返回一条 decision；没有关联时 matches 必须是空数组。

只输出标准 JSON 对象，不要输出 Markdown 或解释：

{
  "decisions": [
    {
      "event_id": "event-id",
      "matches": [
        {
          "goal_id": "g_xxx",
          "confidence": 0.0
        }
      ]
    }
  ]
}

规则：
1. 依据语义判断，不要求字面重叠。例如“科目一”“驾考体检”明确服务于“考驾照”。
2. 只有关系明确时才输出 match。一般生活、课程或医疗事项不能因为宽泛相关就硬套目标。
3. event_id 和 goal_id 只能逐字使用输入中出现的值，不得臆造。
4. confidence 表示关联确定度，范围 0 到 1。低于 0.7 的候选也可以输出，由系统统一执行阈值。
5. 同一日程可以关联多个目标，但每一条都必须分别有直接、明确的关系。
"""


def call_goal_schedule_router(
    client: LLMClient,
    model: str,
    *,
    events: Sequence[Mapping[str, Any]],
    goals: Sequence[Mapping[str, Any]],
    trace_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Classify all supplied events in one secondary-model JSON call."""
    routed_client, routed_model = secondary_model.resolve(client, model)
    if routed_client is None or not routed_model:
        return None
    event_payload = [
        {
            "event_id": str(event["id"]),
            "subject": str(event.get("subject") or ""),
            "series_master_id": (
                str(event["series_master_id"])
                if event.get("series_master_id") is not None
                else None
            ),
        }
        for event in events
    ]
    goal_payload = [
        {
            "goal_id": str(goal["id"]),
            "title": str(goal.get("title") or ""),
            "detail": (
                str(goal["detail"]) if goal.get("detail") is not None else None
            ),
            "horizon": str(goal.get("horizon") or ""),
        }
        for goal in goals
    ]
    known_event_ids = {item["event_id"] for item in event_payload}
    known_goal_ids = {item["goal_id"] for item in goal_payload}
    data = call_json_completion(
        client=routed_client,
        model=routed_model,
        operation="goal_schedule_router",
        timeout=30,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": GOAL_SCHEDULE_ROUTER_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"events": event_payload, "active_goals": goal_payload},
                    ensure_ascii=False,
                ),
            },
        ],
        parser=lambda value: _parse_goal_schedule_content(
            value,
            known_event_ids=known_event_ids,
            known_goal_ids=known_goal_ids,
        ),
        trace_context={
            "channel": "schedule_maintenance",
            "event_count": len(event_payload),
            "goal_count": len(goal_payload),
            **(trace_context or {}),
        },
    )
    if not isinstance(data, dict):
        return None
    decisions = data.get("decisions")
    return decisions if isinstance(decisions, list) else None


def _parse_goal_schedule_content(
    content: str | None,
    *,
    known_event_ids: set[str],
    known_goal_ids: set[str],
) -> dict[str, list[dict[str, Any]]] | None:
    cleaned = clean_json_content(content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return None

    decisions: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for raw_decision in data["decisions"]:
        # For example, {"event_id": "event-1", "matches": [{"goal_id":
        # "invented"}]} is dropped as a whole so event-1 remains retryable.
        if not isinstance(raw_decision, dict):
            continue
        event_id = raw_decision.get("event_id")
        raw_matches = raw_decision.get("matches")
        if (
            not isinstance(event_id, str)
            or event_id not in known_event_ids
            or event_id in seen_event_ids
            or not isinstance(raw_matches, list)
        ):
            continue

        matches: list[dict[str, Any]] = []
        invalid_match = False
        best_by_goal: dict[str, float] = {}
        for raw_match in raw_matches:
            if not isinstance(raw_match, dict):
                invalid_match = True
                break
            goal_id = raw_match.get("goal_id")
            confidence = _confidence(raw_match.get("confidence"))
            if (
                not isinstance(goal_id, str)
                or goal_id not in known_goal_ids
                or confidence is None
            ):
                invalid_match = True
                break
            best_by_goal[goal_id] = max(confidence, best_by_goal.get(goal_id, 0.0))
        if invalid_match:
            continue
        for goal_id, confidence in best_by_goal.items():
            matches.append({"goal_id": goal_id, "confidence": confidence})
        seen_event_ids.add(event_id)
        decisions.append({"event_id": event_id, "matches": matches})
    return {"decisions": decisions}


def _confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0.0 <= result <= 1.0 else None
