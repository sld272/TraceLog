"""LLM generation for SOUL Markdown files."""

from __future__ import annotations

import json
from datetime import datetime

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
3. 至少包含这些标题：人格定位、说话方式、互动边界、回应原则。
4. 不要承诺拥有真实经历、现实身份、专业资质或真实记忆。
5. 不要替用户做医疗、法律、金融等高风险专业决定。
"""


USER_TEMPLATE = """\
SOUL 名称：{name}
创建日期：{created_at}

用户灵感：
{inspiration}

请生成一个完整的 SOUL Markdown 文件。
"""


def generate_soul(
    *,
    name: str,
    inspiration: str,
    client: LLMClient,
    model: str,
) -> dict | None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_TEMPLATE.format(
                name=name,
                created_at=datetime.now().astimezone().date().isoformat(),
                inspiration=inspiration,
            ),
        },
    ]
    return call_json_completion(
        client=client,
        model=model,
        operation="generate_soul",
        messages=messages,
        parser=_parse_soul,
        timeout=45,
        response_format={"type": "json_object"},
        trace_context={"soul_name": name},
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
