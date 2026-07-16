"""LLM extraction of trackable goal candidates.

This router never creates active goals. It only returns candidates that the
suggestion service may persist for explicit user confirmation.
"""

from __future__ import annotations

import json
from datetime import datetime

from core import time_normalizer
from core.llm import secondary_model
from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


GOAL_ROUTER_PROMPT = """\
你是 TraceLog 拾迹的 Goal Router。请从用户本轮输入中识别“值得正式追踪、且用户已表达承诺”的目标候选。

你只能输出一个标准 JSON 对象，不要输出 Markdown 或解释：

{
  "goals": [
    {
      "title": "简洁、可追踪的目标标题",
      "detail": "必要的范围或成功标准；没有则为 null",
      "horizon": "short|long",
      "confidence": 0.0
    }
  ]
}

严格规则：
1. 这里只提议，不代表目标已经成立；用户确认前绝不能进入 active goals。
2. 目标必须是“可持续追踪的结果或长期承诺”：要么有可衡量的成功标准（分数、名次、证书、作品产出等），要么是需要跨越数天以上、反复推进的持续投入。例如“我决定考研”“这学期把 GPA 提到 3.7”“坚持每天背单词，备考法语四级”。
3. 单次、有具体时间点的行动、约定、出席、打卡、提醒，属于一次性事件而非目标——即使内容关乎学习、锻炼或复习，也绝不能输出为目标。例如“明早八点到图书馆复习法语”“周五前交报告”“下午三点开会”都只是单次事件。判断要点：如果它是“某个时刻去做某件具体的事”，就不是目标；只有“想达成的结果”或“要长期坚持的事”才是目标。
4. 随口愿望、兴趣、幻想、情绪或泛泛方向也不是目标，例如“有点想做游戏”“以后也许学日语”；不要为了凑数输出。
5. short 通常在数天到数月内持续推进；long 通常跨学期、跨年度或更久。单个时间点的事件不构成任何 horizon。
6. title 不要加入“用户想要”等套话，直接写目标本身。
7. detail 只写范围、成功标准或推进方式等中性信息；不要复述隐私性细节（保密状态、家人是否知情、人际关系隐情等）——目标对所有 AI 伙伴可见。
8. 一次输入最多输出 3 个，宁缺毋滥；没有可靠候选时输出空数组。
9. confidence ∈ [0,1]，低于 0.65 的候选不要输出。

当前时间：
{current_datetime}
若下方提供了「时间标注」：带「＝」的精确标注采用其主日期（＝号后的第一个日期）；带「≈」的模糊标注必须保留原有精度，不得擅自写成某一天。无标注的相对时间才以当前时间为基准换算。
"""


def call_goal_router(
    client: LLMClient,
    model: str,
    *,
    user_input: str,
    context: str = "",
    trace_context: dict | None = None,
) -> list[dict]:
    client, model = secondary_model.resolve(client, model)
    content = (
        f"## 场景上下文\n\n{context.strip() or '（无）'}\n\n"
        "---\n\n"
        f"## 用户本轮输入\n\n{user_input.strip()}"
    )
    note = time_normalizer.annotation_note(user_input, anchor=datetime.now().astimezone())
    if note:
        content += f"\n\n## 时间标注（系统按说话时刻计算）\n{note}"
    data = call_json_completion(
        client=client,
        model=model,
        operation="goal_router",
        timeout=30,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": GOAL_ROUTER_PROMPT.replace("{current_datetime}", now_str())},
            {"role": "user", "content": content},
        ],
        parser=_parse_goal_router_content,
        trace_context=trace_context,
    )
    return data.get("goals", []) if isinstance(data, dict) else []


def _parse_goal_router_content(content: str | None) -> dict | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_goals = data.get("goals")
    if not isinstance(raw_goals, list):
        raw_goals = []
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
    return {"goals": goals}


def _coerce_confidence(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, result))
