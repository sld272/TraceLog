"""Reflection service for global TraceLog reviews."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import memory
import router
from core import db

if TYPE_CHECKING:
    from openai import OpenAI


GLOBAL_DEEP_REFLECTION_TYPE = "global_deep"


@dataclass(frozen=True)
class DeepReflectionResult:
    id: int
    content: str
    scope_start: str
    scope_end: str
    related_post_ids: list[str]


def trigger_global_deep_reflection(
    client: "OpenAI",
    model: str,
    *,
    trigger: str = "manual",
    limit: int = 100,
) -> DeepReflectionResult | None:
    """Generate and store one global deep reflection for posts since the last run."""
    posts = _load_posts_since_last_reflection(limit)
    if not posts:
        return None

    profile = memory.read_profile()
    todos = memory.load_todos()
    reflection = router.call_global_deep_reflection(
        client=client,
        model=model,
        profile=profile,
        posts=_format_posts(posts),
        todos=_format_todos(todos),
    )
    if not _is_valid_reflection(reflection):
        raise ValueError("深反思内容无效或过短")

    content = reflection.strip()
    related_post_ids = [row["id"] for row in posts]
    reflection_id = _insert_reflection(
        content=content,
        scope_start=posts[0]["ts"],
        scope_end=posts[-1]["ts"],
        related_post_ids=related_post_ids,
        trigger=trigger,
    )
    return DeepReflectionResult(
        id=reflection_id,
        content=content,
        scope_start=posts[0]["ts"],
        scope_end=posts[-1]["ts"],
        related_post_ids=related_post_ids,
    )


def _load_posts_since_last_reflection(limit: int) -> list:
    last = db.query_one(
        """
        SELECT COALESCE(scope_end, ts) AS cursor_ts
        FROM reflections
        WHERE type = ?
        ORDER BY ts DESC, id DESC
        LIMIT 1
        """,
        (GLOBAL_DEEP_REFLECTION_TYPE,),
    )
    if last is None:
        return db.query_all(
            """
            SELECT id, ts, content
            FROM posts
            ORDER BY ts ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        )

    return db.query_all(
        """
        SELECT id, ts, content
        FROM posts
        WHERE ts > ?
        ORDER BY ts ASC, id ASC
        LIMIT ?
        """,
        (last["cursor_ts"], limit),
    )


def _format_posts(rows: list) -> str:
    parts = []
    for row in rows:
        parts.append(
            "---\n"
            f"id: \"{row['id']}\"\n"
            f"date: \"{row['ts']}\"\n"
            "type: \"post\"\n"
            "---\n\n"
            f"{row['content']}"
        )
    return "\n\n---\n\n".join(parts)


def _format_todos(todos: list) -> str:
    if not todos:
        return "（暂无）"
    lines = []
    for item in todos:
        date = item.get("date") or "无日期"
        start_time = item.get("start_time") or ""
        time_part = f" {start_time}" if start_time else ""
        status = item.get("status") or "未完成"
        lines.append(f"- [{status}] {item.get('task', '')}（{date}{time_part}）")
    return "\n".join(lines)


def _insert_reflection(
    *,
    content: str,
    scope_start: str,
    scope_end: str,
    related_post_ids: list[str],
    trigger: str,
) -> int:
    ts = datetime.now().astimezone().isoformat()
    metadata = {
        "trigger": trigger,
        "op": "global_deep_reflection",
        "profile_patch_applied": False,
    }
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO reflections(ts, type, scope_start, scope_end, content, related_posts, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                GLOBAL_DEEP_REFLECTION_TYPE,
                scope_start,
                scope_end,
                content,
                json.dumps(related_post_ids, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def _is_valid_reflection(content: str | None) -> bool:
    if not content:
        return False
    text = content.strip()
    return len(text) >= 20 and ("##" in text or "- " in text or "\n" in text)
