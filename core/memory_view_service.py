"""Materialize identity views (user.md / soul private memory) from core units.

A view is a low-frequency *synthesis* of the core subset of memory units in one
(owner, visibility) boundary — a bounded, always-injected identity floor. The
binding is a one-way DAG: evidence -> units -> view. The view never holds
independent truth, so it cannot drift.

This module owns:
  * the core-subset selector (entry predicate + hysteresis + a pragmatic dwell),
  * the source_unit_set_hash that drives stale/re-synthesis,
  * a deterministic template renderer (the failure fallback / default), and
  * synthesize_view, which accepts an injectable LLM synthesizer (Phase 4b)
    and falls back to the template.

Nothing here is wired into the live reply path yet; Phase 6 flips user.md to be
produced from this. Phase 4 only builds and tests the capability.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass

from core import db, memory_unit_service as mus

# selector thresholds (design §3.2). Importance is a three-band structure:
#   < MIN_ADD_IMPORTANCE (0.30, in memory_reconciler) -> trivia, never a unit
#   0.30 .. MIN_IMPORTANCE                            -> unit + retrieval + current-state block, NOT user.md
#   >= MIN_IMPORTANCE (0.70)                          -> eligible for the always-on identity portrait
# Confidence (ENTER/EXIT) is the orthogonal "is it true" axis with hysteresis.
ENTER = 0.82
EXIT = 0.62
MIN_IMPORTANCE = 0.70
DWELL_MIN_OPS = 2  # add + >=1 confirm before a core unit may enter md

RENDERER_VERSION = "baseline-v1"
USER_MD_CHAR_BUDGET = 1200
SOUL_MEMORY_CHAR_BUDGET = 600

VIEW_USER_MD = "user_md"
VIEW_SOUL_PRIVATE = "soul_private_memory"

# md section ordering + labels by unit type
_TYPE_ORDER = ["identity", "goal", "relationship", "preference", "state", "insight", "freeform"]
_TYPE_LABEL = {
    "identity": "身份",
    "goal": "目标",
    "relationship": "关系",
    "preference": "偏好",
    "state": "近期状态",
    "insight": "洞察",
    "freeform": "其他",
}


def _new_view_id() -> str:
    return f"mv_{int(time.time() * 1000):012x}{os.urandom(4).hex()}"


def _confirm_op_count(unit_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM memory_unit_ops WHERE unit_id = ? AND op IN ('add','confirm')",
        (unit_id,),
    )
    return int(row["n"]) if row else 0


def _passes_core_predicate(unit: sqlite3.Row, *, currently_in_slice: bool) -> bool:
    """Design §3.2 predicate with hysteresis + pragmatic dwell."""
    if unit["status"] != "active":
        return False
    if unit["prompt_policy"] != "allow":
        return False
    profile_policy = unit["profile_policy"]
    if profile_policy == "force_exclude":
        return False
    if profile_policy == "force_include":
        return True

    if unit["tier"] != "core":
        return False
    source = unit["source"]
    confidence = float(unit["confidence"])
    threshold = EXIT if currently_in_slice else ENTER
    confidence_ok = source == "user_authored" or confidence >= threshold
    if not confidence_ok:
        return False
    if float(unit["importance"]) < MIN_IMPORTANCE:
        return False
    # dwell: a reflected core unit must have been confirmed at least once before
    # it can join the always-on identity floor (single-pass misjudgement guard).
    if source != "user_authored" and not currently_in_slice:
        if _confirm_op_count(unit["id"]) < DWELL_MIN_OPS:
            return False
    return True


def recompute_slice(owner_scope: str, visibility_scope: str, *, conn: sqlite3.Connection | None = None) -> list[str]:
    """Recompute in_md_slice for all units in a boundary; return the core ids
    (ordered for rendering). Hysteresis uses each unit's current flag."""
    def _run(c: sqlite3.Connection) -> list[str]:
        rows = c.execute(
            """
            SELECT * FROM memory_units
            WHERE owner_scope = ? AND visibility_scope = ?
            """,
            (owner_scope, visibility_scope),
        ).fetchall()
        core_ids: list[str] = []
        for row in rows:
            currently = bool(row["in_md_slice"])
            keep = _passes_core_predicate(row, currently_in_slice=currently)
            if keep != currently:
                c.execute(
                    "UPDATE memory_units SET in_md_slice = ? WHERE id = ?",
                    (1 if keep else 0, row["id"]),
                )
            if keep:
                core_ids.append(row["id"])
        return core_ids

    if conn is not None:
        return _run(conn)
    with db.immediate_transaction() as owned:
        return _run(owned)


