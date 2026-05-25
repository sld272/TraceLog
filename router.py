"""
TraceLog 拾迹 — LLM Router
Post Reply: 共情回复
Light Reflection: 抽取实体、情绪、事件与重要性
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
**回复 (reply)**：结合上下文给出真诚、有温度的中文回应，字数控制在 2-4 句话。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，绝对不要包含任何 Markdown 代码块格式（如 ```json），也不要有任何前置或后置说明文字。

{
  "reply": "你的回复文字"
}

## 上下文结构说明
「当前上下文」可能包含以下区块（部分可能缺席）：
- **近期帖子**：时间线上最近几篇帖子
- **相关帖子**：语义检索到的历史帖子（话题相关，但时间可能较早）
- **待办事项**：当前待完成任务列表

## 当前时间
{current_datetime}
"""

POST_REPLY_PROMPT = """\
你是 TraceLog 拾迹，一个温暖且有洞察力的个人成长 AI 伴侣。

{task_prompt}
"""

CHAT_REPLY_TASK_PROMPT = """\
## 核心任务
**回复 (reply)**：你正在和用户进行一对一私聊。结合上下文给出自然、真诚、贴近该 SOUL 人格的中文回应，字数控制在 2-5 句话。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，绝对不要包含任何 Markdown 代码块格式，也不要有任何前置或后置说明文字。

{
  "reply": "你的回复文字"
}

## 严格执行规则
1. **私聊边界**：私聊是你和用户的单独频道，不要假装其他 SOUL 看得见这段对话。
2. **工具边界**：不要在回复 JSON 中输出待办字段；待办由独立工具处理，且只从公开 post 抽取。

## 当前时间
{current_datetime}
"""

COMMENT_REPLY_TASK_PROMPT = """\
## 核心任务
**回复 (reply)**：你正在 post 下和用户继续一段只属于你们这个 SOUL 的评论线程。结合原 post、你的首条回复和线程上下文，给出自然、真诚、贴近该 SOUL 人格的中文回应，字数控制在 2-5 句话。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，绝对不要包含任何 Markdown 代码块格式，也不要有任何前置或后置说明文字。

{
  "reply": "你的回复文字"
}

## 严格执行规则
1. **评论线程边界**：这条线程只对你这个 SOUL 可见，不要假装其他 SOUL 看得见后续评论。
2. **工具边界**：不要在回复 JSON 中输出待办字段；待办由独立工具处理，且只从公开 post 抽取。

## 当前时间
{current_datetime}
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


def call_soul_comment_reply(
    user_message: str,
    client: "OpenAI",
    model: str,
    comment_context,
    soul: SoulContext,
) -> dict | None:
    """Call one SOUL for a post comment thread reply."""
    soul_memory = soul.soul_memory.strip() or "（暂无）"
    system_msg = (
        f"## SOUL 人格\n{soul.persona.strip()}\n\n"
        f"---\n\n## SOUL 相处记忆\n{soul_memory}\n\n"
        f"---\n\n{_comment_reply_task_prompt()}"
    )
    return _call_comment_reply_json(user_message, client, model, comment_context.context, system_msg)


def _post_reply_task_prompt() -> str:
    return POST_REPLY_TASK_PROMPT.replace("{current_datetime}", _now_str())


def _chat_reply_task_prompt() -> str:
    return CHAT_REPLY_TASK_PROMPT.replace("{current_datetime}", _now_str())


def _comment_reply_task_prompt() -> str:
    return COMMENT_REPLY_TASK_PROMPT.replace("{current_datetime}", _now_str())


def _clean_json_content(content: str | None) -> str:
    text = (content or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


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


def _call_comment_reply_json(
    user_message: str,
    client: "OpenAI",
    model: str,
    context: str,
    system_msg: str,
) -> dict | None:
    comment_user_msg = _comment_reply_user_message(user_message, context)

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=30,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": comment_user_msg},
            ],
        )
    except Exception as e:
        print(f"[Router] 评论回复 API 调用失败：{e}")
        return None

    return _parse_post_reply_content(response.choices[0].message.content)


def _post_reply_user_message(user_input: str, context: str) -> str:
    return f"## 当前上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 帖子内容\n{user_input}"


def _chat_reply_user_message(user_message: str, context: str) -> str:
    return f"## 私聊上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 用户当前私聊消息\n{user_message}"


def _comment_reply_user_message(user_message: str, context: str) -> str:
    return f"## 评论线程上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 用户当前评论回复\n{user_message}"


def _parse_post_reply_content(content: str | None) -> dict | None:
    content = _clean_json_content(content)

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

    reply = data.get("reply")
    return {"reply": reply}


# 引擎 1.5：Todo Tool

TODO_TOOL_PROMPT = """\
你是 TraceLog 拾迹的 TodoTool。你的任务是只从一条公开 post 中抽取可执行待办。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，不要包含 Markdown 代码块或解释文字。

