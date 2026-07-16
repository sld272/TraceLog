"""Pending, accepted, and dismissed goal suggestions."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from typing import Any

from core import db, goal_service

SUGGESTION_KINDS = {"goal"}
SUGGESTION_STATUSES = {"pending", "accepted", "dismissed"}


def create_suggestion(
    kind: str,
    payload: dict[str, Any],
    evidence_ref: str | None,
    confidence: float = 0.6,
) -> dict[str, Any] | None:
    """Create a pending suggestion unless an equivalent row already exists.

    A dismissed equivalent is a permanent tombstone. Pending/accepted matches
    are also reused so retries and reply reruns cannot create duplicates.
    """
    normalized_payload = _normalize_payload(kind, payload)
    if kind == "goal" and goal_service.has_active_goal_title(normalized_payload["title"]):
        return None
    normalized_ref = _normalize_optional_text(evidence_ref)
    normalized_key = normalized_key_for(kind, normalized_payload, normalized_ref)
    normalized_confidence = _coerce_confidence(confidence)
    with db.immediate_transaction() as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM suggestions
            WHERE normalized_key = ?
            ORDER BY
                CASE status WHEN 'dismissed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                created_at DESC
            LIMIT 1
            """,
            (normalized_key,),
        ).fetchone()
        if existing is not None:
            return None if existing["status"] == "dismissed" else _row_to_dict(existing)

        suggestion_id = _new_suggestion_id()
        now = db.now_ts()
        conn.execute(
            """
            INSERT INTO suggestions(
                id, kind, payload_json, evidence_ref, confidence, status,
                normalized_key, created_at, decided_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL)
            """,
            (
                suggestion_id,
                kind,
                json.dumps(normalized_payload, ensure_ascii=False, sort_keys=True),
                normalized_ref,
                normalized_confidence,
                normalized_key,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _row_to_dict(row)


def get_suggestion(suggestion_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    row = (
        conn.execute(
            "SELECT * FROM suggestions WHERE id = ? AND kind = 'goal'",
            (suggestion_id,),
        ).fetchone()
        if conn is not None
        else db.query_one(
            "SELECT * FROM suggestions WHERE id = ? AND kind = 'goal'",
            (suggestion_id,),
        )
    )
    return _row_to_dict(row) if row is not None else None


def list_pending(kind: str | None = None) -> list[dict[str, Any]]:
    if kind is not None:
        _validate_kind(kind)
    rows = db.query_all(
        """
        SELECT *
        FROM suggestions
        WHERE status = 'pending' AND kind = 'goal'
        ORDER BY confidence DESC, created_at ASC, id ASC
        """
    )
    return [_row_to_dict(row) for row in rows]


def accept(suggestion_id: str) -> dict[str, Any]:
    """Accept one pending suggestion and atomically create its target object."""
    with db.immediate_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE id = ? AND kind = 'goal'",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            raise ValueError("suggestion 不存在")
        if row["status"] != "pending":
            raise ValueError("suggestion 已处理")
        suggestion = _row_to_dict(row)
        payload = suggestion["payload"]
        created = goal_service.create_goal(
            payload["title"],
            payload.get("detail"),
            payload["horizon"],
            source="suggested_accepted",
            focus=bool(payload.get("focus", False)),
            conn=conn,
        )
        decided_at = db.now_ts()
        conn.execute(
            "UPDATE suggestions SET status = 'accepted', decided_at = ? WHERE id = ?",
            (decided_at, suggestion_id),
        )
        updated = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return {"suggestion": _row_to_dict(updated), "created": created}


def dismiss(suggestion_id: str) -> dict[str, Any]:
    """Dismiss a suggestion; its normalized key remains as a permanent tombstone."""
    with db.immediate_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE id = ? AND kind = 'goal'",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            raise ValueError("suggestion 不存在")
        if row["status"] == "accepted":
            raise ValueError("已采纳的 suggestion 不能忽略")
        if row["status"] == "pending":
            conn.execute(
                "UPDATE suggestions SET status = 'dismissed', decided_at = ? WHERE id = ?",
                (db.now_ts(), suggestion_id),
            )
        updated = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _row_to_dict(updated)


def delete_pending_for_evidence(evidence_ref: str) -> int:
    """Delete pending suggestions tied to one source (e.g. a deleted post).

    Pending rows are removed outright rather than tombstoned, so re-creating
    the same source (e.g. reposting) can surface the suggestions again.
    Accepted/dismissed rows are left untouched.
    """
    normalized_ref = _normalize_optional_text(evidence_ref)
    if not normalized_ref:
        return 0
    with db.immediate_transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM suggestions WHERE evidence_ref = ? AND status = 'pending'",
            (normalized_ref,),
        )
        return cursor.rowcount


def normalized_key_for(kind: str, payload: dict[str, Any], evidence_ref: str | None) -> str:
    _validate_kind(kind)
    source_kind = _evidence_source_kind(evidence_ref)
    material = {
        "kind": kind,
        "title": _normalize_key_text(payload.get("title")),
        "horizon": payload.get("horizon"),
        "source": source_kind,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    _validate_kind(kind)
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    title = payload.get("title")
    horizon = payload.get("horizon")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("goal suggestion title 不能为空")
    if horizon not in goal_service.GOAL_HORIZONS:
        raise ValueError("goal suggestion horizon 只支持：short、long")
    detail = payload.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise ValueError("goal suggestion detail 必须是字符串或 null")
    return {
        "title": title.strip(),
        "detail": detail.strip() if isinstance(detail, str) and detail.strip() else None,
        "horizon": horizon,
        "focus": bool(payload.get("focus", horizon == "short")),
    }


def _validate_kind(kind: Any) -> None:
    if kind not in SUGGESTION_KINDS:
        raise ValueError("kind 只支持：goal")


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.6
    return max(0.0, min(1.0, confidence))


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("evidence_ref 必须是字符串或 null")
    return value.strip() or None


def _normalize_key_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _evidence_source_kind(evidence_ref: str | None) -> str:
    if not evidence_ref:
        return "unknown"
    return evidence_ref.split(":", 1)[0].strip().casefold() or "unknown"


def _new_suggestion_id() -> str:
    stamp = int(time.time() * 1000).to_bytes(6, "big").hex()
    return f"s_{stamp}{secrets.token_hex(5)}"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "payload": payload if isinstance(payload, dict) else {},
        "evidence_ref": row["evidence_ref"],
        "confidence": float(row["confidence"]),
        "status": row["status"],
        "normalized_key": row["normalized_key"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }
