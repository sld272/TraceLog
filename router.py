"""
TraceLog 拾迹 — LLM Router
Post Reply: 共情回复 + 待办提取
Deep Reflection: 生成全局深反思
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING

from core.soul_service import SoulContext

if TYPE_CHECKING:
    from openai import OpenAI


# 引擎 1：Post Reply

POST_REPLY_TASK_PROMPT = """\
## 核心任务
1. **回复 (reply)**：结合上下文给出真诚、有温度的中文回应，字数控制在 2-4 句话。
2. **待办提取 (todos_to_upsert & todos_to_delete)**：精准提取用户【明确提出】的新增、状态变更或取消任务。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，绝对不要包含任何 Markdown 代码块格式（如 ```json），也不要有任何前置或后置说明文字。

{
    "reply": "你的回复文字",
  "todos_to_upsert": [
        {
            "id": "已有待办的 id（新增待办时必须为 null）",
            "task": "明确的待办描述",
            "date": "YYYY-MM-DD 或 null",
            "start_time": "HH:MM 或 null",
            "end_time": "HH:MM 或 null",
            "status": "未完成/已完成"
        }
  ],
  "todos_to_delete": [
        {
            "id": "存在于当前待办列表中的确切 id"
        }
  ]
}

## 严格执行规则
1. **增量原则**：只提取本次对话中【新增】或【状态发生变化】的待办。若无变化，相关数组必须输出空数组 []。
2. **更新与删除校验**：如果要更新状态或删除任务，必须先在「待办事项」列表中找到完全对应的 id。若找不到，绝对禁止臆造 id。
3. **新增 id 规则**：新增待办必须输出 "id": null。
4. **不要脑补**：不要把用户情绪、抱怨或你的建议转成待办，除非用户明确表达“我要...”或“提醒我...”。
5. **时间规则**：start_time 与 end_time 使用 24 小时制，仅在用户明确提及时填写，否则置 null。
6. **状态枚举**：status 仅允许 "未完成" 或 "已完成"。

## 上下文结构说明
「当前上下文」可能包含以下区块（部分可能缺席）：
- **近期帖子**：时间线上最近几篇帖子
- **相关帖子**：语义检索到的历史帖子（话题相关，但时间可能较早）
- **待办事项**：当前待完成任务列表

## 当前时间
{current_datetime}
请以此为绝对基准，计算并将“明天、后天、下周三”等相对时间转化为 YYYY-MM-DD。
"""

POST_REPLY_PROMPT = """\
你是 TraceLog 拾迹，一个温暖且有洞察力的个人成长 AI 伴侣。

{task_prompt}
"""

CHAT_REPLY_TASK_PROMPT = """\
## 核心任务
1. **回复 (reply)**：你正在和用户进行一对一私聊。结合上下文给出自然、真诚、贴近该 SOUL 人格的中文回应，字数控制在 2-5 句话。
2. **待办提取 (todos_to_upsert & todos_to_delete)**：精准提取用户【明确提出】的新增、状态变更或取消任务。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，绝对不要包含任何 Markdown 代码块格式，也不要有任何前置或后置说明文字。

{
  "reply": "你的回复文字",
  "todos_to_upsert": [
    {
      "id": "已有待办的 id（新增待办时必须为 null）",
      "task": "明确的待办描述",
      "date": "YYYY-MM-DD 或 null",
      "start_time": "HH:MM 或 null",
      "end_time": "HH:MM 或 null",
      "status": "未完成/已完成"
    }
  ],
  "todos_to_delete": [
    {
      "id": "存在于当前待办列表中的确切 id"
    }
  ]
}

## 严格执行规则
1. **增量原则**：只提取本次私聊消息中【新增】或【状态发生变化】的待办。若无变化，相关数组必须输出空数组 []。
2. **更新与删除校验**：如果要更新状态或删除任务，必须先在「待办事项」列表中找到完全对应的 id。若找不到，绝对禁止臆造 id。
3. **新增 id 规则**：新增待办必须输出 "id": null。
4. **不要脑补**：不要把用户情绪、抱怨或你的建议转成待办，除非用户明确表达“我要...”或“提醒我...”。
5. **时间规则**：start_time 与 end_time 使用 24 小时制，仅在用户明确提及时填写，否则置 null。
6. **状态枚举**：status 仅允许 "未完成" 或 "已完成"。
7. **私聊边界**：私聊是你和用户的单独频道，不要假装其他 SOUL 看得见这段对话。

## 当前时间
{current_datetime}
请以此为绝对基准，计算并将“明天、后天、下周三”等相对时间转化为 YYYY-MM-DD。
"""


def _now_str() -> str:
    now = datetime.now().astimezone()
    weekday = ["一", "二", "三", "四", "五", "六", "日"]
    return now.strftime(f"%Y 年 %m 月 %d 日（周{weekday[now.weekday()]}）%H:%M")


def call_post_reply(user_input: str, client: "OpenAI", model: str, context: str) -> dict | None:
    """Post Reply：共情回复 + 待办增量提取。"""
    system_msg = POST_REPLY_PROMPT.format(
        task_prompt=_post_reply_task_prompt(),
    )
    return _call_post_reply_json(user_input, client, model, context, system_msg)


def call_soul_post_reply(
    user_input: str,
    client: "OpenAI",
    model: str,
    shared_context: str,
    soul: SoulContext,
) -> dict | None:
    """Call one SOUL for a public post reply."""
    soul_memory = soul.soul_memory.strip() or "（暂无）"
    system_msg = (
        f"## SOUL 人格\n{soul.persona.strip()}\n\n"
        f"---\n\n## SOUL 相处记忆\n{soul_memory}\n\n"
        f"---\n\n{_post_reply_task_prompt()}"
    )
    return _call_post_reply_json(user_input, client, model, shared_context, system_msg)


def call_soul_chat_reply(
    user_message: str,
    client: "OpenAI",
    model: str,
    chat_context,
    soul: SoulContext,
) -> dict | None:
    """Call one SOUL for a private chat reply."""
    soul_memory = soul.soul_memory.strip() or "（暂无）"
    system_msg = (
        f"## SOUL 人格\n{soul.persona.strip()}\n\n"
        f"---\n\n## SOUL 相处记忆\n{soul_memory}\n\n"
        f"---\n\n{_chat_reply_task_prompt()}"
    )
    return _call_chat_reply_json(user_message, client, model, chat_context.context, system_msg)


def _post_reply_task_prompt() -> str:
    return POST_REPLY_TASK_PROMPT.replace("{current_datetime}", _now_str())


def _chat_reply_task_prompt() -> str:
    return CHAT_REPLY_TASK_PROMPT.replace("{current_datetime}", _now_str())


def _call_post_reply_json(
    user_input: str,
    client: "OpenAI",
    model: str,
    context: str,
    system_msg: str,
) -> dict | None:
    user_msg = _post_reply_user_message(user_input, context)

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=30,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as e:
        print(f"[Router] API 调用失败：{e}")
        return None

    return _parse_post_reply_content(response.choices[0].message.content)


def _call_chat_reply_json(
    user_message: str,
    client: "OpenAI",
    model: str,
    context: str,
    system_msg: str,
) -> dict | None:
    chat_user_msg = _chat_reply_user_message(user_message, context)

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=30,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": chat_user_msg},
            ],
        )
    except Exception as e:
        print(f"[Router] 私聊 API 调用失败：{e}")
        return None

    return _parse_post_reply_content(response.choices[0].message.content)


def _post_reply_user_message(user_input: str, context: str) -> str:
    return f"## 当前上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 帖子内容\n{user_input}"


def _chat_reply_user_message(user_message: str, context: str) -> str:
    return f"## 私聊上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 用户当前私聊消息\n{user_message}"


def _parse_post_reply_content(content: str | None) -> dict | None:
    content = (content or "").strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[Router] JSON 解析失败：{e}")
        print(f"[Router] 原始输出片段：{content[:200]}")
        return None

    if not isinstance(data, dict):
        print("[Router] 响应不是 JSON 对象")
        return None

    if "reply" not in data:
        print("[Router] 响应缺少 'reply' 字段")
        return None

    data.setdefault("todos_to_upsert", [])
    data.setdefault("todos_to_delete", [])

    if not isinstance(data["todos_to_upsert"], list):
        data["todos_to_upsert"] = []
    if not isinstance(data["todos_to_delete"], list):
        data["todos_to_delete"] = []

    normalized_upsert = []
    for item in data["todos_to_upsert"]:
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            continue
        status = item.get("status", "未完成")
        if status not in ("未完成", "已完成"):
            status = "未完成"
        normalized_upsert.append(
            {
                "id": item.get("id"),
                "task": task.strip(),
                "date": item.get("date"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "status": status,
            }
        )
    data["todos_to_upsert"] = normalized_upsert

    normalized_delete = []
    for item in data["todos_to_delete"]:
        if isinstance(item, dict) and item.get("id"):
            normalized_delete.append({"id": item.get("id")})
    data["todos_to_delete"] = normalized_delete

    return data


# 引擎 2：Global Deep Reflection

GLOBAL_DEEP_REFLECTION_PROMPT = """\
你是 TraceLog 拾迹的全局深反思引擎。

你会读取用户档案、当前待办和本次触发范围内的帖子，生成一份适合用户回看和行动的深反思。

## 输出要求
- 仅输出 Markdown 纯文本，不要输出代码块标记。
- 使用第二人称“你”叙述，语气真诚、具体、克制。
- 不要重写用户档案，不要输出 JSON，不要输出 profile patch。
- 严禁捏造事实；观点必须能从输入中找到依据。
- 建议包含这些部分：
  - 主线事件回顾
  - 情绪与状态趋势
  - 反复出现的压力源或能量来源
  - 待办与行动线索
  - 下一步建议

## 当前时间
{current_datetime}
"""


def call_global_deep_reflection(
    client: "OpenAI",
    model: str,
    profile: str,
    posts: str,
    todos: str,
) -> str | None:
    """Generate one global deep reflection in Markdown."""
    user_content = (
        f"## 用户档案\n\n{profile or '（暂无）'}\n\n"
        "---\n\n"
        f"## 当前待办\n\n{todos or '（暂无）'}\n\n"
        "---\n\n"
        f"## 本次触发范围内的帖子\n\n{posts or '（暂无）'}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=45,
            messages=[
                {"role": "system", "content": GLOBAL_DEEP_REFLECTION_PROMPT.format(current_datetime=_now_str())},
                {"role": "user", "content": user_content},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Router] 深反思生成失败：{e}")
        return None
