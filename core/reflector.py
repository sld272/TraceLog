"""Reflection service for global TraceLog reviews."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import memory
import router
from core import db
from core import profile_service

if TYPE_CHECKING:
    from openai import OpenAI


GLOBAL_DEEP_REFLECTION_TYPE = "global_deep"
PENDING_LIGHT_REFLECT_PREFIX = "pending_reflect:"


@dataclass(frozen=True)
class DeepReflectionResult:
    id: int
    content: str
    scope_start: str
    scope_end: str
    related_post_ids: list[str]
    patch_summary: dict


@dataclass(frozen=True)
class LightReflectionResult:
    post_id: str
    entities: list[dict]
    emotions: list[dict]
    events: list[dict]
    relations: list[dict]
    importance: float


def trigger_light_reflection(
    post_id: str,
    client: "OpenAI",
    model: str,
) -> LightReflectionResult:
    """Run light reflection for one public post and persist derived memory rows."""
    post = _get_post(post_id)
    if post is None:
        raise ValueError(f"post 不存在：{post_id}")

    data = router.call_light_reflection(
        client=client,
        model=model,
        post=_format_posts([post]),
        recent_posts=_format_posts(_load_recent_posts_before(post_id, limit=5)),
        profile=memory.read_profile(),
    )
    if data is None:
        raise ValueError("轻反思没有返回有效 JSON")

    _apply_light_reflection(post, data)
    _clear_pending_light_reflection(post_id)
    return LightReflectionResult(
        post_id=post_id,
        entities=data["entities"],
        emotions=data["emotions"],
        events=data["events"],
        relations=data["relations"],
        importance=data["importance"],
    )


def run_light_reflection_safely(
    post_id: str,
    client: "OpenAI",
    model: str,
) -> LightReflectionResult | None:
    """Run light reflection without interrupting the user-facing post flow."""
    try:
        return trigger_light_reflection(post_id, client, model)
    except Exception as exc:
        _mark_pending_light_reflection(post_id, str(exc))
        return None


def retry_pending_light_reflections(
    client: "OpenAI",
    model: str,
    limit: int | None = None,
) -> int:
    """Retry failed light reflections recorded in meta."""
    sql = """
        SELECT key
        FROM meta
        WHERE key LIKE ?
        ORDER BY key
    """
    params: tuple = (f"{PENDING_LIGHT_REFLECT_PREFIX}%",)
    if limit is not None:
        sql += " LIMIT ?"
        params = (f"{PENDING_LIGHT_REFLECT_PREFIX}%", limit)

    fixed = 0
    for row in db.query_all(sql, params):
        post_id = str(row["key"])[len(PENDING_LIGHT_REFLECT_PREFIX):]
        try:
            trigger_light_reflection(post_id, client, model)
            fixed += 1
        except Exception:
            continue
    return fixed


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
    related_post_ids = [row["id"] for row in posts]
    reflection_result = router.call_global_deep_reflection(
        client=client,
        model=model,
        profile=profile,
        posts=_format_posts(posts),
        light_summary=_format_light_summary(related_post_ids),
        todos=_format_todos(todos),
    )
    if reflection_result is None or not _is_valid_reflection(reflection_result.get("reflection_md")):
        raise ValueError("深反思内容无效或过短")

    content = reflection_result["reflection_md"].strip()
    patch_summary = _apply_profile_patches(reflection_result.get("patches", []))
    reflection_id = _insert_reflection(
        content=content,
        scope_start=posts[0]["ts"],
        scope_end=posts[-1]["ts"],
        related_post_ids=related_post_ids,
        trigger=trigger,
        patch_summary=patch_summary,
    )
    return DeepReflectionResult(
        id=reflection_id,
        content=content,
        scope_start=posts[0]["ts"],
        scope_end=posts[-1]["ts"],
        related_post_ids=related_post_ids,
        patch_summary=patch_summary,
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


def _get_post(post_id: str):
    return db.query_one(
        """
        SELECT id, ts, content, importance
        FROM posts
        WHERE id = ?
        """,
        (post_id,),
    )


def _load_recent_posts_before(post_id: str, limit: int) -> list:
    post = _get_post(post_id)
    if post is None:
        return []
    rows = db.query_all(
        """
        SELECT id, ts, content
        FROM posts
        WHERE ts < ? OR (ts = ? AND id < ?)
        ORDER BY ts DESC, id DESC
        LIMIT ?
        """,
        (post["ts"], post["ts"], post_id, limit),
    )
    return list(reversed(rows))


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


def _format_light_summary(post_ids: list[str]) -> str:
    if not post_ids:
        return "（暂无）"
    placeholders = ",".join("?" for _ in post_ids)
    posts = db.query_all(
        f"""
        SELECT id, ts, importance
        FROM posts
        WHERE id IN ({placeholders})
        ORDER BY ts ASC, id ASC
        """,
        tuple(post_ids),
    )
    if not posts:
        return "（暂无）"

    parts = []
    for post in posts:
        entities = db.query_all(
            """
            SELECT e.type, e.name, pe.role
            FROM post_entities pe
            JOIN entities e ON e.id = pe.entity_id
            WHERE pe.post_id = ?
            ORDER BY e.type, e.name
            """,
            (post["id"],),
        )
        emotions = db.query_all(
            """
            SELECT label, intensity
            FROM emotions
            WHERE post_id = ?
            ORDER BY intensity DESC, label
            """,
            (post["id"],),
        )
        events = db.query_all(
            """
            SELECT summary, category
            FROM events
            WHERE post_id = ?
            ORDER BY id ASC
            """,
            (post["id"],),
        )
        entity_text = "、".join(f"{row['name']}({row['type']}/{row['role']})" for row in entities) or "无"
        emotion_text = "、".join(f"{row['label']} {row['intensity']:.2f}" for row in emotions) or "无"
        event_text = "、".join(f"{row['summary']}({row['category']})" for row in events) or "无"
        parts.append(
            f"## {post['id']} {post['ts']}\n"
            f"- importance: {post['importance'] or 0.5:.2f}\n"
            f"- entities: {entity_text}\n"
            f"- emotions: {emotion_text}\n"
            f"- events: {event_text}"
        )
    return "\n\n".join(parts)


def _apply_light_reflection(post, data: dict) -> None:
    post_id = post["id"]
    post_ts = post["ts"]
    with db.transaction() as conn:
        _remove_old_light_rows(conn, post_id)
        entity_ids_by_name = {}
        for entity in data.get("entities", []):
            entity_id = _upsert_entity(conn, entity, post_ts)
            entity_ids_by_name[entity["name"]] = entity_id
            conn.execute(
                """
                INSERT OR IGNORE INTO post_entities(post_id, entity_id, role)
                VALUES (?, ?, ?)
                """,
                (post_id, entity_id, entity.get("role") or "mentioned"),
            )

        for emotion in data.get("emotions", []):
            conn.execute(
                """
                INSERT INTO emotions(post_id, label, intensity)
                VALUES (?, ?, ?)
                ON CONFLICT(post_id, label) DO UPDATE SET
                    intensity = MAX(emotions.intensity, excluded.intensity)
                """,
                (post_id, emotion["label"], emotion["intensity"]),
            )

        for event in data.get("events", []):
            conn.execute(
                """
                INSERT INTO events(post_id, ts, summary, category, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    event.get("ts") or post_ts,
                    event["summary"],
                    event.get("category"),
                    json.dumps({"source": "light_reflection"}, ensure_ascii=False),
                ),
            )

        for relation in data.get("relations", []):
            entity_a = entity_ids_by_name.get(relation["a"])
            entity_b = entity_ids_by_name.get(relation["b"])
            if entity_a is None or entity_b is None or entity_a == entity_b:
                continue
            _upsert_relation(conn, post_id, entity_a, entity_b, relation, post_ts)

        conn.execute(
            "UPDATE posts SET importance = ?, updated_at = ? WHERE id = ?",
            (data.get("importance", 0.5), db.now_ts(), post_id),
        )