def _core_units_for_render(owner_scope: str, visibility_scope: str) -> list[sqlite3.Row]:
    rows = db.query_all(
        """
        SELECT * FROM memory_units
        WHERE owner_scope = ? AND visibility_scope = ? AND in_md_slice = 1 AND status = 'active'
        """,
        (owner_scope, visibility_scope),
    )
    return _order_units(rows)


def _order_units(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    def sort_key(row: sqlite3.Row):
        try:
            type_rank = _TYPE_ORDER.index(row["type"])
        except ValueError:
            type_rank = len(_TYPE_ORDER)
        # higher importance first within a type
        return (type_rank, -float(row["importance"]), -float(row["confidence"]))

    return sorted(rows, key=sort_key)


def source_unit_set_hash(units: list[sqlite3.Row]) -> str:
    """Hash of the core set + per-member material fields (design §3.3). When this
    matches the stored view hash, no re-synthesis is needed."""
    h = hashlib.sha256()
    h.update(f"selector:{ENTER}:{EXIT}:{MIN_IMPORTANCE}|renderer:{RENDERER_VERSION}".encode())
    for row in units:
        material = "|".join(
            str(x) for x in (
                row["id"], row["content"], row["status"], row["type"], row["tier"],
                row["source"], row["importance"], row["sensitivity"],
                row["profile_policy"], row["prompt_policy"],
            )
        )
        h.update(b"\x00")
        h.update(material.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def render_template(units: list[sqlite3.Row], *, char_budget: int) -> str:
    """Deterministic, hallucination-free fallback: group by type, one line each."""
    if not units:
        return "（暂无足够稳定的画像信息）"
    grouped: dict[str, list[str]] = {}
    for row in units:
        grouped.setdefault(row["type"], []).append(str(row["content"]).strip())
    parts: list[str] = []
    for unit_type in _TYPE_ORDER:
        if unit_type not in grouped:
            continue
        label = _TYPE_LABEL.get(unit_type, unit_type)
        lines = "\n".join(f"- {item}" for item in grouped[unit_type])
        parts.append(f"## {label}\n{lines}")
    # any unknown types last
    for unit_type, items in grouped.items():
        if unit_type in _TYPE_ORDER:
            continue
        lines = "\n".join(f"- {item}" for item in items)
        parts.append(f"## {_TYPE_LABEL.get(unit_type, unit_type)}\n{lines}")
    md = "\n\n".join(parts)
    return md[:char_budget]


def _generated_header(view_type: str, unit_hash: str, content_md: str) -> str:
    content_hash = "sha256:" + hashlib.sha256(content_md.encode("utf-8")).hexdigest()
    return (
        f"<!-- generated_by=tracelog view_type={view_type} editable=false\n"
        f"     source_unit_set_hash={unit_hash} renderer_version={RENDERER_VERSION}\n"
        f"     generated_at={db.now_ts()} content_hash={content_hash} -->\n\n"
    )


@dataclass(frozen=True)
class SynthesizedView:
    view_id: str
    owner_scope: str
    visibility_scope: str
    view_type: str
    content_md: str
    source_unit_set_hash: str
    unit_ids: list[str]
    used_fallback: bool


def synthesize_view(
    owner_scope: str,
    visibility_scope: str,
    view_type: str,
    *,
    synthesizer=None,
    char_budget: int | None = None,
    recompute: bool = True,
) -> SynthesizedView:
    """Pick the core subset and materialize a view row.

    ``synthesizer(units, char_budget)`` is the optional LLM path; on None/error
    it falls back to the deterministic template. The DAG is one-way (units ->
    view), so the view is just a cached synthesis with no independent truth."""
    if char_budget is None:
        char_budget = USER_MD_CHAR_BUDGET if view_type == VIEW_USER_MD else SOUL_MEMORY_CHAR_BUDGET

    if recompute:
        recompute_slice(owner_scope, visibility_scope)
    units = _core_units_for_render(owner_scope, visibility_scope)
    unit_hash = source_unit_set_hash(units)

    used_fallback = True
    body = ""
    if synthesizer is not None:
        try:
            candidate = synthesizer(units, char_budget)
            if isinstance(candidate, str) and candidate.strip():
                body = candidate.strip()[:char_budget]
                used_fallback = False
        except Exception:
            body = ""
    if not body:
        body = render_template(units, char_budget=char_budget)

    content_md = _generated_header(view_type, unit_hash, body) + body
    now = db.now_ts()
    view_id = _new_view_id()
    with db.immediate_transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM memory_views WHERE owner_scope = ? AND visibility_scope = ? AND view_type = ?",
            (owner_scope, visibility_scope, view_type),
        ).fetchone()
        if existing is not None:
            view_id = existing["id"]
            conn.execute(
                """
                UPDATE memory_views
                SET content_md = ?, source_unit_set_hash = ?, renderer_version = ?,
                    status = 'fresh', generated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (content_md, unit_hash, RENDERER_VERSION, now, now, view_id),
            )
            conn.execute("DELETE FROM memory_view_units WHERE view_id = ?", (view_id,))
        else:
            conn.execute(
                """
                INSERT INTO memory_views(
                    id, owner_scope, visibility_scope, view_type, content_md,
                    source_unit_set_hash, renderer_version, status, generated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'fresh', ?, ?)
                """,
                (view_id, owner_scope, visibility_scope, view_type, content_md,
                 unit_hash, RENDERER_VERSION, now, now),
            )
        for index, row in enumerate(units):
            conn.execute(
                "INSERT INTO memory_view_units(view_id, unit_id, order_index) VALUES (?, ?, ?)",
                (view_id, row["id"], index),
            )

    return SynthesizedView(
        view_id=view_id,
        owner_scope=owner_scope,
        visibility_scope=visibility_scope,
        view_type=view_type,
        content_md=content_md,
        source_unit_set_hash=unit_hash,
        unit_ids=[row["id"] for row in units],
        used_fallback=used_fallback,
    )


def get_view(owner_scope: str, visibility_scope: str, view_type: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM memory_views WHERE owner_scope = ? AND visibility_scope = ? AND view_type = ?",
        (owner_scope, visibility_scope, view_type),
    )


def mark_stale_if_changed(owner_scope: str, visibility_scope: str, view_type: str) -> bool:
    """Recompute the slice + hash; if it differs from the stored view, mark it
    stale and return True. Pure confirms / non-core churn leave the hash
    unchanged so re-synthesis stays low-frequency (design §3.3)."""
    view = get_view(owner_scope, visibility_scope, view_type)
    if view is None:
        return False
    recompute_slice(owner_scope, visibility_scope)
    units = _core_units_for_render(owner_scope, visibility_scope)
    new_hash = source_unit_set_hash(units)
    if new_hash == view["source_unit_set_hash"] and view["renderer_version"] == RENDERER_VERSION:
        return False
    with db.immediate_transaction() as conn:
        conn.execute(
            "UPDATE memory_views SET status = 'stale', updated_at = ? WHERE id = ?",
            (db.now_ts(), view["id"]),
        )
    return True
