"""
TraceLog 拾迹 — LLM Router（双引擎）
Post Reply: 共情回复 + 待办提取
Memory Flush: 重写 profile.md 画像
"""

import json
from datetime import datetime
from openai import OpenAI


# 引擎 1：Post Reply

POST_REPLY_PROMPT = """\
你是 TraceLog 拾迹，一个温暖且有洞察力的个人成长 AI 伴侣。

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


def _now_str() -> str:
    now = datetime.now().astimezone()
    weekday = ["一", "二", "三", "四", "五", "六", "日"]
    return now.strftime(f"%Y 年 %m 月 %d 日（周{weekday[now.weekday()]}）%H:%M")


def call_post_reply(user_input: str, client: OpenAI, model: str, context: str) -> dict | None:
    """Post Reply：共情回复 + 待办增量提取。"""
    system_msg = POST_REPLY_PROMPT.replace("{current_datetime}", _now_str())
    user_msg = f"## 当前上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 帖子内容\n{user_input}"

    try:
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as e:
        print(f"[Router] API 调用失败：{e}")
        return None

    content = (response.choices[0].message.content or "").strip()
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


# 引擎 2：Memory Flush

FLUSH_PROMPT = """\
你是 TraceLog 拾迹的记忆整理引擎。

你的任务是读取用户的「旧画像」和「近期帖子」，将其合并提炼，输出一份更新后的、结构化的 Markdown 用户画像。

## 画像合并策略（极其重要）
1. **保留核心**：旧画像中未被新帖子否定的信息，必须完整保留，不允许随意丢弃。
2. **吸收新知**：从近期帖子中提取新信息并补充到对应模块。
3. **解决冲突**：若新帖子与旧画像冲突，以新帖子信息为准更新。

## 输出要求
- 全篇使用第二人称“你”叙述。
- 仅输出 Markdown 纯文本，不要输出代码块标记。
- 使用多级标题（##）、列表（-）和加粗（**）保证结构清晰。
- 仅保留有实质内容的模块，不写“暂无”或“未提及”。建议包含：
    - 身份与现状
    - 技能与专长
    - 兴趣与习惯
    - 关注的核心人际关系
    - 性格与情绪倾向
    - 长期目标与当前痛点
- 严禁捏造或过度推断，仅基于提供内容进行事实总结。
"""


def flush_profile(client: OpenAI, model: str, old_profile: str, recent_posts: str) -> str | None:
    """Memory Flush：读取旧画像 + 近期帖子，让 LLM 重写 profile.md。"""
    user_content = f"## 旧画像\n\n{old_profile or '（无）'}\n\n---\n\n## 近期帖子\n\n{recent_posts or '（无）'}"

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": FLUSH_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Router] 画像刷新失败：{e}")
        return None

