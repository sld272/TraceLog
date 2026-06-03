"""Reflection service for global TraceLog reviews."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from core import db, logging_service
from core import profile_service
from core.llm import reflection_router
from core.llm.types import LLMClient
from core import soul_memory_service
from core import soul_service
from core import todo_service
from core import tool_config_service
from core import vision_service


GLOBAL_DEEP_REFLECTION_TYPE = "global_deep"
SOUL_DEEP_REFLECTION_TYPE = "soul_deep"
PENDING_LIGHT_REFLECT_PREFIX = "pending_reflect:"
SOUL_THREAD_DEEP_CURSOR_PREFIX = "soul_thread_deep_cursor:"


@dataclass(frozen=True)
class DeepReflectionResult:
    id: int
    content: str
    scope_start: str
    scope_end: str
    related_post_ids: list[str]
    patch_summary: dict


@dataclass(frozen=True)
class GlobalDeepReflectionScope:
    post_ids: list[str]
    scope_start: str | None
    scope_end: str | None


@dataclass(frozen=True)
class LightReflectionResult:
    post_id: str
    entities: list[dict]
    emotions: list[dict]
    events: list[dict]
    relations: list[dict]
    importance: float


@dataclass(frozen=True)
class SoulDeepReflectionResult:
    id: int
    soul_name: str
    content: str
    scope_start: float
    scope_end: float
    interaction_count: int
    patch_summary: dict


@dataclass(frozen=True)
class SoulDeepReflectionScope:
    soul_name: str
    interaction_count: int
    scope_start: float
    scope_end: float


def trigger_light_reflection(
    post_id: str,
    client: LLMClient,
    model: str,
) -> LightReflectionResult:
    """Run light reflection for one public post and persist derived memory rows."""
    post = _get_post(post_id)
    if post is None:
        raise ValueError(f"post 不存在：{post_id}")

    data = reflection_router.call_light_reflection(
        client=client,
        model=model,
        post=_format_posts([post]),
        recent_posts=_format_posts(_load_recent_posts_before(post_id, limit=5)),
        profile=profile_service.read_profile(),
        trace_context={"post_id": post_id},
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
    client: LLMClient,
    model: str,
) -> LightReflectionResult | None:
    """Run light reflection without interrupting the user-facing post flow."""
    try:
        return trigger_light_reflection(post_id, client, model)
    except Exception as exc:
        _mark_pending_light_reflection(post_id, str(exc))
        return None


def retry_pending_light_reflections(
    client: LLMClient,
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


def preview_global_deep_reflection_scope(limit: int = 100) -> GlobalDeepReflectionScope:
    """Preview which public posts would be covered by the next global deep reflection."""
    posts = _load_posts_since_last_reflection(limit)
    if not posts:
        return GlobalDeepReflectionScope(post_ids=[], scope_start=None, scope_end=None)
    return GlobalDeepReflectionScope(
        post_ids=[row["id"] for row in posts],
        scope_start=posts[0]["ts"],
        scope_end=posts[-1]["ts"],
    )


def preview_soul_deep_reflection_scopes(limit_per_soul: int = 100) -> list[SoulDeepReflectionScope]:
    """Preview SOUL thread messages that would be covered by the next SOUL deep reflection."""
    scopes: list[SoulDeepReflectionScope] = []
    for soul in soul_service.list_enabled_souls():
        messages = _load_soul_thread_messages_since_cursor(soul.name, limit_per_soul)
        if not messages:
            continue
        scopes.append(
            SoulDeepReflectionScope(
                soul_name=soul.name,
                interaction_count=len(messages),
                scope_start=float(messages[0]["created_at"]),
                scope_end=float(max(item["created_at"] for item in messages)),
            )
        )
    return scopes


def trigger_global_deep_reflection(
    client: LLMClient,
    model: str,
    *,
    trigger: str = "manual",
    limit: int = 100,
) -> DeepReflectionResult | None:
    """Generate and store one global deep reflection for posts since the last run."""
    posts = _load_posts_since_last_reflection(limit)
    if not posts:
        return None

    profile = profile_service.read_profile()
    todos = todo_service.load_todos() if tool_config_service.is_tool_enabled("todo") else []
    related_post_ids = [row["id"] for row in posts]
    reflection_result = reflection_router.call_global_deep_reflection(
        client=client,
        model=model,
        profile=profile,
        posts=_format_posts(posts),
        todos=_format_todos(todos),
        trace_context={
            "trigger": trigger,
            "post_ids": related_post_ids,
            "post_count": len(related_post_ids),
        },
    )
    if reflection_result is None or not _is_valid_reflection(reflection_result.get("reflection_md")):
        raise ValueError("深反思内容无效或过短")

    content = reflection_result["reflection_md"].strip()
    patch_summary = _apply_profile_patches(
        reflection_result.get("patches", []),
        allowed_evidence=related_post_ids,
    )
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


def trigger_soul_deep_reflections(
    client: LLMClient,
    model: str,
    *,
    trigger: str = "manual",
    limit_per_soul: int = 100,
) -> list[SoulDeepReflectionResult]:
    """Generate SOUL-specific deep reflections from raw thread messages."""
    results: list[SoulDeepReflectionResult] = []
    for soul in soul_service.list_enabled_souls():
        messages = _load_soul_thread_messages_since_cursor(soul.name, limit_per_soul)
        if not messages:
            continue
        formatted = _format_soul_thread_messages(messages)
        evidence_ids = _soul_thread_evidence_ids(messages)
        reflection_result = reflection_router.call_soul_deep_reflection(
            client=client,
            model=model,
            soul=soul,
            interactions=formatted,
            trace_context={
                "trigger": trigger,
                "soul_name": soul.name,
                "message_count": len(messages),
                "evidence_ids": evidence_ids,
            },
        )
        if reflection_result is None:
            logging_service.log_event(
                "soul_deep_reflection_skipped",
                level="WARNING",
                soul_name=soul.name,
                reason="invalid_json",
                message_count=len(messages),
            )
            continue
        if not _is_valid_soul_reflection(reflection_result.get("reflection_md")):
            logging_service.log_event(
                "soul_deep_reflection_skipped",
                level="WARNING",
                soul_name=soul.name,
                reason="invalid_reflection",
                message_count=len(messages),
                content_length=len(str(reflection_result.get("reflection_md") or "")),
            )
            continue

        content = reflection_result["reflection_md"].strip()
        patch_summary = _apply_soul_memory_patches(
            soul.name,
            reflection_result.get("patches", []),
            allowed_evidence=evidence_ids,
        )
        scope_start = float(messages[0]["created_at"])
        scope_end = float(max(item["created_at"] for item in messages))
        reflection_id = _insert_soul_reflection(
            soul_name=soul.name,
            content=content,
            scope_start=scope_start,
            scope_end=scope_end,
            related_evidence_ids=evidence_ids,
            trigger=trigger,
            patch_summary=patch_summary,
        )
        _set_soul_thread_deep_cursor(soul.name, _max_soul_thread_cursor(messages))
        results.append(
            SoulDeepReflectionResult(
                id=reflection_id,
                soul_name=soul.name,
                content=content,
                scope_start=scope_start,
                scope_end=scope_end,
                interaction_count=len(messages),
                patch_summary=patch_summary,
            )
        )
    return results


def _load_posts_since_last_reflection(limit: int) -> list:
    last = db.query_one(
        """
        SELECT COALESCE(scope_end, ts) AS cursor_ts
        FROM reflections
        WHERE type = ?
        ORDER BY julianday(ts) DESC, id DESC
        LIMIT 1
        """,
        (GLOBAL_DEEP_REFLECTION_TYPE,),
    )
    if last is None:
        return db.query_all(
            """
            SELECT id, ts, content
            FROM posts
            ORDER BY julianday(ts) ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        )

    return db.query_all(
        """
        SELECT id, ts, content
        FROM posts
        WHERE julianday(ts) > julianday(?)
        ORDER BY julianday(ts) ASC, id ASC
        LIMIT ?
        """,
        (last["cursor_ts"], limit),
    )


