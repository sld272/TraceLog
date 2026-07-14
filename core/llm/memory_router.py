"""LLM calls for memory reconcile, evidence re-link, and view synthesis."""

from __future__ import annotations

import json
import re

from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


MEMORY_RECONCILE_PROMPT = """\
你是 TraceLog 拾迹的记忆对账引擎。你只维护关于【用户】的结构化 memory units。

## 主体边界
- unit 的主语必须是用户本人，或用户与某个 AI 人格的关系、约定和边界。
- AI 人格自身的设定、情绪、经历和偏好不是用户记忆，禁止写入。
- assistant 消息只能帮助理解上下文，不能单独成为证据。

## 输入
- 场景：公开帖子、评论互动或私聊。
- 新证据事件：当前批次唯一可供 add 引用的用户证据，每条有 event_id。
- 已有 units：可被 retain / confirm / revise / retract。
- challenged unit 会附当前有效 evidence；每个 challenged unit 必须恰好得到一个决定。
- tombstones：false 禁止再次生成同义 unit；outdated 只有出现新证据时才能重新成立。

## 回想价值
只有未来对理解用户仍有价值的信息才值得成为 unit。瞬时琐事（正在上课、刚吃饭、等公交）
不应记录。身份、长期目标、稳定偏好、重要关系和持续数天以上的处境可以记录。
可被回问的具体事实（成绩分数、日期、数字、决定、人名、结果）不是瞬时琐事——用户之后
会问起（"我上次说我考了多少分？"），即使只出现一次也值得记录：落成 tier=episodic 的
insight 或 freeform unit，importance 取 0.5。state 只用于当前仍在持续、过期即失效的
短期处境（备考中、生病、赶截止日期），不要用它承载一次性事实——state 过期后不再被读取，
事实会永久丢失。判别标准：琐事过后无人再提，具体事实会被回问。

## 输出
只输出 JSON：
{
  "summary": "一句话摘要",
  "ops": [
    {"op":"add","type":"identity|preference|state|relationship|insight|freeform","content":"陈述","confidence":0.0,"tier":"core|contextual|episodic","importance":0.0,"evidence_event_ids":[1]},
    {"op":"retain","target_id":"mu_x"},
    {"op":"confirm","target_id":"mu_x","evidence_event_ids":[1],"confidence":0.0},
    {"op":"revise","target_id":"mu_x","content":"新陈述","evidence_event_ids":[1]},
    {"op":"retract","target_id":"mu_x","reason":"false|outdated"}
  ]
}

## 打分档位
confidence 和 importance 只能从五个档位里选，不要输出其他小数：
- confidence：0.3=模糊猜测；0.5=单条间接暗示；0.7=单条明确自述；0.85=多条证据一致；0.95=用户反复强调、几乎不可能弄错。
- importance：0.3=仅短期有参考价值；0.5=普通偏好或近况；0.7=长期目标、稳定偏好、重要关系；0.85=核心身份或重大处境；0.95=用户自我定义级别的事实。

## 硬规则
1. 不得编造 event_id 或 target_id；add 至少引用一条本批新证据。
2. 一个 unit 只承载一个可独立回问、可单独验证的事实；不要把多个事实揉成一条主题式
   概括——宏观综合是画像层的职责，不是 unit 的。已有宽泛主题 unit 时，新出现的具体
   事实应 add 新 unit，而不是只 confirm 到主题 unit 上（那会让事实永远无法被检索）。
   明确且持续有效的用户自述可由单条证据成立。
3. importance < 0.3 的瞬时事实不要产出；短期 state 不得设为 core。
4. 正式 goal 由目标系统管理；这里只记录与理解用户有关的倾向或持续处境。
5. challenged unit：剩余证据完整支持用 retain；新版本支持用 confirm；需改写用 revise；
   已不支持用 retract。confirm/revise 必须引用其当前有效 evidence。
6. 与某个人格互动的场景（公开评论或私聊）应识别稳定称呼、互动约定、回应偏好、语气、边界和默契。
7. 没有可靠增量时返回空 ops。宁缺毋滥。
8. unit 内容禁止相对时间词（今晚、明天、最近、下周、这几天等）——记忆会在多天后被读到，
   相对词必然失真。需要时间就按当前时间换算成绝对表述（如「6 月 30 日前交付」「2026 年 7 月初」）。
   revise 时也要顺手把旧内容里的相对时间词改掉。证据行若带〔时间标注〕，其中相对时间必须
   按标注换算，禁止自行推算日期；无标注时才按当前时间换算。

当前时间：{current_datetime}
"""


