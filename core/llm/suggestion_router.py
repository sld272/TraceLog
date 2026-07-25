"""LLM extraction of goal, schedule and goal-activity candidates.

This router never writes application state. It returns goal and event candidates
for explicit confirmation, plus reversible activity candidates for existing goals.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from core import time_normalizer
from core.llm import secondary_model
from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


SUGGESTION_ROUTER_PROMPT = """\
你是 TraceLog 拾迹的 Suggestion Router。请从用户本轮输入中同时识别三类候选：值得正式追踪的目标、单次日程事件，以及已有目标的具体动态。

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
  ],
  "activities": [
    {
      "goal_id": "g_xxx",
      "kind": "commitment|progress|blocked|milestone",
      "evidence_span": "帖子里的原句片段",
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

目标动态规则：
1. activities 只能指向「场景上下文」中「当前目标」列表里真实出现的 goal_id，必须原样填写方括号里的完整 id；不得臆造 id，也不得把没有对应目标的内容硬凑到某个目标。
2. 只判断与该目标直接相关的具体行动、承诺、受阻或结果。纯情绪、自我评价、与该目标无关领域的事情不输出。例如“放假好爽”“感觉我真是超级低精力人群”都不是目标动态；法语专业课的考试也不是“跨专业考研（计算机方向）”的动态。
3. evidence_span 必填，且必须是「用户本轮输入」中真实出现的连续原文片段，不能转述、概括或拼接，控制在 40 字以内。没有可直接引用的原句，就不要输出这条候选。
4. kind 严格按是否已经发生区分：
   - commitment：说要做、尚未发生，例如“明天一定要早点起，十点开始背法语”。
   - progress：实际已经做了，例如“最近每天刷科目一的题”。
   - blocked：没做到或遇到阻碍，例如“今天十一点才起床”“卡在第三题”。
   - milestone：阶段性达成，例如“科一过了”“考完了”。
5. scheduled 不属于模型可输出的 kind；它只由日程来源产生。
6. activities 最多输出 3 条；同一个目标在本轮最多输出一条最能代表具体事实的动态。

共同规则：
1. goals、events 与 activities 都必须返回数组；没有可靠候选时返回空数组。
2. goals 与 events 各自最多输出 3 个，confidence 低于 0.65 的候选不要输出。
3. activities 最多输出 3 个，confidence 低于 0.5 的候选不要输出。这里刻意比 goals/events 宽松，不要把两类阈值统一。

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
    status_callback: Callable[[dict], None] | None = None,
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
        parser=lambda value: _parse_suggestion_router_content(
            value, now=anchor, user_input=user_input
        ),
        trace_context=trace_context,
        status_callback=status_callback,
    )
    if not isinstance(data, dict):
        return {"goals": [], "events": [], "activities": []}
    return {
        "goals": data.get("goals", []),
        "events": data.get("events", []),
        "activities": data.get("activities", []),
    }


def _parse_suggestion_router_content(
    content: str | None,
    *,
    now: datetime | None = None,
    user_input: str = "",
) -> dict | None:
    """Parse one router response. ``user_input`` is the text the quotes must come
    from; an empty one drops every activity, since no quote can be verified."""
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
        "activities": _parse_activities(data.get("activities"), user_input=user_input),
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


def _span_is_verbatim(span: str, user_input: str) -> bool:
    """Whether the quote really is the user's own contiguous wording.

    The prompt asks for a verbatim excerpt, but nothing forces the model to
    comply, and a fabricated quote is the one failure that destroys trust in the
    ledger outright — the UI would show the user a sentence they never wrote, and
    the backfill would put dozens of them on screen at once. Whitespace is
    ignored so harmless reformatting survives; anything else is dropped, taking a
    miss over a fake citation. (Deliberately stricter than the 0.5 confidence
    floor: a false positive is one dismissable row, a false quote is not.)
    """
    if span in user_input:
        return True
    compact = "".join(span.split())
    return bool(compact) and compact in "".join(user_input.split())


def _parse_activities(raw_activities: object, *, user_input: str) -> list[dict]:
    if not isinstance(raw_activities, list):
        return []
    activities: list[dict] = []
    seen_goal_ids: set[str] = set()
    for item in raw_activities:
        if not isinstance(item, dict):
            continue
        goal_id = item.get("goal_id")
        kind = item.get("kind")
        evidence_span = item.get("evidence_span")
        if (
            not isinstance(goal_id, str)
            or not goal_id.strip()
            or kind not in {"commitment", "progress", "blocked", "milestone"}
            or not isinstance(evidence_span, str)
            or not evidence_span.strip()
            or len(evidence_span.strip()) > 40
            or not _span_is_verbatim(evidence_span.strip(), user_input)
        ):
            continue
        confidence = _coerce_confidence(item.get("confidence"))
        if confidence < 0.5:
            continue
        normalized_goal_id = goal_id.strip()
        if normalized_goal_id in seen_goal_ids:
            continue
        seen_goal_ids.add(normalized_goal_id)
        activities.append(
            {
                "goal_id": normalized_goal_id,
                "kind": kind,
                "evidence_span": evidence_span.strip(),
                "confidence": confidence,
            }
        )
        if len(activities) >= 3:
            break
    return activities


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