def _get_post(post_id: str):
    return db.query_one(
        """
        SELECT id, ts, content, importance, created_at
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
        WHERE julianday(ts) < julianday(?)
           OR (julianday(ts) = julianday(?) AND id < ?)
        ORDER BY julianday(ts) DESC, id DESC
        LIMIT ?
        """,
        (post["ts"], post["ts"], post_id, limit),
    )
    return list(reversed(rows))


def _format_posts(rows: list) -> str:
    parts = []
    for row in rows:
        content = str(row["content"] or "")
        vision_context = vision_service.cached_context_for_post(str(row["id"]))
        if vision_context:
            content = f"{content}\n\n{vision_context}" if content.strip() else vision_context
        parts.append(
            "---\n"
            f"id: \"{row['id']}\"\n"
            f"date: \"{row['ts']}\"\n"
            "type: \"post\"\n"
            "---\n\n"
            f"{content}"
        )
    return "\n\n---\n\n".join(parts)


def _load_soul_thread_messages_since_cursor(soul_name: str, limit: int) -> list:
    if limit <= 0:
        return []
    cursor = _get_soul_thread_deep_cursor(soul_name)
    return db.query_all(
        """
        SELECT *
        FROM (
            SELECT
                'chat' AS channel,
                chat_messages.id AS message_id,
                chat_messages.thread_id AS thread_id,
                NULL AS post_id,
                chat_messages.role AS role,
                chat_messages.content AS content,
                chat_messages.created_at AS created_at
            FROM chat_messages
            JOIN chat_threads ON chat_threads.id = chat_messages.thread_id
            WHERE chat_threads.soul_name = ?
              AND chat_messages.id > ?

            UNION ALL

            SELECT
                'comment' AS channel,
                comments.id AS message_id,
                NULL AS thread_id,
                comments.post_id AS post_id,
                comments.role AS role,
                comments.content AS content,
                comments.created_at AS created_at
            FROM comments
            WHERE comments.soul_name = ?
              AND comments.seq > 0
              AND comments.id > ?
        )
        ORDER BY created_at ASC, channel ASC, message_id ASC
        LIMIT ?
        """,
        (
            soul_name,
            cursor.get("chat_message_id", 0),
            soul_name,
            cursor.get("comment_message_id", 0),
            limit,
        ),
    )


