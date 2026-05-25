"""SQLite state database helpers for TraceLog."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
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
        conn.execute("PRAGMA foreign_keys = ON")
        _run_lightweight_migrations(conn)
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"SQLite WAL mode unavailable: {mode}")
        _validate_fts5_trigram(conn)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
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


def _validate_fts5_trigram(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     ("__fts5_probe__", "1970-01-01T00:00:00+00:00", "中文 probe", 0.0, 0.0))
        conn.execute("DELETE FROM posts WHERE id = ?", ("__fts5_probe__",))
    except sqlite3.Error as exc:
        raise RuntimeError("SQLite FTS5 trigram support is required but unavailable") from exc


def _run_lightweight_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for existing local workspaces."""
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(todos)").fetchall()
    }
    if "source_comment_message" not in columns:
        conn.execute(
            """
            ALTER TABLE todos
            ADD COLUMN source_comment_message INTEGER REFERENCES comment_messages(id) ON DELETE SET NULL
            """
        )


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run statements in a write transaction."""
    conn = connect()
    try:
        conn.execute("BEGIN")
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


def execute_many(sql: str, params: Iterable[Sequence[Any]]) -> None:
    with transaction() as conn:
        conn.executemany(sql, params)


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
