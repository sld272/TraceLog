"""SQLite state database helpers for TraceLog."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = BASE_DIR / "workspace"
DB_PATH = WORKSPACE_DIR / "state.db"
INIT_SQL_PATH = BASE_DIR / "schema.sql"


def connect() -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    """Create and validate the state database schema."""
    if not INIT_SQL_PATH.exists():
        raise FileNotFoundError(f"Missing schema file: {INIT_SQL_PATH}")

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    sql = INIT_SQL_PATH.read_text(encoding="utf-8")
    conn = connect()
    try:
        conn.executescript(sql)
        _migrate_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"SQLite WAL mode unavailable: {mode}")
        _validate_fts5_trigram(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to initialize state.db: {exc}") from exc
    except BaseException:
        _rollback_safely(conn)
        raise
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "comments", "edited_at", "REAL")
    _ensure_column(conn, "comments", "rerun_at", "REAL")
    _ensure_column(conn, "chat_messages", "edited_at", "REAL")
    _ensure_column(conn, "chat_messages", "rerun_at", "REAL")
    _ensure_column(conn, "chat_messages", "metadata", "TEXT")
    _ensure_post_soul_orders(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_feedback (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel    TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            doc_id     TEXT NOT NULL,
            verdict    TEXT NOT NULL DEFAULT 'irrelevant',
            created_at REAL NOT NULL,
            UNIQUE(channel, message_id, doc_id)
        )
        """
    )
    _ensure_column(conn, "memory_ingest_events", "author", "TEXT")
    _backfill_memory_event_authors(conn)
    _migrate_comment_event_ownership(conn)
    _backfill_memory_events(conn)
    _migrate_goal_units_to_goaltool(conn)
    _repair_historical_challenged_units(conn)