def _format_soul_thread_messages(rows: list) -> str:
    if not rows:
        return "（暂无）"
    parts = []
    for row in rows:
        evidence_id = _soul_thread_evidence_id(row)
        post_line = f"post_id: {row['post_id']}\n" if row["post_id"] else ""
        parts.append(
            "---\n"
            f"evidence: {evidence_id}\n"
            f"channel: {row['channel']}\n"
            f"thread_id: {row['thread_id']}\n"
            f"{post_line}"
            f"role: {row['role']}\n"
            f"created_at: {row['created_at']}\n"
            "---\n\n"
            f"{row['content']}"
        )
    return "\n\n".join(parts)


def _soul_thread_evidence_ids(rows: list) -> list[str]:
    evidence: list[str] = []
    for row in rows:
        item = _soul_thread_evidence_id(row)
        if item not in evidence:
            evidence.append(item)
    return evidence


def _soul_thread_evidence_id(row) -> str:
    prefix = "chat_message" if row["channel"] == "chat" else "comment_message"
    return f"{prefix}:{row['message_id']}"


def _get_soul_thread_deep_cursor(soul_name: str) -> dict[str, int]:
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (f"{SOUL_THREAD_DEEP_CURSOR_PREFIX}{soul_name}",))
    if row is None:
        return {"chat_message_id": 0, "comment_message_id": 0}
    try:
        data = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return {"chat_message_id": 0, "comment_message_id": 0}
    if not isinstance(data, dict):
        return {"chat_message_id": 0, "comment_message_id": 0}
    return {
        "chat_message_id": _safe_int(data.get("chat_message_id")),
        "comment_message_id": _safe_int(data.get("comment_message_id")),
    }


def _max_soul_thread_cursor(rows: list) -> dict[str, int]:
    cursor = {"chat_message_id": 0, "comment_message_id": 0}
    for row in rows:
        if row["channel"] == "chat":
            cursor["chat_message_id"] = max(cursor["chat_message_id"], int(row["message_id"]))
        elif row["channel"] == "comment":
            cursor["comment_message_id"] = max(cursor["comment_message_id"], int(row["message_id"]))
    return cursor


def _set_soul_thread_deep_cursor(soul_name: str, cursor: dict[str, int]) -> None:
    with db.immediate_transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"{SOUL_THREAD_DEEP_CURSOR_PREFIX}{soul_name}", json.dumps(cursor, ensure_ascii=False)),
        )


