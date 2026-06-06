"""LLM gate for deciding whether a reply should use web search."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core import logging_service
from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient

MAX_QUERIES = 3

WEB_SEARCH_GATE_PROMPT = """\
你是 TraceLog 的网页搜索判断器。你的任务不是回答用户，而是判断本轮回复是否需要搜索公开网页。

只在确实需要外部、当前、公开事实时搜索，例如：
- 新闻、实时信息、价格、政策、版本、发布时间、体育赛程、天气、公司/产品当前状态
- 用户明确要求“搜一下”“查一下”“联网确认”“看看网上”
- 用户询问某个 URL、公开网站、公开产品或公开文档的当前内容

不要搜索这些情况：
- 情绪陪伴、闲聊、写作润色、创意脑暴、个人反思
- TraceLog 本地记忆、用户档案、SOUL 记忆就能回答的问题
- 用户私密经历、身份信息、人际关系细节、账号、地址、密钥或亲密内容
- 无法在不泄露私人信息的前提下生成公开搜索词的问题

隐私规则：
- 搜索词必须短、公开、去个人化。
- 不要把私人姓名、地址、账号、联系方式、密钥、具体人际细节放入搜索词。
- 如果问题需要私密细节才能搜索，返回 should_search=false。

输出必须且只能是 JSON 对象：
{
  "should_search": true,
  "queries": ["公开搜索词"],
  "reason": "一句话说明原因",
  "freshness_required": true
}

最多输出 3 个 queries。当前时间：{current_datetime}
"""


@dataclass(frozen=True)
class WebSearchDecision:
    should_search: bool
    queries: list[str]
    reason: str
    freshness_required: bool


def decide(
    client: LLMClient | None,
    model: str | None,
    user_message: str,
    *,
    channel: str,
    context_hint: str = "",
    trace_context: dict | None = None,
) -> WebSearchDecision:
    """Return an LLM decision, defaulting to no search on any failure."""
    if client is None or model is None:
        decision = default_decision("missing_llm_client")
        _log_decision(decision, channel=channel, trace_context=trace_context, skipped=True)
        return decision
    body = user_message.strip()
    if not body:
        decision = default_decision("empty_user_message")
        _log_decision(decision, channel=channel, trace_context=trace_context, skipped=True)
        return decision

    messages = [
        {"role": "system", "content": WEB_SEARCH_GATE_PROMPT.replace("{current_datetime}", now_str())},
        {"role": "user", "content": _gate_user_message(channel, body, context_hint)},
    ]
    data = call_json_completion(
        client=client,
        model=model,
        operation="web_search_gate",
        timeout=12,
        response_format={"type": "json_object"},
        messages=messages,
        parser=parse_decision_payload,
        trace_context={"channel": channel, **(trace_context or {})},
    )
    if data is None:
        decision = default_decision("gate_failed")
        _log_decision(decision, channel=channel, trace_context=trace_context, skipped=True)
        return decision
    decision = WebSearchDecision(
        should_search=bool(data["should_search"]),
        queries=list(data["queries"]),
        reason=str(data.get("reason") or ""),
        freshness_required=bool(data.get("freshness_required")),
    )
    _log_decision(decision, channel=channel, trace_context=trace_context, skipped=False)
    return decision


def default_decision(reason: str = "") -> WebSearchDecision:
    return WebSearchDecision(
        should_search=False,
        queries=[],
        reason=reason,
        freshness_required=False,
    )


def parse_decision_payload(content: str | None) -> dict | None:
    try:
        raw = json.loads(clean_json_content(content))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    should_search = bool(raw.get("should_search"))
    reason = str(raw.get("reason") or "").strip()
    freshness_required = bool(raw.get("freshness_required"))
    queries = _filter_public_queries(_normalize_queries(raw.get("queries")))
    if should_search and not queries:
        should_search = False
        reason = reason or "missing_or_private_queries"
    return {
        "should_search": should_search,
        "queries": queries if should_search else [],
        "reason": reason,
        "freshness_required": freshness_required,
    }


def _normalize_queries(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    queries: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        text = text[:240]
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(text)
        if len(queries) >= MAX_QUERIES:
            break
    return queries


def _filter_public_queries(queries: list[str]) -> list[str]:
    return [query for query in queries if not _looks_private(query)]


def _looks_private(query: str) -> bool:
    compact = re.sub(r"[\s-]+", "", query)
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", query):
        return True
    if re.search(r"(?:\+?86)?1[3-9]\d{9}", compact):
        return True
    if re.search(r"\d{9,}", compact):
        return True
    if re.search(r"\b(?:sk|pk|rk|ghp|gho|ghu|github_pat)-[A-Za-z0-9_-]{10,}\b", query, re.IGNORECASE):
        return True
    if re.search(r"\b[A-Za-z0-9_-]{32,}\b", query):
        return True
    return False


def _gate_user_message(channel: str, user_message: str, context_hint: str) -> str:
    hint = context_hint.strip()
    if len(hint) > 2000:
        hint = hint[:2000].rstrip() + "…"
    parts = [
        f"回复场景：{channel}",
        "",
        "用户最新消息：",
        user_message,
    ]
    if hint:
        parts.extend(["", "必要上下文摘要：", hint])
    return "\n".join(parts)


def _log_decision(
    decision: WebSearchDecision,
    *,
    channel: str,
    trace_context: dict | None,
    skipped: bool,
) -> None:
    log_context = {"channel": channel, **(trace_context or {})}
    logging_service.log_event(
        "web_search_gate_result",
        **log_context,
        should_search=decision.should_search,
        query_count=len(decision.queries),
        reason=decision.reason,
        freshness_required=decision.freshness_required,
        skipped=skipped,
    )
