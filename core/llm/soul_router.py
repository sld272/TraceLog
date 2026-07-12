"""LLM generation for SOUL Markdown files."""

from __future__ import annotations

import json
from datetime import datetime

from core import logging_service, web_search_gate, web_search_service
from core.llm.common import call_json_completion, clean_json_content
from core.llm.types import LLMClient


SYSTEM_PROMPT = """\
你是 TraceLog 的 SOUL 人格文件设计助手。

你需要把用户自由书写的灵感整理成一个完整、可直接保存的 SOUL Markdown 文件。
输出必须是 JSON，不要输出 Markdown 代码块或额外说明。

JSON 格式：
{
  "soul": "完整 Markdown 文本"
}

Markdown 必须满足：
1. 以 YAML frontmatter 开头，包含 name、version、description、created_at、author、tags。
2. frontmatter 后用中文写清楚这个 SOUL 是 TraceLog 中的 AI 好友。
3. frontmatter 中的 name 必须等于 SOUL 名称，created_at 必须等于创建日期。
4. frontmatter 之后先用一两句话点明人格定位（不要加标题），随后依次包含三个二级标题：## 语气特征、## 怎么回应、## 边界。
5. 不要承诺拥有真实经历、现实身份、专业资质或真实记忆。
6. 不要替用户做医疗、法律、金融等高风险专业决定。

如果用户消息里带有「网页搜索结果」资料：
- 资料只用于把握公开角色/人物的性格、语气、说话方式和典型行为，让人格更贴近原型。
- 生成的仍然是"以其为灵感的 AI 好友"，不得自称真实人物或角色本人，不得声称拥有其真实经历。
- 不要把资料原文、链接或来源列表写进 SOUL 文件。
"""


USER_TEMPLATE = """\
SOUL 名称：{name}
创建日期：{created_at}

用户灵感：
{inspiration}
{reference_section}
请生成一个完整的 SOUL Markdown 文件。
"""


SOUL_SEARCH_GATE_PROMPT = """\
你是 TraceLog 的 SOUL 人格生成搜索判断器。用户正在用一段灵感描述创建 AI 人格文件。\
你的任务不是生成人格，而是判断灵感是否引用了具名的公开角色、人物或作品，\
需要搜索公开网页来补充其性格、语气、说话方式等背景。

需要搜索的情况：
- 灵感提到具名公开作品中的角色（动画、游戏、影视、小说等），例如"像《葬送的芙莉莲》里的芙莉莲"
- 灵感提到具名公开人物（作家、历史人物、名人）的风格，例如"说话像夏目漱石"
- 该名称可能是较新或冷门的作品/角色，仅靠常识可能不准确

不要搜索的情况：
- 灵感只描述抽象性格特质（"温柔的大姐姐""毒舌但可靠的损友"），没有具名公开实体
- 提到的是用户身边的真实私人（朋友、家人、同事），这类信息绝不能搜索
- 灵感包含的实体无法在不泄露私人信息的前提下生成公开搜索词

隐私规则：
- 搜索词必须短、公开、去个人化，只保留公开名称加类别词，例如"芙莉莲 葬送的芙莉莲 角色 性格"。
- 不要把用户的私人叙述、私人姓名、账号或联系方式放入搜索词。

输出必须且只能是 JSON 对象：
{
  "should_search": true,
  "queries": ["公开搜索词"],
  "reason": "一句话说明原因",
  "freshness_required": false
}

最多输出 3 个 queries。
"""


def generate_soul(
    *,
    name: str,
    inspiration: str,
    client: LLMClient,
    model: str,
) -> dict | None:
    """Generate a fresh SOUL Markdown file, with optional web-searched background injected."""
    reference_section, search_used, sources = _build_reference_section(
        name=name, inspiration=inspiration, client=client, model=model
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_TEMPLATE.format(
                name=name,
                created_at=datetime.now().astimezone().date().isoformat(),
                inspiration=inspiration,
                reference_section=reference_section,
            ),
        },
    ]
    parsed = call_json_completion(
        client=client,
        model=model,
        operation="generate_soul",
        messages=messages,
        parser=_parse_soul,
        timeout=45,
        response_format={"type": "json_object"},
        trace_context={"soul_name": name, "search_used": search_used},
    )
    if parsed is None:
        return None
    return {**parsed, "search_used": search_used, "sources": sources}


def _build_reference_section(
    *,
    name: str,
    inspiration: str,
    client: LLMClient,
    model: str,
) -> tuple[str, bool, list[dict]]:
    """Gate → search → format. Silently degrades to no reference on any failure."""
    trace_context = {"channel": "soul_generation", "soul_name": name}
    try:
        settings = web_search_service.effective_config()
        if web_search_service.select_provider(settings) is None:
            return "", False, []
        decision = _decide_search(client, model, inspiration, trace_context=trace_context)
        if not decision.should_search:
            logging_service.log_event(
                "web_search_skipped",
                **trace_context,
                reason=decision.reason or "gate_decision",
                query_count=0,
            )
            return "", False, []
        run = web_search_service.search(
            decision.queries,
            config=settings,
            trace_context=trace_context,
        )
        section = web_search_service.format_results_for_context(run)
        if not section:
            return "", False, []
        logging_service.log_event(
            "web_search_context_injected",
            **trace_context,
            provider=run.provider,
            query_count=len(run.queries),
            result_count=len(run.results),
            context_length=len(section),
        )
        sources = [
            {"title": result.title, "url": result.url}
            for result in run.results
        ]
        return f"\n{section}\n\n", True, sources
    except Exception as exc:  # noqa: BLE001 — search must never break generation
        logging_service.log_event(
            "web_search_failed",
            level="WARNING",
            **trace_context,
            error=f"{type(exc).__name__}: {exc}",
        )
        return "", False, []


def _decide_search(
    client: LLMClient,
    model: str,
    inspiration: str,
    *,
    trace_context: dict,
) -> web_search_gate.WebSearchDecision:
    body = inspiration.strip()
    if not body:
        return web_search_gate.default_decision("empty_inspiration")
    data = call_json_completion(
        client=client,
        model=model,
        operation="soul_search_gate",
        timeout=12,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SOUL_SEARCH_GATE_PROMPT},
            {"role": "user", "content": f"用户灵感：\n{body}"},
        ],
        parser=web_search_gate.parse_decision_payload,
        trace_context=trace_context,
    )
    if data is None:
        return web_search_gate.default_decision("gate_failed")
    return web_search_gate.WebSearchDecision(
        should_search=bool(data["should_search"]),
        queries=list(data["queries"]),
        reason=str(data.get("reason") or ""),
        freshness_required=bool(data.get("freshness_required")),
    )


def _parse_soul(content: str | None) -> dict | None:
    try:
        data = json.loads(clean_json_content(content))
    except json.JSONDecodeError:
        return None
    soul = data.get("soul")
    if not isinstance(soul, str) or not soul.strip():
        return None
    return {"soul": soul.strip()}
