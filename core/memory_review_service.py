"""User-facing review and edit service for long-term memory files."""

from __future__ import annotations

import json
from typing import Any

from core import db, profile_service, soul_memory_service


USER_MEMORY_OVERWRITE_PATCH = {"op": "overwrite_user_memory"}
SOUL_MEMORY_OVERWRITE_PATCH = {"op": "overwrite_soul_memory"}


def read_user_memory() -> str:
    """Read the current user long-term memory file."""
    return profile_service.read_profile()


def save_user_memory(content: str) -> None:
    """Save a user-edited user.md snapshot and record a user revision."""
    _validate_user_memory(content)
    profile_service.write_profile(content, source="user", patch=USER_MEMORY_OVERWRITE_PATCH)


def read_soul_memory(soul_name: str) -> str:
    """Read one SOUL's long-term relationship memory."""
    return soul_memory_service.read_soul_memory(soul_name)


def save_soul_memory(soul_name: str, content: str) -> None:
    """Save a user-edited SOUL memory snapshot and record a user revision."""
    _validate_soul_memory(soul_name, content)
    soul_memory_service.write_soul_memory(
        soul_name,
        content,
        source="user",
        patch=SOUL_MEMORY_OVERWRITE_PATCH,
    )


def list_user_revisions(limit: int = 20, source: str | None = None) -> list[dict[str, Any]]:
    """List user.md revisions without loading snapshot bodies."""
    sql = """
        SELECT id, source, patch, created_at
        FROM user_md_revisions
    """
    params: list[Any] = []
    if source is not None:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(_normalize_limit(limit))
    rows = db.query_all(sql, tuple(params))
    return [_revision_summary(row, target_type="user", target_name=None) for row in rows]


def get_user_revision(revision_id: int) -> dict[str, Any] | None:
    """Return one user.md revision with its snapshot."""
    row = db.query_one(
        """
        SELECT id, snapshot, source, patch, created_at
        FROM user_md_revisions
        WHERE id = ?
        """,
        (revision_id,),
    )
    if row is None:
        return None
    return _revision_detail(row, target_type="user", target_name=None)


def list_soul_revisions(
    soul_name: str | None = None,
    limit: int = 20,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """List SOUL memory revisions without loading snapshot bodies."""
    sql = """
        SELECT id, soul_name, source, patch, created_at
        FROM soul_memory_revisions
    """
    clauses = []
    params: list[Any] = []
    if soul_name is not None:
        clauses.append("soul_name = ?")
        params.append(soul_name)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(_normalize_limit(limit))
    rows = db.query_all(sql, tuple(params))
    return [_revision_summary(row, target_type="soul", target_name=row["soul_name"]) for row in rows]


def get_soul_revision(revision_id: int) -> dict[str, Any] | None:
    """Return one SOUL memory revision with its snapshot."""
    row = db.query_one(
        """
        SELECT id, soul_name, snapshot, source, patch, created_at
        FROM soul_memory_revisions
        WHERE id = ?
        """,
        (revision_id,),
    )
    if row is None:
        return None
    return _revision_detail(row, target_type="soul", target_name=row["soul_name"])


def _validate_user_memory(content: str) -> None:
    _validate_text(content, "用户记忆内容")
    if "# 用户档案" not in content:
        raise ValueError("user.md 必须包含 '# 用户档案'")


def _validate_soul_memory(soul_name: str, content: str) -> None:
    _validate_text(content, "SOUL 记忆内容")
    if f"# {soul_name}的相处记忆" not in content:
        raise ValueError(f"SOUL 记忆必须包含 '# {soul_name}的相处记忆'")


def _validate_text(content: str, label: str) -> None:
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{label}不能为空")
    for char in content:
        codepoint = ord(char)
        if codepoint < 32 and char not in {"\n", "\r", "\t"}:
            raise ValueError(f"{label}不能包含控制字符")


def _normalize_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 20
    return max(1, min(value, 100))


def _revision_summary(row, *, target_type: str, target_name: str | None) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "target_type": target_type,
        "target_name": target_name,
        "source": row["source"],
        "patch": _decode_patch(row["patch"]),
        "created_at": float(row["created_at"]),
    }


def _revision_detail(row, *, target_type: str, target_name: str | None) -> dict[str, Any]:
    detail = _revision_summary(row, target_type=target_type, target_name=target_name)
    detail["snapshot"] = row["snapshot"]
    return detail


def _decode_patch(raw_patch: str) -> Any:
    try:
        return json.loads(raw_patch)
    except (TypeError, json.JSONDecodeError):
        return raw_patch
