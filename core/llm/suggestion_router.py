"""LLM extraction of goal and one-off schedule suggestion candidates.

This router never creates goals or events. It only returns candidates that the
suggestion service may persist for explicit user confirmation.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta

from core import time_normalizer
from core.llm import secondary_model
from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


SUGGESTION_ROUTER_PROMPT = """\
你是 TraceLog 拾迹的 Suggestion Router。请从用户本轮输入中识别两类候选：值得正式追踪的目标，以及单次日程事件。

你只能输出一个标准 JSON 对象，不要输出 Markdown 或解释：

{
  "goals": [
    {
      "title": "简洁、可追踪的目标标题",
      "detail": "必要的范围或成功标准；没有则为 null",
      "horizon": "short|long",
      "confidence": 0.0
    }
  ],
  "events": [
    {
      "subject": "中性的日程标题",
      "date": "YYYY-MM-DD",
      "start_time": "HH:MM|null",
      "end_time": "HH:MM|null",
      "all_day": false,
      "confidence": 0.0
    }
  ]
}

目标规则：
1. 这里只提议，不代表目标已经成立；用户确认前绝不能进入 active goals。
2. 目标必须是“可持续追踪的结果或长期承诺”：要么有可衡量的成功标准（分数、名次、证书、作品产出等），要么是需要跨越数天以上、反复推进的持续投入。例如“我决定考研”“这学期把 GPA 提到 3.7”“坚持每天背单词，备考法语四级”。
3. 单次、有具体时间点的行动、约定、出席、打卡、提醒，属于一次性事件而非目标——即使内容关乎学习、锻炼或复习，也绝不能输出为目标。例如“明早八点到图书馆复习法语”“周五前交报告”“下午三点开会”都只是单次事件。判断要点：如果它是“某个时刻去做某件具体的事”，就不是目标；只有“想达成的结果”或“要长期坚持的事”才是目标。
4. 随口愿望、兴趣、幻想、情绪或泛泛方向也不是目标，例如“有点想做游戏”“以后也许学日语”；不要为了凑数输出。
5. short 通常在数天到数月内持续推进；long 通常跨学期、跨年度或更久。单个时间点的事件不构成任何 horizon。
6. title 不要加入“用户想要”等套话，直接写目标本身。
7. detail 只写范围、成功标准或推进方式等中性信息；不要复述隐私性细节（保密状态、家人是否知情、人际关系隐情等）——目标对所有 AI 伙伴可见。

日程规则：
1. events 只收用户已经确定要做的、单次且有可解析日期的事件。目标规则 3 中被排除的单次行动，正是 events 的正例。
2. 相对时间必须按下方「时间标注」换算。若只有带“≈”的模糊标注，或像“改天”一样无法换算到具体日期，不得输出。
3. 已过去的日期或时间点不得输出。
4. 场景上下文若已存在 subject、日期和时间相同的近期日程，不得重复输出。
5. 随口一提、尚未决定、属于他人的事情不得输出。
6. subject 使用中性、简洁表述，不复述隐私细节——日程对所有 AI 伙伴可见。

共同规则：
1. goals 与 events 各自最多输出 3 个，宁缺毋滥；没有可靠候选时输出空数组。
2. confidence ∈ [0,1]，低于 0.65 的候选不要输出。

