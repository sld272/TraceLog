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

## 你的任务
1. **reply**：给出真诚、有温度的中文回应与建议，2-4 句话。
2. **todos_to_upsert**：从帖子中提取用户**明确提出**的新增或状态变更的待办。
3. **todos_to_delete**：用户明确表示取消或不再需要的待办。

## 输出格式
严格输出一个合法 JSON 对象，不得包含任何 JSON 以外的文字或代码块标记。

{
  "reply": "...",
  "todos_to_upsert": [
    {"task": "待办描述", "date": "YYYY-MM-DD 或 null", "start_time": "HH:MM 或 null", "end_time": "HH:MM 或 null", "status": "未完成/已完成"}
  ],
  "todos_to_delete": [
    {"id": "要删除的待办 id"}
  ]
}

## 待办提取原则
1. 只提取用户**明确表示要做**的事，不要从情绪、担忧或你自己的建议中推断待办。
2. 已有待办列表中状态发生变化的，使用相同 id 放入 todos_to_upsert 更新状态。
3. 没有新增或变更时，todos_to_upsert 和 todos_to_delete 都输出空数组。
4. 新增待办不需要 id 字段，系统会自动生成。仅更新已有待办时才需要 id。
5. start_time 和 end_time 为 24 小时制时间（如 "09:00"），仅在用户提及具体时间时填写，否则置 null。
6. 【极其重要防误删规则】如果用户表示要取消、放弃或完成某件事，你必须先在「当前上下文」的「待办事项」中寻找它。**如果找不到对应的旧任务，请直接忽略，todos_to_delete 输出空数组 []。绝对严禁张冠李戴，使用无关任务的 id！**

## 当前时间
{current_datetime}
请以此为基准将相对时间表达转化为 YYYY-MM-DD 格式。
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

    try:
        data = json.loads(response.choices[0].message.content)
    except json.JSONDecodeError as e:
        print(f"[Router] JSON 解析失败：{e}")
        return None

    if "reply" not in data:
        print("[Router] 响应缺少 'reply' 字段")
        return None

    data.setdefault("todos_to_upsert", [])
    data.setdefault("todos_to_delete", [])
    return data


# 引擎 2：Memory Flush

FLUSH_PROMPT = """\
你是 TraceLog 拾迹的记忆整理引擎。

你的任务：根据下方提供的「旧画像」和「近期帖子」，输出一篇全新的、完整的 Markdown 格式用户画像。

## 画像要求
- 以第二人称"你"叙述。
- 使用清晰的 Markdown 结构（标题、列表、粗体等），排版精美。
- 涵盖以下维度（按需选择，没有内容的维度直接省略，不要写"暂无"）：
  - 身份背景
  - 技能与专长
  - 兴趣爱好
  - 长远目标
  - 身边的人
  - 常去地点
  - 性格与情绪倾向
- 只描述日记和旧画像中**明确出现**的信息，禁止推断或联想。
- 信息不足时画像可以很短，这是正确的行为。
- 只输出 Markdown 画像文本，不要任何额外说明。
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

