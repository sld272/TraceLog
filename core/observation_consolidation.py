"""Boundary-safe observation consolidation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from core import db, observation_service
from core.llm import reflection_router
from core.llm.types import LLMClient

CURSOR_PREFIX = "observation_consolidation_cursor:"


@dataclass(frozen=True)
class ConsolidationScope:
    bucket_key: str
    visibility_scope: str
    scope_value: str | None
    active_count: int
    pending_count: int
    max_observation_id: int


@dataclass(frozen=True)
class ConsolidationRunResult:
    bucket_count: int
    merged_count: int
    superseded_count: int
    skipped_count: int
    invalid_count: int


def preview_consolidation_scopes(limit_per_bucket: int = 80) -> list[ConsolidationScope]:
    """Return active observation buckets with ids newer than their consolidation cursor."""
    scopes = []
    for bucket in _all_active_buckets():
        cursor = _get_cursor(bucket.bucket_key)
        pending_count = min(bucket.pending_count, limit_per_bucket)
        if bucket.max_observation_id > cursor and pending_count > 0:
            scopes.append(
                ConsolidationScope(
                    bucket_key=bucket.bucket_key,
                    visibility_scope=bucket.visibility_scope,
                    scope_value=bucket.scope_value,
                    active_count=bucket.active_count,
                    pending_count=pending_count,
                    max_observation_id=bucket.max_observation_id,
                )
            )
    return scopes


def run_observation_consolidation(
    client: LLMClient,
    model: str,
    *,
    limit_per_bucket: int = 80,
) -> ConsolidationRunResult:
    """Run exact and semantic consolidation within each pending boundary bucket."""
    bucket_count = 0
    merged_count = 0
    superseded_count = 0
    skipped_count = 0
    invalid_count = 0

    for scope in preview_consolidation_scopes(limit_per_bucket):
        rows = _load_bucket_rows(scope, limit_per_bucket)
        if not rows:
            skipped_count += 1
            _set_cursor(scope.bucket_key, scope.max_observation_id)
            continue

        bucket_count += 1
        exact_merged = _merge_exact_duplicates(rows)
        merged_count += exact_merged
        rows = [row for row in _load_bucket_rows(scope, limit_per_bucket) if row["status"] == "active"]

        if len(rows) < 2:
            _set_cursor(scope.bucket_key, scope.max_observation_id)
            continue

        result = reflection_router.call_observation_consolidation(
            client=client,
            model=model,
            bucket_key=scope.bucket_key,
            observations=_format_observations(rows),
            trace_context={
                "bucket_key": scope.bucket_key,
                "visibility_scope": scope.visibility_scope,
                "scope_value": scope.scope_value,
                "observation_count": len(rows),
            },
        )
        if result is None:
            invalid_count += 1
            continue

        applied = _apply_llm_decisions(rows, result)
        merged_count += applied["merged"]
        superseded_count += applied["superseded"]
        invalid_count += applied["invalid"]
        skipped_count += applied["skipped"]
        _set_cursor(scope.bucket_key, scope.max_observation_id)

    return ConsolidationRunResult(
        bucket_count=bucket_count,
        merged_count=merged_count,
        superseded_count=superseded_count,
        skipped_count=skipped_count,
        invalid_count=invalid_count,
    )


def _all_active_buckets() -> list[ConsolidationScope]:
    rows = db.query_all(
        """
        SELECT
            visibility_scope,
            scope_post_id,
            scope_soul_name,
            COUNT(*) AS active_count,
            MAX(id) AS max_id
        FROM observations
        WHERE status = 'active'
          AND visibility_scope IN ('global', 'post_visible', 'soul_scoped')
        GROUP BY visibility_scope, scope_post_id, scope_soul_name
        ORDER BY visibility_scope, scope_post_id, scope_soul_name
        """
    )
    scopes = []
    for row in rows:
        visibility = row["visibility_scope"]
        scope_value = None
        if visibility == "post_visible":
            scope_value = row["scope_post_id"]
        elif visibility == "soul_scoped":
            scope_value = row["scope_soul_name"]
        bucket_key = _bucket_key(visibility, scope_value)
        cursor = _get_cursor(bucket_key)
        pending = db.query_one(
            """
            SELECT COUNT(*) AS count
            FROM observations
            WHERE status = 'active'
              AND id > ?
              AND visibility_scope = ?
              AND (
                  (? IS NULL AND scope_post_id IS NULL AND scope_soul_name IS NULL)
                  OR scope_post_id = ?
                  OR scope_soul_name = ?
              )
            """,
            (cursor, visibility, scope_value, scope_value, scope_value),
        )
        scopes.append(
            ConsolidationScope(
                bucket_key=bucket_key,
                visibility_scope=visibility,
                scope_value=scope_value,
                active_count=int(row["active_count"]),
                pending_count=int(pending["count"] if pending is not None else 0),
                max_observation_id=int(row["max_id"]),
            )
        )
    return scopes


def _load_bucket_rows(scope: ConsolidationScope, limit: int) -> list[Any]:
    if scope.visibility_scope == "global":
        return db.query_all(
            """
            SELECT *
            FROM observations
            WHERE status = 'active'
              AND visibility_scope = 'global'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
    if scope.visibility_scope == "post_visible":
        return db.query_all(
            """
            SELECT *
            FROM observations
            WHERE status = 'active'
              AND visibility_scope = 'post_visible'
              AND scope_post_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (scope.scope_value, limit),
        )
    if scope.visibility_scope == "soul_scoped":
        return db.query_all(
            """
            SELECT *
            FROM observations
            WHERE status = 'active'
              AND visibility_scope = 'soul_scoped'
              AND scope_soul_name = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (scope.scope_value, limit),
        )
    return []


