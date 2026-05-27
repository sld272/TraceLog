"""LLM calls for the optional TodoTool."""

from __future__ import annotations

import json

from core.llm.common import call_json_completion, clean_json_content, now_str
from core.llm.types import LLMClient


# 引擎 1.5：Todo Tool

TODO_TOOL_PROMPT = """\
你是 TraceLog 拾迹的 TodoTool。你的任务是只从一条公开 post 中抽取可执行待办。

## JSON 输出格式强制要求
你必须且只能输出一个标准 JSON 对象，不要包含 Markdown 代码块或解释文字。

{
  "todos_to_upsert": [
    {
      "id": "已有待办的 id（新增待办时必须为 null）",
      "task": "明确的待办描述",
      "date": "YYYY-MM-DD 或 null",
      "start_time": "HH:MM 或 null",
      "end_time": "HH:MM 或 null",
      "status": "未完成/已完成"
    }
  ],
  "todos_to_delete": [
    {
      "id": "存在于当前待办列表中的确切 id"
    }
  ]
}

## 严格规则
1. 只处理目标 post 中用户明确表达的任务、DDL、提醒、取消或完成状态。
2. 不要把情绪、抱怨、建议、愿望或 SOUL 的回复转成待办。
3. 如果要更新状态或删除任务，必须引用「当前待办」中真实存在的 id；找不到就不要输出。
4. 新增待办 id 必须为 null。
5. date 使用 YYYY-MM-DD；start_time/end_time 使用 24 小时 HH:MM；用户未明确提及时填 null。
6. status 仅允许 "未完成" 或 "已完成"。
7. 没有待办变化时，两个数组都输出 []。

## 当前时间
{current_datetime}
请以此为绝对基准，计算并将“明天、后天、下周三”等相对时间转化为 YYYY-MM-DD。
"""


def call_todo_tool(
    client: LLMClient,
    model: str,
    *,
    post: str,
    active_todos: str,
    trace_context: dict | None = None,
) -> dict | None:
    """Extract todo changes from one public post."""
    user_content = (
        f"## 当前待办\n\n{active_todos or '（暂无）'}\n\n"
        "---\n\n"
        f"## 目标公开 post\n\n{post}"
    )

    return call_json_completion(
        client=client,
        model=model,
        operation="todo_tool",
        timeout=30,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": TODO_TOOL_PROMPT.replace("{current_datetime}", now_str())},
            {"role": "user", "content": user_content},
        ],
        parser=_parse_todo_tool_content,
        trace_context=trace_context,
    )


def _parse_todo_tool_content(content: str | None) -> dict | None:
    content = clean_json_content(content)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    return {
        "todos_to_upsert": _normalize_todo_upserts(data.get("todos_to_upsert")),
        "todos_to_delete": _normalize_todo_deletes(data.get("todos_to_delete")),
    }


def _normalize_todo_upserts(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            continue
        status = item.get("status", "未完成")
        if status not in ("未完成", "已完成"):
            status = "未完成"
        normalized.append(
            {
                "id": item.get("id"),
                "task": task.strip(),
                "date": item.get("date"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "status": status,
            }
        )
    return normalized


def _normalize_todo_deletes(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if isinstance(item, dict) and item.get("id"):
            normalized.append({"id": item.get("id")})
    return normalized