MEMORY_NORMALIZE_CLAIM_PROMPT = """\
把每条被撤回的记忆内容压成一条规范化断言（normalized claim），用于同义压制匹配。
要求：
- 主语统一为「用户」，一句话主谓宾，去掉修辞、语气和举例。
- 相对时间换算成绝对表述（当前时间见下）。
- 保留否定词——"不再考研"和"在考研"是两条不同断言。
- 只压缩，不改写事实；无法理解的内容原样返回。
只输出 JSON：{"claims":[{"unit_id":"mu_x","claim":"规范化断言"}]}
当前时间：{current_datetime}
"""


MEMORY_LINK_JUDGE_PROMPT = """\
判断每对记忆之间的关系。两条记忆来自不同的记忆桶（例如公开桶和某个人格的私聊桶）；
桶永不合并，你只判断关系，不改写任何一边。
关系只能是：
- same_fact：同一事实的两次记录（措辞可以不同）。
- contradicts：不能同时为真的直接矛盾（包括"新状态显示旧状态已不再成立"）。
- context_variant：两边都成立，只是不同场合的不同说法或不同侧面——人在公开和私下
  本来就可以说不一样的话，这不是矛盾。
- unrelated：其余一切。拿不准就选 unrelated，宁缺毋滥。
只输出 JSON：{"pairs":[{"a":"mu_x","b":"mu_y","relation":"same_fact|contradicts|context_variant|unrelated"}]}
当前时间：{current_datetime}
"""


MEMORY_RELINK_PROMPT = """\
用户刚修改了一条关于自己的记忆。逐条判断旧证据是否仍支持新内容。
每条证据必须恰好出现在 keep_event_ids 或 drop_event_ids 之一。
只输出 JSON：{"keep_event_ids":[整数],"drop_event_ids":[整数]}
"""


MEMORY_VIEW_SYNTH_PROMPT = """\
把给定的核心 memory units 综合成一段【简洁、整体性】的用户概述：一眼看懂用户是谁——
身份、主要方向/目标、稳定偏好与重要处境，融成一个整体，而不是逐条罗列。

## 硬规则
1. 反捏造：只能陈述给定 units 里明确写出的内容；禁止推断或虚构任何事件、偏好、习惯、
   共同经历或对话方式；units 没写的就不写，不确定就不写。
2. 比例：全文句数不超过「单元数 + 1」。单元少就写得短，禁止铺陈、抒情、举例、渲染情绪。
3. 禁元话语：不得输出「注意/以上叙事/未添加任何虚构/根据提供（给定）的单元」之类的说明、
   免责声明或自我评价；只写画像本身。
4. 克制中性，像一段简短的人物简介，不用文学化修辞；不要把短期状态夸大为长期身份；
   压在字数预算内。

## 输出格式
分段输出。每段都要在 unit_ids 里列出该段内容【直接依据】的 unit id（即每行开头 [id=...]
里的值）；一段找不到可依据的 unit 就不要写这段。
只输出 JSON：{"paragraphs":[{"text":"一段话","unit_ids":["mu_x","mu_y"]}]}
当前时间：{current_datetime}
"""


SOUL_RELATIONSHIP_VIEW_SYNTH_PROMPT = """\
把给定的 relationship units 综合成这个 SOUL 与用户的相处叙事：重点表达称呼、节奏、
回应偏好、边界和默契，不要重复普通身份画像。

## 硬规则
1. 反捏造：只能陈述给定 units 里明确写出的内容；禁止新增或虚构任何共同经历、事件、
   偏好、习惯或对话方式；units 没写的就不写，不确定就不写。
2. 比例：全文句数不超过「单元数 + 1」。单元少就写得短，禁止铺陈、抒情、举例、渲染情绪。
3. 禁元话语：不得输出「注意/以上叙事/未添加任何虚构/根据提供（给定）的单元」之类的说明、
   免责声明或自我评价；只写叙事本身。
4. 压在字数预算内。

## 输出格式
分段输出。每段都要在 unit_ids 里列出该段内容【直接依据】的 unit id（即每行开头 [id=...]
里的值）；一段找不到可依据的 unit 就不要写这段。
只输出 JSON：{"paragraphs":[{"text":"一段话","unit_ids":["mu_x","mu_y"]}]}
当前时间：{current_datetime}
"""