{
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

## 严格规则
1. 只处理目标 post 中用户明确表达的任务、DDL、提醒、取消或完成状态。
2. 不要把情绪、抱怨、建议、愿望或 SOUL 的回复转成待办。
3. 如果要更新状态或删除任务，必须引用「当前待办」中真实存在的 id；找不到就不要输出。
4. 新增待办 id 必须为 null。
5. date 使用 YYYY-MM-DD；start_time/end_time 使用 24 小时 HH:MM；用户未明确提及时填 null。
6. status 仅允许 "未完成" 或 "已完成"。
7. 没有待办变化时，两个数组都输出 []。

## 当前时间
{current_datetime}
请以此为绝对基准，计算并将“明天、后天、下周三”等相对时间转化为 YYYY-MM-DD。
"""


def call_todo_tool(
    client: "OpenAI",
    model: str,
    *,
    post: str,
    active_todos: str,
) -> dict | None:
    """Extract todo changes from one public post."""
    user_content = (
        f"## 当前待办\n\n{active_todos or '（暂无）'}\n\n"
        "---\n\n"
        f"## 目标公开 post\n\n{post}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=30,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": TODO_TOOL_PROMPT.replace("{current_datetime}", _now_str())},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:
        print(f"[Router] TodoTool 调用失败：{e}")
        return None

    return _parse_todo_tool_content(response.choices[0].message.content)


def _parse_todo_tool_content(content: str | None) -> dict | None:
    content = _clean_json_content(content)

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[Router] TodoTool JSON 解析失败：{e}")
        print(f"[Router] 原始输出片段：{content[:200]}")
        return None

    if not isinstance(data, dict):
        print("[Router] TodoTool 响应不是 JSON 对象")
        return None

    return {
        "todos_to_upsert": _normalize_todo_upserts(data.get("todos_to_upsert")),
        "todos_to_delete": _normalize_todo_deletes(data.get("todos_to_delete")),
    }


def _normalize_todo_upserts(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            continue
        status = item.get("status", "未完成")
        if status not in ("未完成", "已完成"):
            status = "未完成"
        normalized.append(
            {
                "id": item.get("id"),
                "task": task.strip(),
                "date": item.get("date"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "status": status,
            }
        )
    return normalized


def _normalize_todo_deletes(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if isinstance(item, dict) and item.get("id"):
            normalized.append({"id": item.get("id")})
    return normalized


# 引擎 2：Light Reflection

LIGHT_REFLECTION_PROMPT = """\
你是 TraceLog 拾迹的轻反思引擎。你的任务是读取一条公开 post，并抽取可被长期查询、聚合和复盘使用的结构化记忆。

## 输入说明
- 目标 post：本次唯一需要抽取的记录。
- 近期 posts：只用于理解上下文，不要把近期 posts 中没有出现在目标 post 的新事实写入结果。
- 用户档案：用于消歧已知人物、课程、项目和长期目标。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，不要包含 Markdown 代码块或解释文字。

{
  "entities": [
    {
      "type": "person|course|project|place|org|event_topic",
      "name": "规范名",
      "aliases": ["本帖中实际出现的称呼"],
      "role": "subject|object|mentioned"
    }
  ],
  "emotions": [
    {
      "label": "焦虑|喜悦|疲惫|兴奋|平静|失落|愤怒|期待|羞愧|无感",
      "intensity": 0.0
    }
  ],
  "events": [
    {
      "ts": "事件发生时间 ISO8601；不明则用 post.ts",
      "summary": "一句话事实描述，最多 30 字",
      "category": "study|social|health|project|life"
    }
  ],
  "relations": [
    {
      "a": "实体名，必须出现在 entities[].name 中",
      "b": "实体名，必须出现在 entities[].name 中",
      "rel_type": "friend|classmate|teammate|mentor|family|colleague",
      "strength_delta": 0.0
    }
  ],
  "importance": 0.0
}

## 严格规则
1. 只抽取目标 post 直接表达或强证据支持的内容，禁止从近期 posts 脑补。
2. 重要性 importance 按 0 到 1 打分：明确决策 +0.30，deadline/具体时间承诺 +0.25，重要人际 +0.20，强情绪 +0.15，转折事件 +0.20，普通日常基线 0.10，封顶 1.0。
3. emotions 最多输出 3 个；没有明显情绪时输出 [{"label":"无感","intensity":0.1}]。
4. events 最多输出 3 个；没有可总结事件时输出 []。
5. relations 只有在目标 post 明确提供互动证据时才输出；strength_delta 限制在 -0.2 到 0.2。

## 当前时间
{current_datetime}
"""


def call_light_reflection(
    client: "OpenAI",
    model: str,
    *,
    post: str,
    recent_posts: str,
    profile: str,
) -> dict | None:
    """Extract structured memory from one post."""
    user_content = (
        f"## 用户档案\n\n{profile or '（暂无）'}\n\n"
        "---\n\n"
        f"## 近期 posts（上下文，不是抽取目标）\n\n{recent_posts or '（暂无）'}\n\n"
        "---\n\n"
        f"## 目标 post\n\n{post}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=30,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LIGHT_REFLECTION_PROMPT.replace("{current_datetime}", _now_str())},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:
        print(f"[Router] 轻反思生成失败：{e}")
        return None

    return _parse_light_reflection_content(response.choices[0].message.content)


def _parse_light_reflection_content(content: str | None) -> dict | None:
    content = _clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[Router] 轻反思 JSON 解析失败：{e}")
        print(f"[Router] 原始输出片段：{content[:200]}")
        return None

    if not isinstance(data, dict):
        print("[Router] 轻反思响应不是 JSON 对象")
        return None

    return {
        "entities": _normalize_reflection_entities(data.get("entities")),
        "emotions": _normalize_reflection_emotions(data.get("emotions")),
        "events": _normalize_reflection_events(data.get("events")),
        "relations": _normalize_reflection_relations(data.get("relations")),
        "importance": _clamp_float(data.get("importance"), 0.5, 0.0, 1.0),
    }


def _normalize_reflection_entities(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    allowed_types = {"person", "course", "project", "place", "org", "event_topic"}
    allowed_roles = {"subject", "object", "mentioned"}
    entities = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        entity_type = item.get("type")
        if entity_type not in allowed_types:
            entity_type = "event_topic"
        role = item.get("role")
        if role not in allowed_roles:
            role = "mentioned"
        aliases = item.get("aliases")
        if not isinstance(aliases, list):
            aliases = []
        normalized_aliases = [a.strip() for a in aliases if isinstance(a, str) and a.strip()]
        key = (entity_type, name.strip(), role)
        if key in seen:
            continue
        seen.add(key)
        entities.append(
            {
                "type": entity_type,
                "name": name.strip(),
                "aliases": normalized_aliases,
                "role": role,
            }
        )
    return entities


def _normalize_reflection_emotions(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    allowed = {"焦虑", "喜悦", "疲惫", "兴奋", "平静", "失落", "愤怒", "期待", "羞愧", "无感"}
    emotions_by_label = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if label not in allowed:
            continue
        intensity = _clamp_float(item.get("intensity"), 0.1, 0.0, 1.0)
        emotions_by_label[label] = max(intensity, emotions_by_label.get(label, 0.0))
    return [
        {"label": label, "intensity": intensity}
        for label, intensity in sorted(emotions_by_label.items())
    ]


def _normalize_reflection_events(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    allowed_categories = {"study", "social", "health", "project", "life"}
    events = []
    for item in value:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        category = item.get("category")
        if category not in allowed_categories:
            category = "life"
        ts = item.get("ts")
        if not isinstance(ts, str) or not ts.strip():
            ts = None
        events.append(
            {
                "ts": ts,
                "summary": summary.strip()[:80],
                "category": category,
            }
        )
    return events


def _normalize_reflection_relations(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    allowed_types = {"friend", "classmate", "teammate", "mentor", "family", "colleague"}
    relations = []
    for item in value:
        if not isinstance(item, dict):
            continue
        a = item.get("a")
        b = item.get("b")
        if not isinstance(a, str) or not a.strip() or not isinstance(b, str) or not b.strip():
            continue
        rel_type = item.get("rel_type")
        if rel_type not in allowed_types:
            rel_type = "friend"
        relations.append(
            {
                "a": a.strip(),
                "b": b.strip(),
                "rel_type": rel_type,
                "strength_delta": _clamp_float(item.get("strength_delta"), 0.0, -0.2, 0.2),
            }
        )
    return relations


def _clamp_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


# 引擎 3：Global Deep Reflection

GLOBAL_DEEP_REFLECTION_PROMPT = """\
你是 TraceLog 拾迹的全局深反思引擎。

你会读取用户档案、当前待办、本次触发范围内的帖子，以及轻反思已经抽取出的实体、情绪、事件和重要性摘要，生成一份适合用户回看和行动的深反思。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，不要包含 Markdown 代码块或解释文字。

{
  "reflection_md": "Markdown 深反思正文",
  "patches": [
    {
      "section": "技能与专长",
      "ops": [
        {"op": "add", "value": "熟悉 ChromaDB 与 FTS5 双轨检索"}
      ],
      "evidence": ["20260520-003"],
      "confidence": 0.86
    }
  ]
}

## reflection_md 要求
- 使用第二人称“你”叙述，语气真诚、具体、克制。
- 严禁捏造事实；观点必须能从输入中找到依据。
- 建议包含这些部分：
  - 主线事件回顾
  - 情绪与状态趋势
  - 反复出现的压力源或能量来源
  - 待办与行动线索
  - 下一步建议

## patches 要求
- 只在有明确证据时输出 patch；没有可靠画像更新时输出空数组 []。
- patch 只能修改输入 user.md 已存在的 section。
- add 不带 anchor；update/remove 必须使用 user.md 里原样存在的 anchor。
- 不得输出“暂无”“待补充”“未知”等无信息条目；空章节保持空白即可。
- 你维护的是一份会不断修正的用户画像，不是只追加事实的日志。
- 如果已有条目可被修正、合并或细化，应优先 update，而不是 add 一条近似重复的新内容。
- 如果已有条目被新证据推翻、已经过时、重复，或只是占位内容，应输出 remove。
- high sensitivity 章节必须极度保守，只在用户明确陈述、证据真实、置信度高时输出 patch。
- 当用户明确自我介绍时，必须输出对应 patch。例如“我叫 X”输出到“基本信息”，“我是高一生/大学生/主唱”等稳定身份输出到“关键身份”或“身份与现状”。
- evidence 必须是本次输入中真实存在的 post id。
- confidence 使用 0 到 1。

## 当前时间
{current_datetime}
"""


def call_global_deep_reflection(
    client: "OpenAI",
    model: str,
    profile: str,
    posts: str,
    light_summary: str,
    todos: str,
) -> dict | None:
    """Generate one global deep reflection plus profile patches."""
    user_content = (
        f"## 用户档案\n\n{profile or '（暂无）'}\n\n"
        "---\n\n"
        f"## 当前待办\n\n{todos or '（暂无）'}\n\n"
        "---\n\n"
        f"## 轻反思摘要\n\n{light_summary or '（暂无）'}\n\n"
        "---\n\n"
        f"## 本次触发范围内的帖子\n\n{posts or '（暂无）'}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=45,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": GLOBAL_DEEP_REFLECTION_PROMPT.replace("{current_datetime}", _now_str())},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:
        print(f"[Router] 深反思生成失败：{e}")
        return None

    return _parse_global_deep_reflection_content(response.choices[0].message.content)


SOUL_DEEP_REFLECTION_PROMPT = """\
你是 TraceLog 拾迹的 SOUL 独立画像深反思引擎。

你会读取某个 SOUL 的人格、当前相处记忆，以及这段时间用户与该 SOUL 相关的原始互动，生成该 SOUL 对用户的独立理解更新。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，不要包含 Markdown 代码块或解释文字。

{
  "reflection_md": "Markdown 深反思正文",
  "patches": [
    {
      "section": "对用户的理解",
      "ops": [
        {"op": "add", "value": "用户在这个 SOUL 面前更愿意直接表达疲惫和求助"}
      ],
      "evidence": ["chat_message:12"],
      "confidence": 0.82
    }
  ]
}

## reflection_md 要求
- 使用第三人称或“用户”叙述，写给系统内部调试与未来相处使用。
- 严禁捏造事实；观点必须能从输入中找到依据。
- 关注这个 SOUL 与用户之间的互动模式、偏好、边界和可持续的相处线索。

## patches 要求
- 只在有明确证据时输出 patch；没有可靠画像更新时输出空数组 []。
- patch 只能修改输入 SOUL 记忆里已存在的 section。
- add 不带 anchor；update/remove 必须使用当前 SOUL 记忆里原样存在的 anchor。
- 不得输出“暂无”“待补充”“未知”等无信息条目；空章节保持空白即可。
- 维护的是这个 SOUL 对用户的独立理解，不要简单复制全局基本信息。
- 如果已有条目可被修正、合并或细化，应优先 update，而不是 add 一条近似重复的新内容。
- 如果已有条目被新证据推翻、已经过时、重复，或只是占位内容，应输出 remove。
- evidence 必须是本次输入中真实存在的 evidence id，例如 post:20260525-001、comment:3、chat_message:12、comment_message:8。
- confidence 使用 0 到 1。

## 当前时间
{current_datetime}
"""


def call_soul_deep_reflection(
    client: "OpenAI",
    model: str,
    soul: SoulContext,
    interactions: str,
) -> dict | None:
    """Generate one SOUL-specific deep reflection plus soul memory patches."""
    user_content = (
        f"## SOUL 人格\n\n{soul.persona.strip() or '（暂无）'}\n\n"
        "---\n\n"
        f"## 当前 SOUL 相处记忆\n\n{soul.soul_memory.strip() or '（暂无）'}\n\n"
        "---\n\n"
        f"## 本次触发范围内的原始互动\n\n{interactions or '（暂无）'}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            timeout=45,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SOUL_DEEP_REFLECTION_PROMPT.replace("{current_datetime}", _now_str())},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:
        print(f"[Router] SOUL 深反思生成失败：{e}")
        return None

    return _parse_global_deep_reflection_content(response.choices[0].message.content)


def _parse_global_deep_reflection_content(content: str | None) -> dict | None:
    content = _clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[Router] 深反思 JSON 解析失败：{e}")
        print(f"[Router] 原始输出片段：{content[:200]}")
        return None

    if not isinstance(data, dict):
        print("[Router] 深反思响应不是 JSON 对象")
        return None

    reflection_md = data.get("reflection_md")
    if not isinstance(reflection_md, str) or not reflection_md.strip():
        print("[Router] 深反思缺少 reflection_md")
        return None

    patches = data.get("patches")
    if not isinstance(patches, list):
        patches = []

    normalized_patches = [patch for patch in patches if isinstance(patch, dict)]
    return {
        "reflection_md": reflection_md.strip(),
        "patches": normalized_patches,
    }
