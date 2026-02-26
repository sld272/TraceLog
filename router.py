"""
TraceLog 拾迹  LLM Router
负责：Prompt 设计 + API 调用 + JSON 解析
"""

import json
from datetime import datetime
from openai import OpenAI

SYSTEM_PROMPT = """\
你是 TraceLog 拾迹的日记分析引擎。用户会输入一段自然语言日记，你需要完成两件事：
1. 给出温暖、有洞察力的中文情感回应与行动建议（reply）
2. 从日记中提取结构化信息（extracted_data）

## 输出格式要求
你必须且只能输出一个合法的 JSON 对象，不得包含任何 JSON 以外的文字、代码块标记或解释。

## JSON 结构规范
顶层必须且只能包含 "reply" 和 "extracted_data" 两个键，每个键名在整个 JSON 中只能出现一次。

{
  "reply": "（字符串）温暖的情感回应 + 具体可行的行动建议，中文，2-4句话",
  "extracted_data": {
    "mood": "（字符串，必填）用2-4个词描述当日整体情绪状态，如：平静、有些疲惫、兴奋期待",
    "summary": "（字符串，必填）用一句话概括今日日记的核心内容",

    "skills": [{"name": "技能名称", "proficiency": "对当前掌握程度的描述，如：刚入门、练习中、比较熟练", "notes": "具体情况或感受或其他补充信息，可为 null"}],
    "hobbies": [{"name": "兴趣名称", "notes": "参与方式、频率或情感联结或其他补充信息，可为 null"}],
    "todos": [{"task": "具体任务描述", "date": "YYYY-MM-DD 或文字描述或 null", "status": "未开始/进行中/已完成", "notes": "补充信息，可为 null"}],
    "goals": [{"goal": "目标描述", "deadline": "YYYY-MM-DD 或文字描述或 null", "status": "未达成/已达成", "notes": "动机或背景或其他补充信息，可为 null"}],
    "people": [{"name": "人名或称谓", "relation": "与用户的关系，如：朋友、室友、导师、父母、恋人", "notes": "互动描述或对此人的说明或其他补充信息，可为 null"}],
    "places": [{"name": "地点名称", "type": "语义标签，如：学校、图书馆、旅行目的地，自行判断", "notes": "背景信息，可为 null"}],
    "media": [{"title": "作品名称", "type": "类型，如：小说、电影、播客、游戏，自行判断", "status": "当前状态，如：想看、在读、玩过等", "notes": "感受或评价或其他补充信息，可为 null"}],
    "food": [{"name": "食物名称", "notes": "相关描述，如：想试试、今天吃了觉得不错"}],
    "health": [{"type": "简洁标签，如：跑步、冥想、失眠、感冒", "notes": "具体描述"}],
    "ideas": [{"content": "想法或灵感的具体内容"}],
    "purchases": [{"item": "物品名称", "status": "想要/已拥有", "notes": "原因或用途或其他补充信息，可为 null"}],
    "emotions": [{"trigger": "情绪触发原因", "feeling": "具体情绪词", "reflection": "本人的反思或应对"}]
  }
}

## 提取原则（必须遵守）
1. 宁少勿滥：只提取日记中有实质内容支撑的信息，不要为了"填完字段"而强行推断。
2. todos 只记录真正有意义的待办事项，"休息""睡觉"等日常行为不是 todo，不要提取。
3. people 只记录日记中提及的其他人，不要将用户本人（"我""本人"）作为条目提取。
4. skills 避免将同一技能拆分为多个条目。
5. 每个字段名在 JSON 中只能出现一次，严禁重复 key。
6. 没有相关内容的字段直接省略，不要输出空数组。

## 当前时间
{current_datetime}
请以此为基准将日记中的相对时间表达（如"明天""4号""下周五"）全部转化为准确的 YYYY-MM-DD 格式。
"""


def parse_response(response_text: str) -> dict | None:
    """解析 LLM 返回的 JSON 字符串，校验必要字段，失败返回 None。"""
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        print(f"[Router] JSON 解析失败：{e}")
        print(f"[Router] 原始响应：\n{response_text}")
        return None

    if "reply" not in data or "extracted_data" not in data:
        print("[Router] 响应缺少必要字段 'reply' 或 'extracted_data'")
        print(f"[Router] 原始响应：\n{response_text}")
        return None

    extracted = data["extracted_data"]
    if "mood" not in extracted or "summary" not in extracted:
        print("[Router] extracted_data 缺少必要字段 'mood' 或 'summary'")
        return None

    return data


def call_router(user_input: str, client: OpenAI, model: str) -> dict | None:
    """
    将用户日记发给 LLM，返回包含 reply 和 extracted_data 的结构化 dict。
    失败时返回 None。
    """
    now = datetime.now()
    weekday_cn = ["一", "二", "三", "四", "五", "六", "日"]
    current_datetime = now.strftime(f"现在是 %Y 年 %m 月 %d 日（周{weekday_cn[now.weekday()]}）%H:%M")

    system_prompt = SYSTEM_PROMPT.replace("{current_datetime}", current_datetime)

    try:
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ],
        )
    except Exception as e:
        print(f"[Router] API 调用失败：{e}")
        return None

    return parse_response(response.choices[0].message.content)
