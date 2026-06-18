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
    _backfill_memory_events(conn)


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