当前时间：
{current_datetime}
若下方提供了「时间标注」：带「＝」的精确标注采用其主日期（＝号后的第一个日期）；带「≈」的模糊标注不能擅自写成某一天。无标注的相对时间才以当前时间为基准换算。
"""

_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def call_suggestion_router(
    client: LLMClient,
    model: str,
    *,
    user_input: str,
    context: str = "",
    trace_context: dict | None = None,
) -> dict[str, list[dict]]:
    client, model = secondary_model.resolve(client, model)
    anchor = datetime.now().astimezone()
    content = (
        f"## 场景上下文\n\n{context.strip() or '（无）'}\n\n"
        "---\n\n"
        f"## 用户本轮输入\n\n{user_input.strip()}"
    )
    note = time_normalizer.annotation_note(user_input, anchor=anchor)
    if note:
        content += f"\n\n## 时间标注（系统按说话时刻计算）\n{note}"
    data = call_json_completion(
        client=client,
        model=model,
        operation="suggestion_router",
        timeout=30,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": SUGGESTION_ROUTER_PROMPT.replace(
                    "{current_datetime}", now_str()
                ),
            },
            {"role": "user", "content": content},
        ],
        parser=lambda value: _parse_suggestion_router_content(value, now=anchor),
        trace_context=trace_context,
    )
    if not isinstance(data, dict):
        return {"goals": [], "events": []}
    return {
        "goals": data.get("goals", []),
        "events": data.get("events", []),
    }


def _parse_suggestion_router_content(
    content: str | None,
    *,
    now: datetime | None = None,
) -> dict | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "goals": _parse_goals(data.get("goals")),
        "events": _parse_events(data.get("events"), now=now),
    }


def _parse_goals(raw_goals: object) -> list[dict]:
    if not isinstance(raw_goals, list):
        return []
    goals: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_goals:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        horizon = item.get("horizon")
        if not isinstance(title, str) or not title.strip() or horizon not in {"short", "long"}:
            continue
        confidence = _coerce_confidence(item.get("confidence"))
        if confidence < 0.65:
            continue
        detail = item.get("detail")
        if detail is not None and not isinstance(detail, str):
            detail = None
        key = (title.strip().casefold(), horizon)
        if key in seen:
            continue
        seen.add(key)
        goals.append(
            {
                "title": title.strip(),
                "detail": detail.strip() if isinstance(detail, str) and detail.strip() else None,
                "horizon": horizon,
                "confidence": confidence,
            }
        )
        if len(goals) >= 3:
            break
    return goals


def _parse_events(raw_events: object, *, now: datetime | None = None) -> list[dict]:
    if not isinstance(raw_events, list):
        return []
    anchor = now or datetime.now().astimezone()
    if anchor.tzinfo is None:
        anchor = anchor.astimezone()
    events: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject")
        event_date = _parse_date(item.get("date"))
        all_day = item.get("all_day")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or event_date is None
            or not isinstance(all_day, bool)
        ):
            continue
        start_time = _parse_time(item.get("start_time"))
        end_time = _parse_time(item.get("end_time"))
        if item.get("start_time") is not None and start_time is None:
            continue
        if item.get("end_time") is not None and end_time is None:
            continue
        if all_day and (start_time is not None or end_time is not None):
            continue
        if start_time is None and end_time is not None:
            continue
        if start_time is not None and end_time is not None and end_time <= start_time:
            continue
        confidence = _coerce_confidence(item.get("confidence"))
        if confidence < 0.65 or _event_has_expired(
            event_date,
            start_time=start_time,
            end_time=end_time,
            all_day=all_day,
            now=anchor,
        ):
            continue
        key = (subject.strip().casefold(), event_date.isoformat(), _format_time(start_time))
        if key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "subject": subject.strip(),
                "date": event_date.isoformat(),
                "start_time": _format_time(start_time),
                "end_time": _format_time(end_time),
                "all_day": all_day,
                "confidence": confidence,
            }
        )
        if len(events) >= 3:
            break
    return events


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value) if len(value) == 10 else None
    except ValueError:
        return None


def _parse_time(value: object) -> time | None:
    if value is None:
        return None
    if not isinstance(value, str) or _TIME_PATTERN.fullmatch(value) is None:
        return None
    return time.fromisoformat(value)


def _format_time(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def _event_has_expired(
    event_date: date,
    *,
    start_time: time | None,
    end_time: time | None,
    all_day: bool,
    now: datetime,
) -> bool:
    if all_day or start_time is None:
        expires_at = datetime.combine(event_date, time.max, now.tzinfo)
    elif end_time is None:
        expires_at = datetime.combine(event_date, start_time, now.tzinfo) + timedelta(hours=1)
    else:
        expires_at = datetime.combine(event_date, end_time, now.tzinfo)
    return expires_at < now


def _coerce_confidence(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, result))