def _migrate_goal_units_to_goaltool(conn: sqlite3.Connection) -> None:
    """Move active legacy goal units into the dedicated goaltool once."""
    marker_key = "memory_v2_goaltool_migration_v1"
    marker = conn.execute("SELECT value FROM meta WHERE key = ?", (marker_key,)).fetchone()
    if marker is not None:
        return

    from core import goal_service, memory_unit_service

    rows = conn.execute(
        """
        SELECT id, content, owner_scope, visibility_scope
        FROM memory_units
        WHERE type = 'goal' AND status = 'active'
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    migrated_ids: list[str] = []
    for row in rows:
        content = str(row["content"] or "").strip()
        if not content:
            continue
        horizon = _goal_horizon_from_content(content)
        goal_service.create_goal(
            content,
            None,
            horizon,
            source="suggested_accepted",
            focus=horizon == "short",
            conn=conn,
        )
        memory_unit_service.retract_unit(
            str(row["id"]),
            by="migration",
            reason="outdated",
            actor="goaltool_migration",
            conn=conn,
        )
        migrated_ids.append(str(row["id"]))

    if migrated_ids:
        placeholders = ",".join("?" for _ in migrated_ids)
        conn.execute(
            f"UPDATE memory_units SET in_md_slice = 0 WHERE id IN ({placeholders})",
            tuple(migrated_ids),
        )
        conn.execute(
            f"""
            UPDATE memory_views
            SET status = 'stale', updated_at = ?
            WHERE id IN (
                SELECT DISTINCT view_id
                FROM memory_view_units
                WHERE unit_id IN ({placeholders})
            )
            """,
            (now_ts(), *migrated_ids),
        )

    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        (marker_key, str(len(migrated_ids))),
    )


def _goal_horizon_from_content(content: str) -> str:
    """Conservative migration heuristic: only explicit near-term wording is short."""
    short_markers = (
        "今天",
        "明天",
        "本周",
        "这周",
        "下周",
        "本月",
        "这个月",
        "月底",
        "本学期",
        "这学期",
        "近期",
        "短期",
        "天内",
        "周内",
        "月内",
    )
    return "short" if any(marker in content for marker in short_markers) else "long"


def _repair_historical_challenged_units(conn: sqlite3.Connection) -> None:
    marker = conn.execute(
        "SELECT value FROM meta WHERE key = 'memory_v2_rechallenge_v1'"
    ).fetchone()
    if marker is not None:
        return
    from core import memory_unit_service

    rows = conn.execute(
        """
        SELECT DISTINCT latest.id AS trigger_event_id
        FROM memory_units u
        JOIN memory_unit_evidence ue ON ue.unit_id = u.id
        JOIN memory_ingest_events linked ON linked.id = ue.event_id
        JOIN memory_ingest_events latest
          ON latest.source_type = linked.source_type
         AND latest.source_id = linked.source_id
        WHERE u.status = 'active'
          AND u.source IN ('reflected','migrated')
          AND latest.id = (
              SELECT newest.id
              FROM memory_ingest_events newest
              WHERE newest.source_type = linked.source_type
                AND newest.source_id = linked.source_id
              ORDER BY newest.source_revision DESC, newest.id DESC
              LIMIT 1
          )
          AND latest.id != linked.id
          AND latest.op IN ('edit','delete')
        ORDER BY latest.id
        """
    ).fetchall()
    for row in rows:
        memory_unit_service.challenge_units_for_source(
            conn,
            int(row["trigger_event_id"]),
            actor="migration",
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('memory_v2_rechallenge_v1', ?)",
        (str(len(rows)),),
    )


def _backfill_memory_event_authors(conn: sqlite3.Connection) -> None:
    # Posts are always user-authored.
    conn.execute(
        "UPDATE memory_ingest_events SET author = 'user' "
        "WHERE author IS NULL AND source_type = 'post'"
    )
    # For comments, owner_scope encodes authorship exactly (global=user, soul=assistant).
    conn.execute(
        "UPDATE memory_ingest_events SET author = "
        "CASE WHEN owner_scope = 'global' THEN 'user' ELSE 'assistant' END "
        "WHERE author IS NULL AND source_type = 'comment_message'"
    )
    # Chat owner is the soul for both roles, so derive role from the message row.
    conn.execute(
        "UPDATE memory_ingest_events SET author = ("
        "  SELECT role FROM chat_messages WHERE CAST(chat_messages.id AS TEXT) = memory_ingest_events.source_id"
        ") WHERE author IS NULL AND source_type = 'chat_message' "
        "AND EXISTS (SELECT 1 FROM chat_messages WHERE CAST(chat_messages.id AS TEXT) = memory_ingest_events.source_id)"
    )


def _migrate_comment_event_ownership(conn: sqlite3.Connection) -> None:
    # Comment events used to put user comments under owner_scope='global'; they
    # now belong to the soul whose thread they're in (so comment interactions
    # build that soul's relationship memory). Re-own legacy user-comment events
    # to soul:<comments.soul_name>. Runs AFTER author backfill, which relied on
    # the old owner mapping. Idempotent (only touches owner_scope='global').
    conn.execute(
        "UPDATE memory_ingest_events SET owner_scope = 'soul:' || ("
        "  SELECT soul_name FROM comments WHERE CAST(comments.id AS TEXT) = memory_ingest_events.source_id"
        ") WHERE source_type = 'comment_message' AND owner_scope = 'global' "
        "AND EXISTS (SELECT 1 FROM comments WHERE CAST(comments.id AS TEXT) = memory_ingest_events.source_id)"
    )
    # If reconcile ran before this migration, units/cursors for the old
    # (global, thread:*) buckets are now orphaned — their evidence just moved to
    # soul-owned buckets. A global+thread bucket could also mix several souls'
    # comments, so re-mapping isn't clean; drop those units (+ evidence links)
    # and cursors and let the new soul-owned buckets reconcile the migrated
    # evidence from scratch. Idempotent: in steady state there are none.
    orphan_unit_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM memory_units WHERE owner_scope = 'global' AND visibility_scope LIKE 'thread:%'"
        ).fetchall()
    ]
    if orphan_unit_ids:
        placeholders = ",".join("?" for _ in orphan_unit_ids)
        conn.execute(f"DELETE FROM memory_unit_evidence WHERE unit_id IN ({placeholders})", orphan_unit_ids)
        conn.execute(f"DELETE FROM memory_units WHERE id IN ({placeholders})", orphan_unit_ids)
    conn.execute(
        "DELETE FROM memory_reconcile_cursors WHERE owner_scope = 'global' AND visibility_scope LIKE 'thread:%'"
    )


def _backfill_memory_events(conn: sqlite3.Connection) -> None:
    # Lazy import to avoid a circular import (memory_events_service imports db).
    from core import memory_events_service

    memory_events_service.backfill_from_existing(conn)


def _ensure_post_soul_orders(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS post_soul_orders (
            post_id    TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            soul_name  TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            PRIMARY KEY (post_id, soul_name)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_post_soul_orders_post_order
            ON post_soul_orders(post_id, sort_order, soul_name)
        """
    )
    _backfill_post_soul_orders(conn)


