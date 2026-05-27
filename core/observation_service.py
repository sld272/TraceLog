"""Observation persistence and boundary-aware listing helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from core import db

OBSERVATION_TYPES = {
    "preference",
    "correction",
    "convention",
    "decision",
    "insight",
    "pattern",
    "state",
    "relationship",
    "todo_signal",
}
SOURCE_CHANNELS = {"post", "comment", "comment_thread", "chat", "reflection", "todo"}
VISIBILITY_SCOPES = {"global", "post_visible", "soul_scoped", "private_blocked"}
EVIDENCE_ACCESS = {"all", "post_visible", "source_soul_only", "none"}
SOURCE_TYPES = {"post", "comment", "comment_message", "chat_message", "todo", "reflection"}


def create_observation(observation: dict[str, Any], sources: list[dict[str, Any]]) -> int:
    """Create one observation and its source evidence rows."""
    normalized = _normalize_observation(observation)
    normalized_sources = [_normalize_source(source, normalized["visibility_scope"]) for source in sources]
    if not normalized_sources:
        raise ValueError("observation must have at least one source")

    now = db.now_ts()
    with db.immediate_transaction() as conn:
        return _insert_observation(conn, normalized, normalized_sources, now=now)


def replace_post_observations(
    post_id: str,
    observations: list[dict[str, Any]],
    *,
    observed_at: float | None = None,
    excerpt: str | None = None,
    conn=None,
) -> list[int]:
    """Replace light-reflection observations extracted from one public post."""
    source_id = str(post_id).strip()
    if not source_id:
        raise ValueError("post_id is required")

    normalized_items = [
        _normalize_observation(
            {
                **item,
                "source_channel": "post",
                "visibility_scope": "global",
                "observed_at": observed_at,
            }
        )
        for item in observations
    ]
    source = _normalize_source(
        {
            "source_type": "post",
            "source_id": source_id,
            "excerpt": excerpt,
            "evidence_access": "all",
            "metadata": {"extractor": "light_reflection"},
        },
        "global",
    )

    if conn is not None:
        return _replace_post_observations(conn, source_id, normalized_items, source)

    with db.immediate_transaction() as write_conn:
        return _replace_post_observations(write_conn, source_id, normalized_items, source)


def save_extraction_batch(
    *,
    source_kind: str,
    source_key: str,
    cursor_value: str,
    observations: list[dict[str, Any]],
    source_channel: str,
    visibility_scope: str,
    source_type: str,
    evidence_access: str,
    source_excerpt_by_id: dict[int, str],
    source_observed_at_by_id: dict[int, float],
    scope_post_id: str | None = None,
    scope_soul_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[int]:
    """Save extracted observations and advance the source cursor atomically."""
    if not source_kind.strip() or not source_key.strip():
        raise ValueError("source_kind and source_key are required")
    _require_choice({"source_channel": source_channel}, "source_channel", SOURCE_CHANNELS)
    _require_choice({"visibility_scope": visibility_scope}, "visibility_scope", VISIBILITY_SCOPES)
    _require_choice({"source_type": source_type}, "source_type", SOURCE_TYPES)
    _require_choice({"evidence_access": evidence_access}, "evidence_access", EVIDENCE_ACCESS)

    normalized_items: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for item in observations:
        source_ids = _valid_source_ids(item.get("source_message_ids"), source_excerpt_by_id)
        if not source_ids:
            continue
        observed_at = max(source_observed_at_by_id.get(source_id, db.now_ts()) for source_id in source_ids)
        normalized = _normalize_observation(
            {
                **item,
                "source_channel": source_channel,
                "visibility_scope": visibility_scope,
                "scope_post_id": scope_post_id,
                "scope_soul_name": scope_soul_name,
                "observed_at": observed_at,
                "metadata": {
                    **(metadata or {}),
                    "extractor": "thread_observation",
                    "source_kind": source_kind,
                    "source_key": str(source_key),
                },
            }
        )
        sources = [
            _normalize_source(
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "excerpt": source_excerpt_by_id[source_id],
                    "evidence_access": evidence_access,
                    "metadata": {"extractor": "thread_observation"},
                },
                visibility_scope,
            )
            for source_id in source_ids
        ]
        normalized_items.append((normalized, sources))

    now = db.now_ts()
    with db.immediate_transaction() as conn:
        observation_ids = [
            _insert_observation(conn, item, sources, now=now)
            for item, sources in normalized_items
        ]
        _set_cursor(
            conn,
            source_kind.strip(),
            source_key.strip(),
            str(cursor_value),
            now=now,
            metadata=metadata,
        )
    return observation_ids


def get_observation(observation_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM observations WHERE id = ?", (observation_id,))
    if row is None:
        return None
    data = _row_to_dict(row)
    data["sources"] = [
        _row_to_dict(source)
        for source in db.query_all(
            """
            SELECT source_type, source_id, excerpt, evidence_access, created_at, metadata
            FROM observation_sources
            WHERE observation_id = ?
            ORDER BY source_type, source_id
            """,
            (observation_id,),
        )
    ]
    return data


def list_active_observations(
    *,
    visibility_scope: str | None = None,
    scope_post_id: str | None = None,
    scope_soul_name: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["status = 'active'"]
    params: list[Any] = []
    if visibility_scope is not None:
        if visibility_scope not in VISIBILITY_SCOPES:
            raise ValueError(f"invalid visibility_scope: {visibility_scope}")
        clauses.append("visibility_scope = ?")
        params.append(visibility_scope)
    if scope_post_id is not None:
        clauses.append("scope_post_id = ?")
        params.append(scope_post_id)
    if scope_soul_name is not None:
        clauses.append("scope_soul_name = ?")
        params.append(scope_soul_name)

    rows = db.query_all(
        f"""
        SELECT *
        FROM observations
        WHERE {" AND ".join(clauses)}
        ORDER BY observed_at DESC, id DESC
        """,
        tuple(params),
    )
    return [_row_to_dict(row) for row in rows]


def mark_merged(observation_id: int, merged_into: int) -> None:
    _mark_status(observation_id, "merged", merged_into=merged_into)


def mark_superseded(observation_id: int, superseded_by: int) -> None:
    _mark_status(observation_id, "superseded", superseded_by=superseded_by)


def archive_observation(observation_id: int) -> None:
    _mark_status(observation_id, "archived")


def get_cursor(source_kind: str, source_key: str) -> str | None:
    row = db.query_one(
        """
        SELECT cursor_value
        FROM observation_cursors
        WHERE source_kind = ? AND source_key = ?
        """,
        (source_kind, source_key),
    )
    return None if row is None else str(row["cursor_value"])


def set_cursor(
    source_kind: str,
    source_key: str,
    cursor_value: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not source_kind.strip() or not source_key.strip():
        raise ValueError("source_kind and source_key are required")
    now = db.now_ts()
    with db.immediate_transaction() as conn:
        _set_cursor(conn, source_kind.strip(), source_key.strip(), str(cursor_value), now=now, metadata=metadata)


def cleanup_orphan_observations() -> int:
    """Remove sources whose raw evidence disappeared, then remove source-less observations."""
    with db.immediate_transaction() as conn:
        _cleanup_missing_sources(conn)
        cursor = conn.execute(
            """
            DELETE FROM observations
            WHERE id IN (
                SELECT observations.id
                FROM observations
                LEFT JOIN observation_sources
                    ON observation_sources.observation_id = observations.id
                WHERE observation_sources.observation_id IS NULL
            )
            """
        )
        return cursor.rowcount if cursor.rowcount is not None else 0


def _replace_post_observations(conn, post_id: str, observations: list[dict[str, Any]], source: dict[str, Any]) -> list[int]:
    conn.execute(
        """
        DELETE FROM observations
        WHERE id IN (
            SELECT observations.id
            FROM observations
            JOIN observation_sources
                ON observation_sources.observation_id = observations.id
            WHERE observations.source_channel = 'post'
              AND observations.visibility_scope = 'global'
              AND observation_sources.source_type = 'post'
              AND observation_sources.source_id = ?
        )
        """,
        (post_id,),
    )
    now = db.now_ts()
    return [
        _insert_observation(conn, item, [source], now=now)
        for item in observations
    ]


def _insert_observation(
    conn,
    observation: dict[str, Any],
    sources: list[dict[str, Any]],
    *,
    now: float,
) -> int:
    observed_at = observation.get("observed_at") or now
    cursor = conn.execute(
        """
        INSERT INTO observations(
            type, title, summary, narrative, source_channel,
            visibility_scope, scope_post_id, scope_soul_name,
            importance, confidence, status,
            merged_into, superseded_by,
            observed_at, created_at, updated_at, metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?, ?, ?)
        """,
        (
            observation["type"],
            observation["title"],
            observation.get("summary"),
            observation["narrative"],
            observation["source_channel"],
            observation["visibility_scope"],
            observation.get("scope_post_id"),
            observation.get("scope_soul_name"),
            observation["importance"],
            observation["confidence"],
            observed_at,
            now,
            now,
            _json_or_none(observation.get("metadata")),
        ),
    )
    observation_id = db.require_lastrowid(cursor, "observation insert")
    for source in sources:
        conn.execute(
            """
            INSERT INTO observation_sources(
                observation_id, source_type, source_id,
                excerpt, evidence_access, created_at, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                source["source_type"],
                source["source_id"],
                source.get("excerpt"),
                source["evidence_access"],
                now,
                _json_or_none(source.get("metadata")),
            ),
        )
    return observation_id


