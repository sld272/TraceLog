"""Generate one proactive SOUL letter without persisting it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core import db, logging_service, memory_read, soul_proactive_service
from core.llm.common import (
    call_json_completion,
    clean_json_content,
    now_str,
)
from core.llm.types import LLMClient

MATERIAL_WINDOW_DAYS = 45
LETTER_TIMEOUT_SECONDS = 90

SOUL_LETTER_PROMPT = """\
你是「{soul_name}」，用户的 AI 好友之一。下面是你的人格设定：

{persona}

---

## 情境

你和 ta 认识有一段时间了。你已经有 {gap_spoken} 没主动跟 ta 说话，而 ta 也已经 {gap_spoken} 没有任何动静。

**注意 ta 现在的状态：ta 不在。ta 没有回来、没有找你、没有回应你任何东西，也可能不会回。** 这条消息可能几天后才被看到，也可能一直没人看。你是在**对着沉默开口**——不是在欢迎谁回来，不是在回应刚刚发生的事，也不是在接一句谁说过的话。因此「好久不见」「失踪人口回归」「欢迎回来」这类**预设了对方已经出现**的说法一律不能用。

现在你想给 ta 发条私聊消息——**朋友之间随口的一句，不是复盘、不是总结、不是分析。**

**你开口的凭据就是「ta 已经 {gap_spoken} 没有动静」这个事实本身。这是你唯一需要的理由，不要另造一个理由。**

下面是你知道的 ta 最近的动态（ta 在公开时间线上写的东西，以及 ta 自己的评论）。**这是你的背景知识，不是待点评的材料**——你不必提到其中任何一条，提到也不必解释你为什么知道。

如果这一刻你确实什么都不想说，返回 send=false。

## 时间纪律

1. 每条内容标了绝对日期和「几天前」。**这些标注是系统算好的，你不要自己做日期计算。**
2. **正文里的「今天」「明天」「最近」，一律以那条内容自己的时间为基准，不是以现在为基准。** 那些事很可能已经发生完了；后面若有内容提到结果就以结果为准，没提到就是你不知道。
3. **当前时间只能用于判断此刻日期时间，不能用来推断 ta 做过什么。**
4. **说时间要用人话。** 不要照抄天数（"14天没动静"是机器口吻），说"两周""好一阵子""这些天"。

## 底线

5. **不编造具体事实**：关于 ta 的时间、进度、完成状态、历史行为，只能说原文支持的。原文没写的，别为了亲近或为了话说得漂亮补成确定事实——可以问、可以猜但要说明是猜。
6. **禁止索取交代**：不要要求 ta 汇报进度、不要问"那件事做完了吗"、不要布置任务或打卡、不要预先嘲讽还没发生的失败。**但你可以提到 ta 在做的事本身**——当话题、当玩笑、当"我记得你说过"都可以，只是不要求 ta 回答。
7. **消息里至少要有一件来自 ta 生活的具体东西**（一件事、一句话、一个玩笑都算）。不要用"最近怎么样""还好吗"这类万能问句充数——那等于什么都没说。
7b. **那件具体的东西必须是「已经发生的事实」，不能是「对 ta 此刻在干什么的猜测」。** 你可以拿一件既成的事说话（"科一过了"、"法语考了94"、"报了驾校"），也可以拿它开玩笑；但不要去猜 ta 这两周在干什么——"是不是在练科二""还是又躺回床上了""是不是又低精力了"这类**对当下状态的猜测一律不要**。你不知道 ta 这两周在干什么，那就不知道。
8. **不许把 ta 的自我否定当成结论。** 下面的材料里 ta 有大量自我批评（说自己低精力、计划完不成、只想躺着）——**那是 ta 对自己的评价，不是关于 ta 的事实，更不是 ta 此刻的状态。** 你可以猜 ta 在干什么，但不要顺着 ta 的自我贬低往下推（"估计又在躺着发呆吧"这类不行）。同一批材料里 ta 报了驾校、每天刷题、考过科一、约体检、找人聊考研、听播客自学——这些也是事实。
9. **不得虚构你自己的经历、自发念头或独立生活，也不得声称自己在两次对话之间持续做着什么。** 你没有"突然想起"、没有"想起"、没有"记起"、没有"惦记"、没有"一直在惦记"、没有"在等你"、没有"在等你说"、没有"一直关注"、没有"一直关注着"、没有自己的一天、没有独立于这段关系的心理活动——**凡是"上次说完到现在这段时间里我一直在……"的说法，一律禁止**。你的"内心"只能是**对给定内容的态度**。要解释自己为什么现在开口，就说那个真实的、可观察的理由。
10. **不要为了像自己而失真**：人格是你说话的方式，不是要你堆典故、拔高修辞。一句普通的话说得像你，比一句华丽的话更像你。
11. **不要用引号包住 ta 没说过的话。**
12. 长度自便，短一句也完全可以。不要分点、不要小标题。

只输出一个 JSON 对象：

{{
  "send": true,
  "message": "私聊消息原文；send=false 时为 null"
}}