def call_memory_reconcile(
    client: LLMClient,
    model: str,
    *,
    boundary_text: str,
    events_text: str,
    active_units_text: str,
    tombstones_text: str,
    trace_context: dict | None = None,
) -> dict | None:
    user_content = (
        f"## 场景\n\n{boundary_text}\n\n---\n\n"
        f"## 新证据事件\n\n{events_text or '（无）'}\n\n---\n\n"
        f"## 已有 units\n\n{active_units_text or '（无）'}\n\n---\n\n"
        f"## tombstones\n\n{tombstones_text or '（无）'}"
    )
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_reconcile",
        timeout=45,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": MEMORY_RECONCILE_PROMPT.replace(
                    "{current_datetime}", now_str()
                ),
            },
            {"role": "user", "content": user_content},
        ],
        parser=_parse_memory_reconcile_content,
        trace_context=trace_context,
    )


_RECONCILE_OPS = {"add", "retain", "confirm", "revise", "retract"}
_RECONCILE_TYPES = {
    "identity",
    "preference",
    "state",
    "relationship",
    "insight",
    "freeform",
}
_RECONCILE_TIERS = {"core", "contextual", "episodic"}


def _coerce_event_ids(value) -> list[int]:
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def _coerce_float(value, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, result))


def _parse_memory_reconcile_content(content: str | None) -> dict | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    raw_ops = data.get("ops")
    if not isinstance(raw_ops, list):
        raw_ops = []
    ops: list[dict] = []
    for item in raw_ops:
        if not isinstance(item, dict) or item.get("op") not in _RECONCILE_OPS:
            continue
        op = str(item["op"])
        normalized: dict = {
            "op": op,
            "evidence_event_ids": _coerce_event_ids(item.get("evidence_event_ids")),
        }
        if op == "add":
            unit_type = item.get("type")
            tier = item.get("tier")
            if unit_type == "episodic":
                # the prompt says one-shot facts become "episodic units"; a model
                # echoing that as the type must keep the episodic-tier intent
                # instead of falling through to insight/contextual
                unit_type = "insight"
                if tier not in _RECONCILE_TIERS:
                    tier = "episodic"
            normalized["type"] = (
                unit_type if unit_type in _RECONCILE_TYPES else "insight"
            )
            normalized["content"] = str(item.get("content") or "").strip()
            normalized["confidence"] = _coerce_float(item.get("confidence"), 0.6)
            normalized["tier"] = (
                tier if tier in _RECONCILE_TIERS else "contextual"
            )
            normalized["importance"] = _coerce_float(item.get("importance"), 0.5)
        else:
            normalized["target_id"] = str(item.get("target_id") or "")
            if op == "revise":
                normalized["content"] = str(item.get("content") or "").strip()
                if item.get("type") in _RECONCILE_TYPES:
                    normalized["type"] = item["type"]
                if item.get("tier") in _RECONCILE_TIERS:
                    normalized["tier"] = item["tier"]
            if op in {"confirm", "revise"} and item.get("confidence") is not None:
                normalized["confidence"] = _coerce_float(
                    item.get("confidence"), 0.6
                )
            if op == "confirm" and item.get("importance") is not None:
                normalized["importance"] = _coerce_float(
                    item.get("importance"), 0.5
                )
            if op == "retract":
                reason = item.get("reason")
                normalized["reason"] = (
                    reason if reason in {"false", "outdated"} else None
                )
        ops.append(normalized)
    summary = data.get("summary")
    return {
        "ops": ops,
        "summary": summary.strip() if isinstance(summary, str) else "",
    }


def call_memory_relink(
    client: LLMClient,
    model: str,
    *,
    content: str,
    evidence_text: str,
    trace_context: dict | None = None,
) -> dict | None:
    user_content = (
        f"## 记忆的新内容\n\n{content}\n\n---\n\n"
        f"## 旧证据\n\n{evidence_text or '（无）'}"
    )
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_relink",
        timeout=45,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": MEMORY_RELINK_PROMPT},
            {"role": "user", "content": user_content},
        ],
        parser=_parse_memory_relink_content,
        trace_context=trace_context,
    )


def _parse_memory_relink_content(content: str | None) -> dict | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "keep_event_ids": _coerce_event_ids(data.get("keep_event_ids")),
        "drop_event_ids": _coerce_event_ids(data.get("drop_event_ids")),
    }


_LINK_RELATIONS = {"same_fact", "contradicts", "context_variant", "unrelated"}


