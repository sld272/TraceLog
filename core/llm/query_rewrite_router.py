"""LLM call for retrieval query rewrite."""

from __future__ import annotations

import json

from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


QUERY_REWRITE_PROMPT = """\
你是 TraceLog 的检索 query rewrite 引擎。你的任务是把用户的自然语言检索意图改写成更适合记忆检索的语义查询和关键词。

## 严格规则
- 只做检索改写，不要回答用户问题。
- 不要生成用户没有表达的新事实。
- 不要改变、推断或声明任何隐私/可见性权限边界。
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


def call_query_rewrite(
    client: LLMClient,
    model: str,
    *,
    raw_query: str,
    channel: str,
    trace_context: dict | None = None,
) -> dict | None:
    user_content = (
        f"channel: {channel}\n\n"
        "---\n\n"
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
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
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
