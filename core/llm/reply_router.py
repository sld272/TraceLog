"""LLM calls for public post, private chat, and comment replies."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from core.llm.common import clean_json_content, now_str
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


def call_post_reply(user_input: str, client: "OpenAI", model: str, context: str) -> dict | None:
    """Post Reply: generate one empathetic reply."""
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
    return POST_REPLY_TASK_PROMPT.replace("{current_datetime}", now_str())


def _chat_reply_task_prompt() -> str:
    return CHAT_REPLY_TASK_PROMPT.replace("{current_datetime}", now_str())


def _comment_reply_task_prompt() -> str:
    return COMMENT_REPLY_TASK_PROMPT.replace("{current_datetime}", now_str())


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
    except Exception:
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
    except Exception:
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
    except Exception:
        return None

    return _parse_post_reply_content(response.choices[0].message.content)


def _post_reply_user_message(user_input: str, context: str) -> str:
    return f"## 当前上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 帖子内容\n{user_input}"


def _chat_reply_user_message(user_message: str, context: str) -> str:
    return f"## 私聊上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 用户当前私聊消息\n{user_message}"


def _comment_reply_user_message(user_message: str, context: str) -> str:
    return f"## 评论线程上下文\n{context or '（暂无历史数据）'}\n\n---\n\n## 用户当前评论回复\n{user_message}"


def _parse_post_reply_content(content: str | None) -> dict | None:
    content = clean_json_content(content)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    if "reply" not in data:
        return None

    reply = data.get("reply")
    return {"reply": reply}