def call_memory_link_judge(
    client: LLMClient,
    model: str,
    *,
    pairs: list[dict],
    trace_context: dict | None = None,
) -> list[dict] | None:
    """Judge cross-bucket unit pairs (P1 crosslink). ``pairs``:
    [{"a": {"unit_id","content","layer"}, "b": {...}}] where layer is a coarse
    公开/私聊 label — never the raw scope string, so no soul name leaks into the
    judging context. Returns [{"a","b","relation"}] or None on call failure."""
    if not pairs:
        return []
    blocks = []
    for index, pair in enumerate(pairs, start=1):
        a, b = pair["a"], pair["b"]
        blocks.append(
            f"### 对 {index}\n"
            f"A（{a['unit_id']}，{a['layer']}）：{a['content']}\n"
            f"B（{b['unit_id']}，{b['layer']}）：{b['content']}"
        )
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_link_judge",
        timeout=30,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": MEMORY_LINK_JUDGE_PROMPT.replace(
                    "{current_datetime}", now_str()
                ),
            },
            {"role": "user", "content": "## 待判定的记忆对\n\n" + "\n\n".join(blocks)},
        ],
        parser=_parse_link_judge_content,
        trace_context=trace_context,
    )


def _parse_link_judge_content(content: str | None) -> list[dict] | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        return None
    out: list[dict] = []
    for item in data["pairs"]:
        if not isinstance(item, dict):
            continue
        a = str(item.get("a") or "").strip()
        b = str(item.get("b") or "").strip()
        relation = str(item.get("relation") or "").strip()
        if a and b and relation in _LINK_RELATIONS:
            out.append({"a": a, "b": b, "relation": relation})
    return out


def call_memory_normalize_claims(
    client: LLMClient,
    model: str,
    *,
    items: list[dict],
    trace_context: dict | None = None,
) -> dict[str, str] | None:
    """Normalize retracted-unit contents into canonical claims (P2 tombstone
    matching / P1 crosslink key). ``items``: [{"unit_id", "content"}]. Returns
    {unit_id: claim} for the ids the model answered, None on call failure."""
    if not items:
        return {}
    lines = [f"- unit_id={item['unit_id']}\n  {item['content']}" for item in items]
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_normalize_claims",
        timeout=30,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": MEMORY_NORMALIZE_CLAIM_PROMPT.replace(
                    "{current_datetime}", now_str()
                ),
            },
            {"role": "user", "content": "## 被撤回的记忆\n\n" + "\n".join(lines)},
        ],
        parser=_parse_normalize_claims_content,
        trace_context=trace_context,
    )


def _parse_normalize_claims_content(content: str | None) -> dict[str, str] | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return None
    claims: dict[str, str] = {}
    for item in data["claims"]:
        if not isinstance(item, dict):
            continue
        unit_id = str(item.get("unit_id") or "").strip()
        claim = str(item.get("claim") or "").strip()
        if unit_id and claim:
            claims[unit_id] = claim
    return claims


MEMORY_CONSOLIDATION_PROMPT = """\
你是 TraceLog 拾迹的记忆深反思引擎。给你某个主体（用户主记忆，或某个 AI 人格）的全部
active memory units，请做"巩固"：合并重复、处理矛盾。只输出 JSON。

## 输入
每条 unit：unit_id、visibility（public 公开 / private 私密）、type、content。

## 可做的操作
- merge：多条说的是同一件事 → 选一条 survivor 保留，其余 absorbed 并入。
  铁律：survivor 不能比任何被并入的 unit 更公开——合并公开层与私密层的同一信念时，
  survivor 必须是私密那条。可给 survivor 一段融合后的 content（可省略）。
- retract：某条明显错误，或已被另一条取代/矛盾 → 撤回，reason=false|outdated。

## 规则
1. 只用给定的 unit_id，不得编造；survivor 不能出现在自己的 absorbed 列表里。
2. 没把握不要动——宁缺毋滥。只合并真正同义、只撤回真正错误/矛盾的。
3. 不要把不同主语、不同时间的不同事实强行合并。

只输出 JSON：
{
  "summary": "一句话",
  "ops": [
    {"op":"merge","survivor_id":"mu_a","absorbed_ids":["mu_b"],"content":"可选融合内容"},
    {"op":"retract","target_id":"mu_x","reason":"false|outdated"}
  ]
}
当前时间：{current_datetime}
"""


def call_memory_consolidation(
    client: LLMClient,
    model: str,
    *,
    units_text: str,
    trace_context: dict | None = None,
) -> dict | None:
    return call_json_completion(
        client=client,
        model=model,
        operation="memory_consolidation",
        timeout=45,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": MEMORY_CONSOLIDATION_PROMPT.replace(
                    "{current_datetime}", now_str()
                ),
            },
            {"role": "user", "content": f"## 主体的全部 active units\n\n{units_text or '（无）'}"},
        ],
        parser=_parse_memory_consolidation_content,
        trace_context=trace_context,
    )


