"""LLM calls for memory reconcile, evidence re-link, and view synthesis."""

from __future__ import annotations

import json

from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


MEMORY_RECONCILE_PROMPT = """\
你是 TraceLog 拾迹的记忆对账引擎。你只维护关于【用户】的结构化 memory units。

## 主体边界
- unit 的主语必须是用户本人，或用户与某个 AI 人格的关系、约定和边界。
- AI 人格自身的设定、情绪、经历和偏好不是用户记忆，禁止写入。
- assistant 消息只能帮助理解上下文，不能单独成为证据。

## 输入
- 场景：公开帖子、评论互动或私聊。
- 新证据事件：当前批次唯一可供 add 引用的用户证据，每条有 event_id。
- 已有 units：可被 retain / confirm / revise / retract。
- challenged unit 会附当前有效 evidence；每个 challenged unit 必须恰好得到一个决定。
- tombstones：false 禁止再次生成同义 unit；outdated 只有出现新证据时才能重新成立。

## 回想价值
只有未来对理解用户仍有价值的信息才值得成为 unit。瞬时琐事（正在上课、刚吃饭、等公交）
不应记录。身份、长期目标、稳定偏好、重要关系和持续数天以上的处境可以记录。

## 输出
只输出 JSON：
{
  "summary": "一句话摘要",
  "ops": [
    {"op":"add","type":"identity|preference|state|relationship|insight|freeform","content":"陈述","confidence":0.0,"tier":"core|contextual|episodic","importance":0.0,"evidence_event_ids":[1]},
    {"op":"retain","target_id":"mu_x"},
    {"op":"confirm","target_id":"mu_x","evidence_event_ids":[1],"confidence":0.0},
    {"op":"revise","target_id":"mu_x","content":"新陈述","evidence_event_ids":[1]},
    {"op":"retract","target_id":"mu_x","reason":"false|outdated"}
  ]
}

## 硬规则
1. 不得编造 event_id 或 target_id；add 至少引用一条本批新证据。
2. add 应是可复用的抽象，不是逐条转写。明确且持续有效的用户自述可由单条证据成立。
3. importance < 0.3 的瞬时事实不要产出；短期 state 不得设为 core。
4. 正式 goal 由目标系统管理；这里只记录与理解用户有关的倾向或持续处境。
5. challenged unit：剩余证据完整支持用 retain；新版本支持用 confirm；需改写用 revise；
   已不支持用 retract。confirm/revise 必须引用其当前有效 evidence。
6. thread/private 场景应识别稳定称呼、互动约定、回应偏好、语气、边界和默契。
7. 没有可靠增量时返回空 ops。宁缺毋滥。

当前时间：{current_datetime}
"""


MEMORY_RELINK_PROMPT = """\
用户刚修改了一条关于自己的记忆。逐条判断旧证据是否仍支持新内容。
每条证据必须恰好出现在 keep_event_ids 或 drop_event_ids 之一。
只输出 JSON：{"keep_event_ids":[整数],"drop_event_ids":[整数]}
"""


MEMORY_VIEW_SYNTH_PROMPT = """\
把给定的核心 memory units 综合成稳定、克制、有界的用户画像。
只能使用输入 units，不得脑补；不要把短期状态夸大为长期身份；压在字数预算内。
输出连贯 prose，不要标题或逐条罗列。只输出 JSON：{"profile_md":"画像"}
当前时间：{current_datetime}
"""


SOUL_RELATIONSHIP_VIEW_SYNTH_PROMPT = """\
把给定的 relationship units 综合成这个 SOUL 与用户的相处叙事。
只能使用输入 units，不得新增共同经历；重点表达称呼、节奏、回应偏好、边界和默契。
不要重复普通身份画像；压在字数预算内。只输出 JSON：{"profile_md":"关系叙事"}
当前时间：{current_datetime}
"""


def call_memory_reconcile(
    client: LLMClient,
    model: str,
    *,
    boundary_text: str,
    events_text: str,
    active_units_text: str,
    tombstones_text: str,
    trace_context: dict | None = None,
) -> dict | None:
    user_content = (
        f"## 场景\n\n{boundary_text}\n\n---\n\n"
        f"## 新证据事件\n\n{events_text or '（无）'}\n\n---\n\n"
        f"## 已有 units\n\n{active_units_text or '（无）'}\n\n---\n\n"
        f"## tombstones\n\n{tombstones_text or '（无）'}"
    )
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_reconcile",
        timeout=45,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": MEMORY_RECONCILE_PROMPT.replace(
                    "{current_datetime}", now_str()
                ),
            },
            {"role": "user", "content": user_content},
        ],
        parser=_parse_memory_reconcile_content,
        trace_context=trace_context,
    )


