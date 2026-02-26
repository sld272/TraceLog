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
顶层必须包含以下两个键：

{
  "reply": "（字符串）温暖的情感回应 + 具体可行的行动建议，中文，2-4句话",
  "extracted_data": {
    "mood": "（字符串，必填）用2-4个词描述当日整体情绪状态，如：平静、有些疲惫、兴奋期待",
    "summary": "（字符串，必填）用一句话概括今日日记的核心内容",

    以下字段按实际内容按需输出，无相关内容则直接省略该字段，不要输出空数组

    "skills": [{"name": "技能名称", "category": "学习/编程/语言/音乐/体育/其他"}],
    "hobbies": [{"name": "活动名称", "category": "音乐/运动/游戏/阅读/创作/社交/其他"}],
    "todos": [{"task": "具体任务描述", "date": "YYYY-MM-DD 或 null", "status": "pending/done"}],
    "goals": [{"goal": "目标描述", "type": "学习/职业/健康/生活/其他", "deadline": "时间描述或 null"}],
    "people": [{"name": "人名或称谓", "relation": "朋友/家人/同事/恋人/其他", "context": "简短描述互动场景"}],
    "places": [{"name": "地点名称", "type": "旅行目的地/常去地点/工作场所/其他", "date": "YYYY-MM-DD 或 null"}],
    "media": [{"title": "作品名称", "type": "书/电影/剧/音乐/播客/游戏/其他", "status": "想看/在看/已完成"}],
    "food": [{"name": "食物名称", "type": "烹饪尝试/外出就餐/想尝试/其他"}],
    "health": [{"type": "运动/睡眠/饮食/心理/其他", "content": "具体描述"}],
    "ideas": [{"content": "想法或灵感的具体内容"}],
    "purchases": [{"item": "物品名称", "status": "想买/已购买/已收到"}],
    "emotions": [{"trigger": "情绪触发原因", "feeling": "具体情绪词", "reflection": "本人的反思或应对"}]
  }
}

## 当前时间
{current_datetime}
请以此为基准将日记中的相对时间表达（如“明天”“4号”“下周五”）全部转化为准确的 YYYY-MM-DD 格式。
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
    current_datetime = now.strftime("现在是 %Y 年 %m 月 %d 日（周%A）%H:%M")
    # 将周几转为中文
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
