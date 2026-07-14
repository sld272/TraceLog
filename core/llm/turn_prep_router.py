"""Merged turn-prep LLM call: web-search gate + query rewrite in one round trip.

The two duties are independent (both read only the user message and recent turns),
so folding them into a single JSON-mode call removes one serial LLM hop from every
reply. The system prompt merges the two source prompts verbatim into a dual-duty
prompt, and the parser reuses each module's validation kernel so the merged path
degrades exactly like the two standalone calls did."""

from __future__ import annotations

import json

from core import web_search_gate
from core.llm import query_rewrite_router, secondary_model
from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


TURN_PREP_PROMPT = """\
你是 TraceLog 的回合预处理器。你要在一次调用里同时完成两件彼此独立的判断，只输出一个 JSON，不要回答用户。

════════ 职责一：网页搜索判断 ════════
判断本轮回复是否需要搜索公开网页。

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
- 最多输出 3 个 queries。

════════ 职责二：检索 query rewrite ════════
把用户的自然语言检索意图改写成更适合记忆检索的语义查询和关键词。

## 严格规则
- 只做检索改写，不要回答用户问题。
- 不要生成用户没有表达的新事实。
- 不要改变、推断或声明任何隐私/可见性权限边界。
- 若提供了「最近对话」，用它来补全 raw_query 里的指代和省略（"那件事""它""还是老样子"等），
  把检索意图还原成自包含的具体话题；但只能消解指代，不得编造对话里没有的内容。
- semantic_query 用自然语言描述同一个检索意图，适合向量检索。
- keywords 给 FTS5 使用，保留硬线索、同义词和短语；不要包含空泛问题壳。

════════ 输出格式 ════════
你必须且只能输出一个标准 JSON 对象，不要包含 Markdown 代码块或解释文字。两个职责互不影响：
即使 should_search=false，也要照常给出 semantic_query 和 keywords；即使无需搜索，也要照常做检索改写。

{
  "should_search": true,
  "queries": ["公开搜索词"],
  "reason": "一句话说明搜索原因",
  "freshness_required": true,
  "semantic_query": "用户是否曾表达过夜晚在图书馆学习效率更高的偏好",
  "keywords": ["晚上", "夜晚", "图书馆", "自习室", "学习效率"]
}

当前时间：{current_datetime}
"""


def call_turn_prep(
    client: LLMClient | None,
    model: str | None,
    *,
    user_message: str,
    channel: str,
    recent_turns: list[dict] | None = None,
    context_hint: str = "",
    trace_context: dict | None = None,
) -> dict | None:
    """One LLM call returning both halves merged:
    ``{should_search, queries, reason, freshness_required, semantic_query, keywords}``,
    or ``None`` on any failure (so both halves fall back together upstream)."""
    client, model = secondary_model.resolve(client, model)
    if client is None or model is None:
        return None
    messages = [
        {"role": "system", "content": TURN_PREP_PROMPT.replace("{current_datetime}", now_str())},
        {"role": "user", "content": _turn_prep_user_message(channel, user_message, recent_turns, context_hint)},
    ]
    return call_json_completion(
        client=client,
        model=model,
        operation="turn_prep",
        timeout=20,
        response_format={"type": "json_object"},
        messages=messages,
        parser=_parse_turn_prep_content,
        trace_context={"channel": channel, **(trace_context or {})},
    )


def _turn_prep_user_message(
    channel: str,
    user_message: str,
    recent_turns: list[dict] | None,
    context_hint: str,
) -> str:
    """Carry both halves' inputs in one message: recent turns (rewrite anaphora)
    plus the latest message and context summary (search gate)."""
    hint = context_hint.strip()
    if len(hint) > 2000:
        hint = hint[:2000].rstrip() + "…"
    parts = [f"回复场景：{channel}", ""]
    recent = query_rewrite_router.format_recent_turns(recent_turns)
    if recent:
        parts.extend([recent.rstrip(), ""])
    parts.extend(["用户最新消息：", user_message])
    if hint:
        parts.extend(["", "必要上下文摘要：", hint])
    return "\n".join(parts)


def _parse_turn_prep_content(content: str | None) -> dict | None:
    try:
        raw = json.loads(clean_json_content(content))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    search = web_search_gate.decision_fields_from_payload(raw)
    rewrite = query_rewrite_router.rewrite_fields_from_payload(raw)
    if search is None or rewrite is None:
        return None
    return {**search, **rewrite}