def _set_cursor(
    conn,
    source_kind: str,
    source_key: str,
    cursor_value: str,
    *,
    now: float,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO observation_cursors(source_kind, source_key, cursor_value, updated_at, metadata)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, source_key) DO UPDATE SET
            cursor_value = excluded.cursor_value,
            updated_at = excluded.updated_at,
            metadata = excluded.metadata
        """,
        (
            source_kind,
            source_key,
            cursor_value,
            now,
            _json_or_none(metadata),
        ),
    )


def _valid_source_ids(value: Any, source_excerpt_by_id: dict[int, str]) -> list[int]:
    if not isinstance(value, list):
        return []
    valid_ids = []
    for raw_id in value:
        try:
            source_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if source_id in source_excerpt_by_id and source_id not in valid_ids:
            valid_ids.append(source_id)
    return valid_ids


def _normalize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    item = dict(observation)
    _require_choice(item, "type", OBSERVATION_TYPES)
    _require_choice(item, "source_channel", SOURCE_CHANNELS)
    _require_choice(item, "visibility_scope", VISIBILITY_SCOPES)
    for key in ("title", "narrative"):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
        item[key] = value.strip()
    summary = item.get("summary")
    if summary is not None:
        if not isinstance(summary, str):
            raise ValueError("summary must be a string")
        item["summary"] = summary.strip() or None
    item["importance"] = _normalized_score(item.get("importance", 0.5), "importance")
    item["confidence"] = _normalized_score(item.get("confidence", 0.5), "confidence")
    _validate_scope(item)
    return item


def _normalize_source(source: dict[str, Any], visibility_scope: str) -> dict[str, Any]:
    item = dict(source)
    _require_choice(item, "source_type", SOURCE_TYPES)
    _require_choice(item, "evidence_access", EVIDENCE_ACCESS)
    source_id = item.get("source_id")
    if source_id is None or not str(source_id).strip():
        raise ValueError("source_id is required")
    item["source_id"] = str(source_id).strip()
    excerpt = item.get("excerpt")
    if excerpt is not None and not isinstance(excerpt, str):
        raise ValueError("excerpt must be a string")
    if visibility_scope == "private_blocked" and item["evidence_access"] != "none":
        raise ValueError("private_blocked observations require evidence_access=none")
    return item


def _validate_scope(item: dict[str, Any]) -> None:
    visibility = item["visibility_scope"]
    if visibility == "post_visible" and not item.get("scope_post_id"):
        raise ValueError("post_visible observations require scope_post_id")
    if visibility == "soul_scoped" and not item.get("scope_soul_name"):
        raise ValueError("soul_scoped observations require scope_soul_name")
    if visibility == "global" and item.get("scope_soul_name"):
        raise ValueError("global observations cannot set scope_soul_name")


def _mark_status(
    observation_id: int,
    status: str,
    *,
    merged_into: int | None = None,
    superseded_by: int | None = None,
) -> None:
    now = db.now_ts()
    with db.immediate_transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE observations
            SET status = ?, merged_into = ?, superseded_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, merged_into, superseded_by, now, observation_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"observation not found: {observation_id}")


def _cleanup_missing_sources(conn) -> None:
    statements: Iterable[tuple[str, tuple[Any, ...]]] = (
        (
            """
            DELETE FROM observation_sources
            WHERE source_type = 'post'
              AND NOT EXISTS (SELECT 1 FROM posts WHERE posts.id = observation_sources.source_id)
            """,
            (),
        ),
        (
            """
            DELETE FROM observation_sources
            WHERE source_type = 'comment'
              AND NOT EXISTS (
                  SELECT 1 FROM comments
                  WHERE CAST(comments.id AS TEXT) = observation_sources.source_id
              )
            """,
            (),
        ),
        (
            """
            DELETE FROM observation_sources
            WHERE source_type = 'comment_message'
              AND NOT EXISTS (
                  SELECT 1 FROM comment_messages
                  WHERE CAST(comment_messages.id AS TEXT) = observation_sources.source_id
              )
            """,
            (),
        ),
        (
            """
            DELETE FROM observation_sources
            WHERE source_type = 'chat_message'
              AND NOT EXISTS (
                  SELECT 1 FROM chat_messages
                  WHERE CAST(chat_messages.id AS TEXT) = observation_sources.source_id
              )
            """,
            (),
        ),
        (
            """
            DELETE FROM observation_sources
            WHERE source_type = 'todo'
              AND NOT EXISTS (SELECT 1 FROM todos WHERE todos.id = observation_sources.source_id)
            """,
            (),
        ),
        (
            """
            DELETE FROM observation_sources
            WHERE source_type = 'reflection'
              AND NOT EXISTS (
                  SELECT 1 FROM reflections
                  WHERE CAST(reflections.id AS TEXT) = observation_sources.source_id
              )
            """,
            (),
        ),
    )
    for sql, params in statements:
        conn.execute(sql, params)


def _require_choice(item: dict[str, Any], key: str, choices: set[str]) -> None:
    value = item.get(key)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"invalid {key}: {value}")


def _normalized_score(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    score = float(value)
    if score < 0.0 or score > 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return score


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _row_to_dict(row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