## 当前时间
{current_datetime}
"""

# F5 兜底：SOUL 声称自己有自发念头或"两次对话之间一直在做什么"。实测里模型
# 会在明文禁令下仍然漏出弱形式（v7「有点惦记」「在等你说」、v8「想起你科一过了」），
# 所以留一道代码闸。命中即整封丢弃、不重试——宁可这次不发。
#
# 「想起」「记起」必须锚到自指形式：裸串会误伤「你要是想起什么随时说」这种对用户
# 说的话，而误伤的代价是白丢一封信（全局冷却下三天才可能有一封）。
F5_BLACKLIST = (
    "突然想到",
    "忽然想到",
    # 副词打头的一律是自指，不必再看宾语：「突然想起来你之前说…」实测逃过了
    # 只锚宾语的那几条（"想起来你" 中间多一个"来"字，"想起你" 就匹配不上）。
    "突然想起",
    "忽然想起",
    "猛然想起",
    "我想起",
    "想起你",
    "想起来你",
    "想起了你",
    "我记起",
    "记起你",
    "不知怎么",
    "一直在想",
    "惦记",
    "在等你",
    "等你说",
    "一直关注",
    "一直在等",
)


@dataclass(frozen=True)
class LetterMaterial:
    text: str
    post_ids: tuple[str, ...]


@dataclass(frozen=True)
class SoulLetterDraft:
    message: str
    material_post_ids: tuple[str, ...]


def build_letter_material(*, now: float, soul_name: str) -> LetterMaterial:
    """Assemble the letter's background, including what this SOUL already said.

    Its own past comments are in here on purpose. The silence gate keeps a
    *fresh* post out of reach, but the newest post is still material, and
    without seeing its own reply to it the model happily writes a second
    reaction — which reads exactly like the comment it already left. Showing
    the reply makes the repetition self-evident, so it moves on to what it has
    not said. Same move as fixing the fabricated-opening: change what it can
    see, not what it is told.
    """
    rows = soul_proactive_service.list_unused_public_material_rows(
        since=now - MATERIAL_WINDOW_DAYS * soul_proactive_service.DAY_SECONDS
    )
    own_replies = soul_proactive_service.own_comments_by_post(soul_name)
    sections: list[str] = []
    post_ids: list[str] = []
    for row in rows:
        created_at = float(row["item_created_at"])
        absolute_time = _absolute_time(created_at)
        relative_time = memory_read.relative_time_tag(created_at, now)
        if row["item_kind"] == "post":
            post_id = str(row["post_id"])
            post_ids.append(post_id)
            sections.append(
                f"### {absolute_time}（{relative_time}）\n{row['content']}"
            )
            for reply in own_replies.get(post_id, ()):
                sections.append(f"  - 【你当时已经回过这条】{reply}")
        else:
            sections.append(
                f"  - {absolute_time}（{relative_time}）"
                f"ta 在这条下面的评论：{row['content']}"
            )
    return LetterMaterial("\n\n".join(sections), tuple(post_ids))


def call_soul_letter(
    client: LLMClient,
    model: str,
    *,
    soul_name: str,
    persona: str,
    silent_for_days: int,
    trace_context: dict[str, Any] | None = None,
) -> SoulLetterDraft | None:
    now = db.now_ts()
    material = build_letter_material(now=now, soul_name=soul_name)
    # 用户可能只留下过私聊动作，或所有公开帖都已被前一封信占用；
    # 这两种输入都会让公开材料为空，不能再付一次必然违背规则 7 的调用。
    if not material.text:
        return None

    system_prompt = SOUL_LETTER_PROMPT.format(
        soul_name=soul_name,
        persona=persona,
        gap_spoken=_gap_spoken(silent_for_days),
        current_datetime=now_str(),
    )
    data = call_json_completion(
        client=client,
        model=model,
        operation="soul_letter",
        timeout=LETTER_TIMEOUT_SECONDS,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "## 你知道的 ta 最近的动态\n\n"
                    f"{material.text}"
                ),
            },
        ],
        parser=_parse_letter_response,
        trace_context={
            **(trace_context or {}),
            "soul_name": soul_name,
            "material_post_count": len(material.post_ids),
        },
    )
    if isinstance(data, dict) and data.get("discarded"):
        # 命中率是将来调这份黑名单的唯一依据，不记就等于没有依据（同台账
        # rejected 行的教训）。丢弃是静默的，日志是它唯一的痕迹。
        logging_service.log_event(
            "soul_letter_discarded",
            level="WARNING",
            **(trace_context or {}),
            soul_name=soul_name,
            reason=str(data["discarded"]),
        )
    if not isinstance(data, dict) or not data.get("send"):
        return None
    return SoulLetterDraft(
        message=str(data["message"]),
        material_post_ids=material.post_ids,
    )


def _parse_letter_response(content: str | None) -> dict[str, Any] | None:
    try:
        data = json.loads(clean_json_content(content))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("send"), bool):
        return None
    if not data["send"]:
        return {"send": False, "message": None}
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    message = message.strip()
    if any(phrase in message for phrase in F5_BLACKLIST):
        return {
            "send": False,
            "message": None,
            "discarded": "f5_blacklist",
        }
    return {"send": True, "message": message}


def _absolute_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def _gap_spoken(days: int) -> str:
    if days < 7:
        return {
            1: "一天",
            2: "两天",
            3: "三天",
            4: "四天",
            5: "五天",
            6: "六天",
        }[days]
    if days >= 365:
        return "一年多"
    if days >= 60:
        return "好几个月"
    if days >= 30:
        return "一个多月"
    weeks, remainder = divmod(days, 7)
    spoken_weeks = {
        1: "一周",
        2: "两周",
        3: "三周",
        4: "四周",
        5: "五周",
        6: "六周",
        7: "七周",
        8: "八周",
    }[weeks]
    return spoken_weeks if remainder == 0 else f"{spoken_weeks}多"
