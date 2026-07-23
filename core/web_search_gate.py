"""LLM gate for deciding whether a reply should use web search."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core import logging_service
from core.llm import secondary_model
from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient

MAX_QUERIES = 3

WEB_SEARCH_GATE_PROMPT = """\
你是 TraceLog 的网页搜索判断器。你的任务不是回答用户，而是判断本轮回复是否需要搜索公开网页。

只在确实需要外部、当前、公开事实或轻量公开背景时搜索，例如：
- 新闻、实时信息、价格、政策、版本、发布时间、体育赛程、天气、公司/产品当前状态
- 用户明确要求“搜一下”“查一下”“联网确认”“看看网上”
- 用户询问某个 URL、公开网站、公开产品或公开文档的当前内容
- 用户虽然是在闲聊/表达感受，但提到了具名公开作品、产品、人物、地点、事件、组织、游戏、影视、番剧、书籍、论文、软件库等，而上下文没有足够背景；这时应做一次轻量背景搜索，让回复更贴切。

不要搜索这些情况：
- 不包含具名公开实体的情绪陪伴、闲聊、写作润色、创意脑暴、个人反思
- TraceLog 本地记忆、用户档案、SOUL 记忆就能回答的问题
- 用户私密经历、身份信息、人际关系细节、账号、地址、密钥或亲密内容
- 无法在不泄露私人信息的前提下生成公开搜索词的问题

轻量背景搜索规则：
- 如果用户只是说“刚刚看了新番《某作品》，好好磕啊”“最近在玩某游戏，好上头”“某个公开产品挺有意思”，且该名称是公开作品/产品/事件，返回 should_search=true。
- 这类搜索不一定要求最新性，freshness_required 通常为 false，除非用户问“最新、现在、今年、什么时候、哪里看、价格、版本”等。
- 查询词只保留公开名称和必要类别词，例如“在超市后门吸烟的二人 动画”，不要把用户整句情绪表达放进搜索词。

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
    client, model = secondary_model.resolve(client, model)
    if client is None or model is None:
        decision = default_decision("missing_llm_client")
        log_decision(decision, channel=channel, trace_context=trace_context, skipped=True)
        return decision
    body = user_message.strip()
    if not body:
        decision = default_decision("empty_user_message")
        log_decision(decision, channel=channel, trace_context=trace_context, skipped=True)
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
        log_decision(decision, channel=channel, trace_context=trace_context, skipped=True)
        return decision
    decision = WebSearchDecision(
        should_search=bool(data["should_search"]),
        queries=list(data["queries"]),
        reason=str(data.get("reason") or ""),
        freshness_required=bool(data.get("freshness_required")),
    )
    log_decision(decision, channel=channel, trace_context=trace_context, skipped=False)
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
    return decision_fields_from_payload(raw)


def decision_fields_from_payload(raw: Any) -> dict | None:
    """Validate + normalize the web-search half of an already-parsed JSON payload.

    Shared by the standalone gate (``parse_decision_payload``) and the merged
    turn-prep parser, so both apply the exact same query normalization, privacy
    filtering, and no-query degradation."""
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


def log_decision(
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