def _backfill_post_soul_orders(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT comments.post_id, comments.soul_name, comments.created_at, comments.id
        FROM comments
        LEFT JOIN post_soul_orders
          ON post_soul_orders.post_id = comments.post_id
         AND post_soul_orders.soul_name = comments.soul_name
        WHERE comments.seq = 0
          AND post_soul_orders.post_id IS NULL
        ORDER BY comments.post_id ASC, comments.created_at ASC, comments.id ASC
        """
    ).fetchall()
    if not rows:
        return

    max_orders = {
        row["post_id"]: int(row["max_sort_order"])
        for row in conn.execute(
            """
            SELECT post_id, MAX(sort_order) AS max_sort_order
            FROM post_soul_orders
            GROUP BY post_id
            """
        ).fetchall()
        if row["max_sort_order"] is not None
    }
    offsets: dict[str, int] = {}
    inserts = []
    for row in rows:
        post_id = str(row["post_id"])
        next_offset = offsets.get(post_id, 0)
        offsets[post_id] = next_offset + 1
        sort_order = max_orders.get(post_id, -1) + 1 + next_offset
        inserts.append((post_id, row["soul_name"], sort_order, row["created_at"]))

    conn.executemany(
        """
        INSERT OR IGNORE INTO post_soul_orders(post_id, soul_name, sort_order, created_at)
        VALUES (?, ?, ?, ?)
        """,
        inserts,
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(row["name"] == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _validate_fts5_trigram(conn: sqlite3.Connection) -> None:
    probe_id = f"__fts5_probe__:{os.getpid()}:{time.time_ns()}"
    try:
        conn.execute("INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (probe_id, "1970-01-01T00:00:00+00:00", "中文 probe", 0.0, 0.0))
        conn.execute("DELETE FROM posts WHERE id = ?", (probe_id,))
    except sqlite3.Error as exc:
        raise RuntimeError("SQLite FTS5 trigram support is required but unavailable") from exc


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run statements in a write transaction."""
    with _transaction("BEGIN") as conn:
        yield conn


@contextmanager
def immediate_transaction() -> Iterator[sqlite3.Connection]:
    """Run statements in a write transaction that acquires the write lock up front."""
    with _transaction("BEGIN IMMEDIATE") as conn:
        yield conn


@contextmanager
def _transaction(begin_sql: str) -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute(begin_sql)
        yield conn
        conn.commit()
    except BaseException:
        _rollback_safely(conn)
        raise
    finally:
        conn.close()


def _rollback_safely(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def execute(sql: str, params: Sequence[Any] = ()) -> None:
    with transaction() as conn:
        conn.execute(sql, params)


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def query_all(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def now_ts() -> float:
    return time.time()


def require_lastrowid(cursor: sqlite3.Cursor, context: str) -> int:
    """Return cursor.lastrowid or raise if SQLite did not provide one."""
    if cursor.lastrowid is None:
        raise RuntimeError(f"SQLite did not return lastrowid for {context}")
    return cursor.lastrowid