def _parse_memory_consolidation_content(content: str | None) -> dict | None:
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_ops = data.get("ops")
    if not isinstance(raw_ops, list):
        raw_ops = []
    ops: list[dict] = []
    for item in raw_ops:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        if op == "merge":
            survivor = str(item.get("survivor_id") or "")
            absorbed = [str(x) for x in (item.get("absorbed_ids") or []) if str(x)]
            if not survivor or not absorbed:
                continue
            normalized: dict = {"op": "merge", "survivor_id": survivor, "absorbed_ids": absorbed}
            merged = item.get("content")
            if isinstance(merged, str) and merged.strip():
                normalized["content"] = merged.strip()
            ops.append(normalized)
        elif op == "retract":
            target = str(item.get("target_id") or "")
            if not target:
                continue
            reason = item.get("reason")
            ops.append({
                "op": "retract",
                "target_id": target,
                "reason": reason if reason in {"false", "outdated"} else None,
            })
    summary = data.get("summary")
    return {
        "ops": ops,
        "summary": summary.strip() if isinstance(summary, str) else "",
    }


def call_view_synthesis(
    client: LLMClient,
    model: str,
    *,
    units_text: str,
    char_budget: int,
    view_type: str,
    valid_ids: set[str],
    trace_context: dict | None = None,
) -> str | None:
    """Synthesize a portrait body from cited units. ``valid_ids`` is the set of
    unit ids offered to the model; every kept paragraph must cite only ids in
    this set, making "did not fabricate" a code-verifiable property rather than a
    prompt promise. Returns a paragraph-joined body, or None on any failure /
    full strip so synthesize_view falls back to the deterministic template."""
    user_content = (
        f"## 画像类型\n\n{view_type}\n\n"
        f"## 字数预算\n\n不超过 {char_budget} 字\n\n---\n\n"
        f"## 核心记忆单元\n\n{units_text or '（无）'}"
    )
    prompt = (
        SOUL_RELATIONSHIP_VIEW_SYNTH_PROMPT
        if view_type == "soul_relationship_memory"
        else MEMORY_VIEW_SYNTH_PROMPT
    )

    def parser(content: str | None) -> str | None:
        return _parse_view_synthesis_content(
            content, valid_ids=valid_ids, char_budget=char_budget
        )

    return call_json_completion(
        client=client,
        model=model,
        operation="memory_view_synthesis",
        timeout=45,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": prompt.replace("{current_datetime}", now_str()),
            },
            {"role": "user", "content": user_content},
        ],
        parser=parser,
        trace_context=trace_context,
    )


# Model meta-discourse: explanations/disclaimers/self-evaluation about its own
# output. It must never reach the user-facing portrait, so drop any paragraph
# whose text hits this. ^注意 is start-anchored (mid-sentence 注意 is fine); the
# rest match anywhere.
_VIEW_META_RE = re.compile(r"^注意[：:]|以上叙事|未添加任何虚构|根据(提供|给定)的")


def _join_paragraphs_within_budget(paragraphs: list[str], char_budget: int) -> str:
    """Join paragraphs with blank lines, dropping whole trailing paragraphs once
    the budget would overflow — never hard-cutting a sentence mid-way."""
    selected: list[str] = []
    for para in paragraphs:
        candidate = "\n\n".join(selected + [para])
        if len(candidate) > char_budget:
            break
        selected.append(para)
    return "\n\n".join(selected)


def _parse_view_synthesis_content(
    content: str | None,
    *,
    valid_ids: set[str],
    char_budget: int,
) -> str | None:
    """Parse the {"paragraphs":[{"text","unit_ids"}]} response into a verifiable
    portrait body. A paragraph survives only if it cites a non-empty set of KNOWN
    unit ids (subset of ``valid_ids``) and its text is not meta-discourse; the
    kept set is count-capped to len(valid_ids)+1 and whole-paragraph budget
    trimmed. Any parse failure or a fully-stripped result returns None so the
    caller falls back to the deterministic template."""
    content = clean_json_content(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_paragraphs = data.get("paragraphs")
    if not isinstance(raw_paragraphs, list):
        return None

    kept: list[str] = []
    for item in raw_paragraphs:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text or _VIEW_META_RE.search(text):
            continue
        raw_ids = item.get("unit_ids")
        if not isinstance(raw_ids, list):
            continue
        ids = {str(x).strip() for x in raw_ids if str(x).strip()}
        if not ids or not ids.issubset(valid_ids):
            continue
        kept.append(text)

    if not kept:
        return None
    kept = kept[: len(valid_ids) + 1]
    body = _join_paragraphs_within_budget(kept, char_budget)
    return body or None
