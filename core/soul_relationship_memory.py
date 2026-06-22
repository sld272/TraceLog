"""Cross-bucket relationship memory for one SOUL.

The public interface is deliberately small: discover affected views, refresh a
SOUL's derived relationship prose, and read it. Thread/private bucket fan-in,
the virtual view key, selector hysteresis, hashing, and stale propagation stay
inside this module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core import db, memory_view_service as mvs

VIEW_TYPE = mvs.VIEW_SOUL_RELATIONSHIP
VIEW_VISIBILITY = "relationship"
CHAR_BUDGET = 900

PUBLIC_USE_RULE = (
    "这份相处记忆包含你和用户在不同场合形成的共同经历。你可以完整理解并使用；"
    "在公开场合提及只在私聊得知的内容前，要像真实朋友一样判断是否合适，"
    "拿不准时不要主动公开。"
)


@dataclass(frozen=True)
class ViewRef:
    owner_scope: str
    visibility_scope: str
    view_type: str


def _owner_scope(soul_name: str) -> str:
    return f"soul:{soul_name}"


def view_ref(soul_name: str) -> ViewRef:
    return ViewRef(_owner_scope(soul_name), VIEW_VISIBILITY, VIEW_TYPE)


def soul_for_bucket(owner_scope: str, visibility_scope: str) -> str | None:
    # Public-post comments now land in global/public; the only soul-scoped
    # interaction left is private 1:1 chat, so relationship memory is sourced
    # exclusively from a soul's private bucket.
    if not owner_scope.startswith("soul:"):
        return None
    soul_name = owner_scope[len("soul:"):]
    if visibility_scope == f"private:soul:{soul_name}":
        return soul_name
    return None


def affected_views(owner_scope: str, visibility_scope: str) -> list[ViewRef]:
    soul_name = soul_for_bucket(owner_scope, visibility_scope)
    return [view_ref(soul_name)] if soul_name else []


def _fetchall(
    conn: sqlite3.Connection | None,
    sql: str,
    params: tuple = (),
) -> list[sqlite3.Row]:
    if conn is not None:
        return conn.execute(sql, params).fetchall()
    return db.query_all(sql, params)


def _fetchone(
    conn: sqlite3.Connection | None,
    sql: str,
    params: tuple = (),
) -> sqlite3.Row | None:
    if conn is not None:
        return conn.execute(sql, params).fetchone()
    return db.query_one(sql, params)


def relationship_units_for_soul(
    soul_name: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row]:
    """Select stable relationship units from this SOUL's private bucket."""
    ref = view_ref(soul_name)
    view = _fetchone(
        conn,
        "SELECT id FROM memory_views "
        "WHERE owner_scope = ? AND visibility_scope = ? AND view_type = ?",
        (ref.owner_scope, ref.visibility_scope, ref.view_type),
    )
    current_members: set[str] = set()
    if view is not None:
        current_members = {
            str(row["unit_id"])
            for row in _fetchall(
                conn,
                "SELECT unit_id FROM memory_view_units WHERE view_id = ?",
                (view["id"],),
            )
        }

    rows = _fetchall(
        conn,
        """
        SELECT *
        FROM memory_units
        WHERE owner_scope = ?
          AND type = 'relationship'
          AND visibility_scope = ?
        """,
        (ref.owner_scope, f"private:soul:{soul_name}"),
    )
    selected = [
        row
        for row in rows
        if mvs.passes_core_predicate(
            row,
            currently_in_slice=str(row["id"]) in current_members,
        )
    ]
    return sorted(
        selected,
        key=lambda row: (
            -float(row["importance"]),
            -float(row["last_confirmed"]),
            str(row["id"]),
        ),
    )


def mark_stale_if_changed_for_bucket(
    owner_scope: str,
    visibility_scope: str,
    *,
    conn: sqlite3.Connection | None = None,
    now: float | None = None,
) -> bool:
    """Mark the owning SOUL's aggregate view stale when its selected set changed."""
    soul_name = soul_for_bucket(owner_scope, visibility_scope)
    if soul_name is None:
        return False
    ref = view_ref(soul_name)
    view = _fetchone(
        conn,
        "SELECT * FROM memory_views "
        "WHERE owner_scope = ? AND visibility_scope = ? AND view_type = ?",
        (ref.owner_scope, ref.visibility_scope, ref.view_type),
    )
    if view is None:
        return False
    units = relationship_units_for_soul(soul_name, conn=conn)
    unit_hash = mvs.source_unit_set_hash(units)
    if (
        unit_hash == view["source_unit_set_hash"]
        and view["renderer_version"] == mvs.RENDERER_VERSION
    ):
        return False
    timestamp = db.now_ts() if now is None else float(now)
    if conn is not None:
        conn.execute(
            "UPDATE memory_views SET status = 'stale', updated_at = ? WHERE id = ?",
            (timestamp, view["id"]),
        )
    else:
        with db.immediate_transaction() as owned:
            owned.execute(
                "UPDATE memory_views SET status = 'stale', updated_at = ? WHERE id = ?",
                (timestamp, view["id"]),
            )
    return True


def souls_needing_view() -> list[str]:
    names = {
        str(row["owner_scope"])[len("soul:"):]
        for row in db.query_all(
            "SELECT owner_scope FROM memory_views "
            "WHERE view_type = ? AND status = 'stale' AND owner_scope LIKE 'soul:%'",
            (VIEW_TYPE,),
        )
    }
    candidates = {
        str(row["owner_scope"])[len("soul:"):]
        for row in db.query_all(
            """
            SELECT DISTINCT owner_scope
            FROM memory_units
            WHERE owner_scope LIKE 'soul:%'
              AND type = 'relationship'
              AND status = 'active'
              AND (
                    visibility_scope LIKE 'thread:%'
                    OR visibility_scope = 'private:' || owner_scope
              )
            """
        )
    }
    for soul_name in candidates:
        ref = view_ref(soul_name)
        existing = db.query_one(
            "SELECT 1 FROM memory_views "
            "WHERE owner_scope = ? AND visibility_scope = ? AND view_type = ?",
            (ref.owner_scope, ref.visibility_scope, ref.view_type),
        )
        if existing is None and relationship_units_for_soul(soul_name):
            names.add(soul_name)
    return sorted(names)


def refresh_relationship_memory(
    soul_name: str,
    *,
    synthesizer=None,
) -> mvs.SynthesizedView:
    ref = view_ref(soul_name)
    units = relationship_units_for_soul(soul_name)
    return mvs.synthesize_units_view(
        ref.owner_scope,
        ref.visibility_scope,
        ref.view_type,
        units,
        synthesizer=synthesizer,
        char_budget=CHAR_BUDGET,
    )


def read_relationship_memory(soul_name: str) -> str:
    units = relationship_units_for_soul(soul_name)
    if not units:
        return ""
    ref = view_ref(soul_name)
    return mvs.read_view_body_with_units(
        ref.owner_scope,
        ref.visibility_scope,
        ref.view_type,
        units,
        char_budget=CHAR_BUDGET,
    )
