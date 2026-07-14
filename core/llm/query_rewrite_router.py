"""LLM call for retrieval query rewrite."""

from __future__ import annotations

import json
from typing import Any

from core.llm import secondary_model
from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


QUERY_REWRITE_PROMPT = """\
你是 TraceLog 的检索 query rewrite 引擎。你的任务是把用户的自然语言检索意图改写成更适合记忆检索的语义查询和关键词。

## 严格规则
- 只做检索改写，不要回答用户问题。
- 不要生成用户没有表达的新事实。
- 不要改变、推断或声明任何隐私/可见性权限边界。
- 若提供了「最近对话」，用它来补全 raw_query 里的指代和省略（"那件事""它""还是老样子"等），
  把检索意图还原成自包含的具体话题；但只能消解指代，不得编造对话里没有的内容。
- semantic_query 用自然语言描述同一个检索意图，适合向量检索。
- keywords 给 FTS5 使用，保留硬线索、同义词和短语；不要包含空泛问题壳。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，不要包含 Markdown 代码块或解释文字。

{
  "semantic_query": "用户是否曾表达过夜晚在图书馆学习效率更高的偏好",
  "keywords": ["晚上", "夜晚", "图书馆", "自习室", "学习效率", "更高效"]
}

## 当前时间
{current_datetime}
"""


def format_recent_turns(recent_turns: list[dict] | None) -> str:
    if not recent_turns:
        return ""
    lines = []
    for turn in recent_turns:
        role = "用户" if str(turn.get("role")) == "user" else "AI"
        content = str(turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}：{content}")
    if not lines:
        return ""
    return "最近对话（用于消解指代/省略）：\n" + "\n".join(lines) + "\n\n---\n\n"


def call_query_rewrite(
    client: LLMClient,
    model: str,
    *,
    raw_query: str,
    channel: str,
    recent_turns: list[dict] | None = None,
    trace_context: dict | None = None,
) -> dict | None:
    client, model = secondary_model.resolve(client, model)
    user_content = (
        f"channel: {channel}\n\n"
        "---\n\n"
        f"{format_recent_turns(recent_turns)}"
        f"raw_query:\n{raw_query}"
    )
    return call_json_completion(
        client=client,
        model=model,
        operation="query_rewrite",
        timeout=20,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": QUERY_REWRITE_PROMPT.replace("{current_datetime}", now_str())},
            {"role": "user", "content": user_content},
        ],
        parser=_parse_query_rewrite_content,
        trace_context=trace_context,
    )


def _parse_query_rewrite_content(content: str | None) -> dict | None:
    try:
        data = json.loads(clean_json_content(content))
    except json.JSONDecodeError:
        return None
    return rewrite_fields_from_payload(data)


def rewrite_fields_from_payload(data: Any) -> dict | None:
    """Extract the query-rewrite half (semantic_query + string keywords) from an
    already-parsed JSON payload. Shared by the standalone rewrite call and the
    merged turn-prep parser; length caps/thresholds stay in ``query_rewriter``."""
    if not isinstance(data, dict):
        return None
    semantic_query = data.get("semantic_query")
    keywords = data.get("keywords")
    if not isinstance(semantic_query, str):
        semantic_query = ""
    if not isinstance(keywords, list):
        keywords = []
    return {
        "semantic_query": semantic_query,
        "keywords": [item for item in keywords if isinstance(item, str)],
    }