def _remove_old_light_rows(conn, post_id: str) -> None:
    old_entities = conn.execute(
        "SELECT entity_id FROM post_entities WHERE post_id = ?",
        (post_id,),
    ).fetchall()
    for row in old_entities:
        conn.execute(
            """
            UPDATE entities
            SET mention_count = MAX(mention_count - 1, 0)
            WHERE id = ?
            """,
            (row["entity_id"],),
        )

    old_relations = conn.execute(
        "SELECT relation_id, delta FROM relations_log WHERE post_id = ?",
        (post_id,),
    ).fetchall()
    for row in old_relations:
        conn.execute(
            """
            UPDATE relations
            SET strength = MIN(MAX(strength - ?, 0.0), 1.0)
            WHERE id = ?
            """,
            (row["delta"], row["relation_id"]),
        )

    conn.execute("DELETE FROM relations_log WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM post_entities WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM emotions WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM events WHERE post_id = ?", (post_id,))


def _upsert_entity(conn, entity: dict, post_ts: str) -> int:
    row = conn.execute(
        """
        SELECT id, aliases
        FROM entities
        WHERE type = ? AND name = ?
        """,
        (entity["type"], entity["name"]),
    ).fetchone()
    aliases = _merge_aliases(row["aliases"] if row else None, entity.get("aliases", []))
    metadata = json.dumps({"source": "light_reflection"}, ensure_ascii=False)
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO entities(type, name, aliases, first_seen, last_seen, mention_count, metadata)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (entity["type"], entity["name"], json.dumps(aliases, ensure_ascii=False), post_ts, post_ts, metadata),
        )
        return int(cur.lastrowid)

    conn.execute(
        """
        UPDATE entities
        SET aliases = ?, last_seen = ?, mention_count = mention_count + 1
        WHERE id = ?
        """,
        (json.dumps(aliases, ensure_ascii=False), post_ts, row["id"]),
    )
    return int(row["id"])