_RECONCILE_OPS = {"add", "retain", "confirm", "revise", "retract"}
_RECONCILE_TYPES = {
    "identity",
    "preference",
    "state",
    "relationship",
    "insight",
    "freeform",
}
_RECONCILE_TIERS = {"core", "contextual", "episodic"}


def _coerce_event_ids(value) -> list[int]:
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def _coerce_float(value, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, result))


def _parse_memory_reconcile_content(content: str | None) -> dict | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    raw_ops = data.get("ops")
    if not isinstance(raw_ops, list):
        raw_ops = []
    ops: list[dict] = []
    for item in raw_ops:
        if not isinstance(item, dict) or item.get("op") not in _RECONCILE_OPS:
            continue
        op = str(item["op"])
        normalized: dict = {
            "op": op,
            "evidence_event_ids": _coerce_event_ids(item.get("evidence_event_ids")),
        }
        if op == "add":
            unit_type = item.get("type")
            normalized["type"] = (
                unit_type if unit_type in _RECONCILE_TYPES else "insight"
            )
            normalized["content"] = str(item.get("content") or "").strip()
            normalized["confidence"] = _coerce_float(item.get("confidence"), 0.6)
            tier = item.get("tier")
            normalized["tier"] = (
                tier if tier in _RECONCILE_TIERS else "contextual"
            )
            normalized["importance"] = _coerce_float(item.get("importance"), 0.5)
        else:
            normalized["target_id"] = str(item.get("target_id") or "")
            if op == "revise":
                normalized["content"] = str(item.get("content") or "").strip()
                if item.get("type") in _RECONCILE_TYPES:
                    normalized["type"] = item["type"]
                if item.get("tier") in _RECONCILE_TIERS:
                    normalized["tier"] = item["tier"]
            if op in {"confirm", "revise"} and item.get("confidence") is not None:
                normalized["confidence"] = _coerce_float(
                    item.get("confidence"), 0.6
                )
            if op == "confirm" and item.get("importance") is not None:
                normalized["importance"] = _coerce_float(
                    item.get("importance"), 0.5
                )
            if op == "retract":
                reason = item.get("reason")
                normalized["reason"] = (
                    reason if reason in {"false", "outdated"} else None
                )
        ops.append(normalized)
    summary = data.get("summary")
    return {
        "ops": ops,
        "summary": summary.strip() if isinstance(summary, str) else "",
    }


def call_memory_relink(
    client: LLMClient,
    model: str,
    *,
    content: str,
    evidence_text: str,
    trace_context: dict | None = None,
) -> dict | None:
    user_content = (
        f"## 记忆的新内容\n\n{content}\n\n---\n\n"
        f"## 旧证据\n\n{evidence_text or '（无）'}"
    )
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_relink",
        timeout=45,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": MEMORY_RELINK_PROMPT},
            {"role": "user", "content": user_content},
        ],
        parser=_parse_memory_relink_content,
        trace_context=trace_context,
    )


def _parse_memory_relink_content(content: str | None) -> dict | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "keep_event_ids": _coerce_event_ids(data.get("keep_event_ids")),
        "drop_event_ids": _coerce_event_ids(data.get("drop_event_ids")),
    }


def call_view_synthesis(
    client: LLMClient,
    model: str,
    *,
    units_text: str,
    char_budget: int,
    view_type: str,
    trace_context: dict | None = None,
) -> str | None:
    user_content = (
        f"## 画像类型\n\n{view_type}\n\n"
        f"## 字数预算\n\n不超过 {char_budget} 字\n\n---\n\n"
        f"## 核心记忆单元\n\n{units_text or '（无）'}"
    )
    prompt = (
        SOUL_RELATIONSHIP_VIEW_SYNTH_PROMPT
        if view_type == "soul_relationship_memory"
        else MEMORY_VIEW_SYNTH_PROMPT
    )
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_view_synthesis",
        timeout=45,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": prompt.replace("{current_datetime}", now_str()),
            },
            {"role": "user", "content": user_content},
        ],
        parser=_parse_view_synthesis_content,
        trace_context=trace_context,
    )


def _parse_view_synthesis_content(content: str | None) -> str | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    profile_md = data.get("profile_md")
    if not isinstance(profile_md, str) or not profile_md.strip():
        return None
    return profile_md.strip()
