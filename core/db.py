"""SQLite state database helpers for TraceLog."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from core import paths

BASE_DIR = paths.RESOURCE_DIR
WORKSPACE_DIR = paths.WORKSPACE_DIR
DB_PATH = WORKSPACE_DIR / "state.db"
INIT_SQL_PATH = paths.SCHEMA_FILE


def connect() -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
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
        _drop_retired_tables(conn)
        _migrate_columns(conn)
        _migrate_suggestions_kind_constraint(conn)
        conn.executescript(sql)
        _backfill_schedule_event_accounts(conn)
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


# Columns added to existing tables after their initial release. schema.sql only
# runs CREATE TABLE IF NOT EXISTS, so a pre-existing DB never picks up new
# columns from it — each entry here is ALTER TABLEd in when missing. Keep the
# ADD COLUMN definition identical to the schema.sql one (minus non-constant
# defaults, which SQLite ADD COLUMN cannot take).
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("memory_units", "contested_at", "REAL"),
    ("chat_messages", "client_request_id", "TEXT"),
    ("goals", "schedule_expectation", "TEXT"),
    ("schedule_events", "account_id", "TEXT"),
    ("vector_index_items", "dim", "INTEGER"),
    ("vector_index_items", "embedding", "BLOB"),
)


def _drop_retired_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS todos")


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _COLUMN_MIGRATIONS:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if exists is None:
            continue  # fresh DB: schema.sql creates the full table
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.commit()


def _migrate_suggestions_kind_constraint(conn: sqlite3.Connection) -> None:
    """Allow schedule suggestions in databases created by the goal-only schema."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'suggestions'"
    ).fetchone()
    if row is None or "schedule" in str(row[0] or "").casefold():
        return
    conn.execute(
        """
        CREATE TABLE suggestions_kind_v2 (
            id             TEXT PRIMARY KEY,
            kind           TEXT NOT NULL CHECK(kind IN ('goal', 'schedule')),
            payload_json   TEXT NOT NULL,
            evidence_ref   TEXT,
            confidence     REAL NOT NULL DEFAULT 0.6
                               CHECK(confidence >= 0.0 AND confidence <= 1.0),
            status         TEXT NOT NULL DEFAULT 'pending'
                               CHECK(status IN ('pending', 'accepted', 'dismissed')),
            normalized_key TEXT,
            created_at     REAL NOT NULL,
            decided_at     REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO suggestions_kind_v2(
            id, kind, payload_json, evidence_ref, confidence, status,
            normalized_key, created_at, decided_at
        )
        SELECT id, kind, payload_json, evidence_ref, confidence, status,
               normalized_key, created_at, decided_at
        FROM suggestions
        """
    )
    conn.execute("DROP TABLE suggestions")
    conn.execute("ALTER TABLE suggestions_kind_v2 RENAME TO suggestions")
    conn.commit()


def _backfill_schedule_event_accounts(conn: sqlite3.Connection) -> None:
    legacy_count = conn.execute(
        "SELECT COUNT(*) FROM schedule_events WHERE account_id IS NULL"
    ).fetchone()[0]
    if legacy_count <= 0:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO calendar_accounts(id, provider, display_name, created_at)
        VALUES ('outlook', 'outlook', 'Outlook', ?)
        """,
        (now_ts(),),
    )
    conn.execute(
        "UPDATE schedule_events SET account_id = 'outlook' WHERE account_id IS NULL"
    )


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