def _merge_aliases(existing_json: str | None, new_aliases: list[str]) -> list[str]:
    try:
        existing = json.loads(existing_json or "[]")
    except json.JSONDecodeError:
        existing = []
    merged = []
    for alias in [*existing, *new_aliases]:
        if isinstance(alias, str) and alias.strip() and alias.strip() not in merged:
            merged.append(alias.strip())
    return merged


def _upsert_relation(conn, post_id: str, entity_a: int, entity_b: int, relation: dict, post_ts: str) -> None:
    a, b = sorted((entity_a, entity_b))
    rel_type = relation["rel_type"]
    delta = relation["strength_delta"]
    metadata = json.dumps({"source": "light_reflection"}, ensure_ascii=False)
    row = conn.execute(
        """
        SELECT id
        FROM relations
        WHERE entity_a = ? AND entity_b = ? AND rel_type = ?
        """,
        (a, b, rel_type),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO relations(entity_a, entity_b, rel_type, strength, last_seen, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (a, b, rel_type, _clamp(0.5 + delta), post_ts, metadata),
        )
        relation_id = int(cur.lastrowid)
    else:
        relation_id = int(row["id"])
        conn.execute(
            """
            UPDATE relations
            SET strength = MIN(MAX(strength + ?, 0.0), 1.0),
                last_seen = ?
            WHERE id = ?
            """,
            (delta, post_ts, relation_id),
        )
    conn.execute(
        """
        INSERT OR REPLACE INTO relations_log(post_id, relation_id, delta, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (post_id, relation_id, delta, db.now_ts()),
    )


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _mark_pending_light_reflection(post_id: str, error: str) -> None:
    payload = {
        "post_id": post_id,
        "error": error,
        "created_at": db.now_ts(),
    }
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (f"{PENDING_LIGHT_REFLECT_PREFIX}{post_id}", json.dumps(payload, ensure_ascii=False)),
    )


def _clear_pending_light_reflection(post_id: str) -> None:
    db.execute("DELETE FROM meta WHERE key = ?", (f"{PENDING_LIGHT_REFLECT_PREFIX}{post_id}",))


def _insert_reflection(
    *,
    content: str,
    scope_start: str,
    scope_end: str,
    related_post_ids: list[str],
    trigger: str,
    patch_summary: dict,
) -> int:
    ts = datetime.now().astimezone().isoformat()
    metadata = {
        "trigger": trigger,
        "op": "global_deep_reflection",
        "profile_patch_applied": patch_summary.get("applied", 0) > 0,
        "profile_patch_summary": patch_summary,
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


def _apply_profile_patches(patches: list) -> dict:
    summary = {"applied": 0, "skipped": 0}
    if not isinstance(patches, list):
        return summary

    for patch in patches:
        if not isinstance(patch, dict):
            summary["skipped"] += 1
            continue
        result = profile_service.apply_patch(patch, source="reflector")
        status = result.get("status")
        if status in summary:
            summary[status] += 1
        else:
            summary["skipped"] += 1
    return summary