def _merge_exact_duplicates(rows: list[Any]) -> int:
    groups: dict[tuple[str, str, str], list[Any]] = {}
    for row in rows:
        key = (
            row["type"],
            _normalize_text(row["title"]),
            _normalize_text(row["narrative"]),
        )
        groups.setdefault(key, []).append(row)

    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        target = max(
            group,
            key=lambda row: (
                float(row["confidence"]),
                float(row["importance"]),
                float(row["observed_at"]),
                int(row["id"]),
            ),
        )
        for row in group:
            if int(row["id"]) == int(target["id"]):
                continue
            observation_service.mark_merged(int(row["id"]), int(target["id"]))
            merged += 1
    return merged


def _apply_llm_decisions(rows: list[Any], result: dict) -> dict[str, int]:
    valid_ids = {int(row["id"]) for row in rows}
    changed_ids: set[int] = set()
    counts = {"merged": 0, "superseded": 0, "skipped": 0, "invalid": 0}

    for group in result.get("merge_groups", []):
        target_id = int(group["target_id"])
        merged_ids = [int(item) for item in group.get("merged_ids", [])]
        if target_id not in valid_ids or target_id in changed_ids:
            counts["invalid"] += 1
            continue
        applied_any = False
        for merged_id in merged_ids:
            if merged_id not in valid_ids or merged_id == target_id or merged_id in changed_ids:
                counts["invalid"] += 1
                continue
            observation_service.mark_merged(merged_id, target_id)
            changed_ids.add(merged_id)
            counts["merged"] += 1
            applied_any = True
        if not applied_any:
            counts["skipped"] += 1

    for item in result.get("supersede", []):
        old_id = int(item["old_id"])
        new_id = int(item["new_id"])
        if (
            old_id not in valid_ids
            or new_id not in valid_ids
            or old_id == new_id
            or old_id in changed_ids
            or new_id in changed_ids
        ):
            counts["invalid"] += 1
            continue
        observation_service.mark_superseded(old_id, new_id)
        changed_ids.add(old_id)
        counts["superseded"] += 1
    return counts


def _format_observations(rows: list[Any]) -> str:
    parts = []
    for row in sorted(rows, key=lambda item: int(item["id"])):
        summary = f"\nsummary: {row['summary']}" if row["summary"] else ""
        parts.append(
            "---\n"
            f"id: {row['id']}\n"
            f"type: {row['type']}\n"
            f"title: {row['title']}{summary}\n"
            f"importance: {float(row['importance']):.2f}\n"
            f"confidence: {float(row['confidence']):.2f}\n"
            f"observed_at: {float(row['observed_at']):.3f}\n"
            "---\n\n"
            f"{row['narrative']}"
        )
    return "\n\n".join(parts)


def _bucket_key(visibility_scope: str, scope_value: str | None) -> str:
    if visibility_scope == "global":
        return "global"
    return f"{visibility_scope}:{scope_value}"


def _get_cursor(bucket_key: str) -> int:
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (f"{CURSOR_PREFIX}{bucket_key}",))
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _set_cursor(bucket_key: str, observation_id: int) -> None:
    with db.immediate_transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"{CURSOR_PREFIX}{bucket_key}", str(int(observation_id))),
        )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()