def _safe_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _format_todos(todos: list) -> str:
    if not todos:
        return "（暂无）"
    lines = [todo_service.format_todo_for_context(item, include_status=True) for item in todos]
    return "\n".join(lines)


def _apply_light_reflection(post, data: dict) -> None:
    post_id = post["id"]
    post_ts = post["ts"]
    with db.immediate_transaction() as conn:
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
        return db.require_lastrowid(cur, "entity insert")

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
        relation_id = db.require_lastrowid(cur, "relation insert")
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
        return db.require_lastrowid(cur, "global reflection insert")


def _insert_soul_reflection(
    *,
    soul_name: str,
    content: str,
    scope_start: float,
    scope_end: float,
    related_evidence_ids: list[str],
    trigger: str,
    patch_summary: dict,
) -> int:
    ts = datetime.now().astimezone().isoformat()
    metadata = {
        "trigger": trigger,
        "op": "soul_deep_reflection",
        "soul_name": soul_name,
        "soul_memory_patch_applied": patch_summary.get("applied", 0) > 0,
        "soul_memory_patch_summary": patch_summary,
    }
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO reflections(ts, type, scope_start, scope_end, content, related_posts, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                SOUL_DEEP_REFLECTION_TYPE,
                str(scope_start),
                str(scope_end),
                content,
                json.dumps(related_evidence_ids, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        return db.require_lastrowid(cur, "soul reflection insert")


def _is_valid_reflection(content: str | None) -> bool:
    if not content:
        return False
    text = content.strip()
    return len(text) >= 20


def _is_valid_soul_reflection(content: str | None) -> bool:
    if not content:
        return False
    return len(content.strip()) >= 20


def _apply_profile_patches(patches: list, allowed_evidence: list[str] | None = None) -> dict:
    summary = {"applied": 0, "skipped": 0, "skipped_details": []}
    if not isinstance(patches, list):
        return summary
    allowed = set(allowed_evidence) if allowed_evidence is not None else None

    for patch in patches:
        if not isinstance(patch, dict):
            summary["skipped"] += 1
            summary["skipped_details"].append({"reason": "invalid_patch"})
            continue
        if allowed is not None and not _patch_evidence_allowed(patch, allowed):
            summary["skipped"] += 1
            summary["skipped_details"].append(_profile_patch_skip_detail(patch, {"status": "skipped", "reason": "invalid_evidence"}))
            continue
        result = profile_service.apply_patch(patch, source="reflector")
        status = result.get("status")
        if status in summary:
            summary[status] += 1
        else:
            summary["skipped"] += 1
        if status != "applied":
            summary["skipped_details"].append(_profile_patch_skip_detail(patch, result))
    return summary


def _apply_soul_memory_patches(
    soul_name: str,
    patches: list,
    allowed_evidence: list[str] | None = None,
) -> dict:
    summary = {"applied": 0, "skipped": 0, "skipped_details": []}
    if not isinstance(patches, list):
        return summary
    allowed = set(allowed_evidence) if allowed_evidence is not None else None

    for patch in patches:
        if not isinstance(patch, dict):
            summary["skipped"] += 1
            summary["skipped_details"].append({"reason": "invalid_patch"})
            continue
        if allowed is not None and not _patch_evidence_allowed(patch, allowed):
            summary["skipped"] += 1
            summary["skipped_details"].append(_profile_patch_skip_detail(patch, {"status": "skipped", "reason": "invalid_evidence"}))
            continue
        result = soul_memory_service.apply_patch(soul_name, patch, source="soul_deep_reflector")
        status = result.get("status")
        if status in summary:
            summary[status] += 1
        else:
            summary["skipped"] += 1
        if status != "applied":
            summary["skipped_details"].append(_profile_patch_skip_detail(patch, result))
    return summary


def _profile_patch_skip_detail(patch: dict, result: dict) -> dict:
    return {
        "reason": result.get("reason") or result.get("status") or "unknown",
        "section": patch.get("section"),
        "ops": patch.get("ops"),
        "evidence": patch.get("evidence"),
        "confidence": patch.get("confidence"),
    }


def _patch_evidence_allowed(patch: dict, allowed_evidence: set[str]) -> bool:
    evidence = patch.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    for item in evidence:
        if not isinstance(item, str) or item.strip() not in allowed_evidence:
            return False
    return True
