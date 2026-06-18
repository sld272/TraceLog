"""Append-only evidence event ledger for memory v2.

The ledger is the source of *evidence truth*: every create/edit/rerun/delete on
a business row (post / comment / chat message) appends one immutable event that
freezes the content snapshot, hash and visibility boundary at that moment.

Memory units (added in a later phase) bind to these event versions rather than
to mutable source rows, so editing a post or rerunning a reply never silently
rewrites the historical basis of a belief. The auto-increment ``id`` doubles as
the monotonic consumption cursor for per-bucket reconcile.

This module deliberately has *no consumer* yet — Phase 1 only establishes the
ledger and backfills history. Live write-path wiring and unit reconcile arrive
in later phases.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from core import db

# --- boundary vocabulary ---------------------------------------------------

GLOBAL_SCOPE = "global"
PUBLIC_VISIBILITY = "public"

SOURCE_CHANNELS = frozenset({"post", "comment", "chat"})
SOURCE_TYPES = frozenset({"post", "comment_message", "chat_message"})
EVENT_OPS = frozenset({"create", "edit", "rerun", "delete"})


def soul_scope(soul_name: str) -> str:
    return f"soul:{soul_name}"


def thread_visibility(post_id: str) -> str:
    return f"thread:{post_id}"


def private_visibility(soul_name: str) -> str:
    return f"private:soul:{soul_name}"


@dataclass(frozen=True)
class IngestEvent:
    id: int
    source_type: str
    source_id: str
    source_revision: int
    owner_scope: str
    visibility_scope: str
    op: str


def _content_hash(snapshot: str | None) -> str | None:
    if snapshot is None:
        return None
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def _next_revision(conn: sqlite3.Connection, source_type: str, source_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(source_revision), 0) AS max_rev
        FROM memory_ingest_events
        WHERE source_type = ? AND source_id = ?
        """,
        (source_type, source_id),
    ).fetchone()
    return int(row["max_rev"]) + 1


def append_event(
    conn: sqlite3.Connection,
    *,
    owner_scope: str,
    visibility_scope: str,
    source_channel: str,
    source_type: str,
    source_id: str,
    op: str,
    content_snapshot: str | None,
    occurred_at: float,
    created_at: float | None = None,
) -> IngestEvent:
    """Append one evidence event using the caller's open transaction.

    The caller MUST pass the same ``conn`` it used to mutate the business row, so
    that the business write and its evidence event commit atomically.
    """
    if source_channel not in SOURCE_CHANNELS:
        raise ValueError(f"非法 source_channel：{source_channel}")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"非法 source_type：{source_type}")
    if op not in EVENT_OPS:
        raise ValueError(f"非法 op：{op}")
    if not owner_scope or not visibility_scope:
        raise ValueError("owner_scope / visibility_scope 不能为空")

    source_id = str(source_id)
    revision = _next_revision(conn, source_type, source_id)
    created = db.now_ts() if created_at is None else float(created_at)
    cursor = conn.execute(
        """
        INSERT INTO memory_ingest_events(
            owner_scope, visibility_scope, source_channel, source_type,
            source_id, source_revision, op, content_snapshot, content_hash,
            occurred_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            owner_scope,
            visibility_scope,
            source_channel,
            source_type,
            source_id,
            revision,
            op,
            content_snapshot,
            _content_hash(content_snapshot),
            float(occurred_at),
            created,
        ),
    )
    event_id = db.require_lastrowid(cursor, "memory_ingest_event insert")
    return IngestEvent(
        id=event_id,
        source_type=source_type,
        source_id=source_id,
        source_revision=revision,
        owner_scope=owner_scope,
        visibility_scope=visibility_scope,
        op=op,
    )


# --- channel-specific convenience wrappers (encode boundary mapping) --------

def record_post_mutation(
    conn: sqlite3.Connection,
    *,
    post_id: str,
    op: str,
    content: str | None,
    occurred_at: float,
    created_at: float | None = None,
) -> IngestEvent:
    """Public post -> global + public."""
    return append_event(
        conn,
        owner_scope=GLOBAL_SCOPE,
        visibility_scope=PUBLIC_VISIBILITY,
        source_channel="post",
        source_type="post",
        source_id=str(post_id),
        op=op,
        content_snapshot=content,
        occurred_at=occurred_at,
        created_at=created_at,
    )


def record_comment_mutation(
    conn: sqlite3.Connection,
    *,
    comment_id: int,
    post_id: str,
    soul_name: str,
    role: str,
    op: str,
    content: str | None,
    occurred_at: float,
    created_at: float | None = None,
) -> IngestEvent:
    """Comment -> owner depends on author (user=global, soul=soul:<name>),
    visibility always thread:<post_id>. Thread membership never auto-promotes to
    public; that requires an explicit promote op in a later phase."""
    owner = GLOBAL_SCOPE if role == "user" else soul_scope(soul_name)
    return append_event(
        conn,
        owner_scope=owner,
        visibility_scope=thread_visibility(str(post_id)),
        source_channel="comment",
        source_type="comment_message",
        source_id=str(comment_id),
        op=op,
        content_snapshot=content,
        occurred_at=occurred_at,
        created_at=created_at,
    )


def record_chat_mutation(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    soul_name: str,
    op: str,
    content: str | None,
    occurred_at: float,
    created_at: float | None = None,
) -> IngestEvent:
    """Private chat -> soul:<name> + private:soul:<name> (both roles)."""
    return append_event(
        conn,
        owner_scope=soul_scope(soul_name),
        visibility_scope=private_visibility(soul_name),
        source_channel="chat",
        source_type="chat_message",
        source_id=str(message_id),
        op=op,
        content_snapshot=content,
        occurred_at=occurred_at,
        created_at=created_at,
    )


# --- cursor helpers (per owner+visibility bucket) --------------------------

def get_cursor(owner_scope: str, visibility_scope: str) -> int:
    row = db.query_one(
        """
        SELECT last_event_id
        FROM memory_reconcile_cursors
        WHERE owner_scope = ? AND visibility_scope = ?
        """,
        (owner_scope, visibility_scope),
    )
    return int(row["last_event_id"]) if row is not None else 0


def advance_cursor(
    conn: sqlite3.Connection,
    owner_scope: str,
    visibility_scope: str,
    last_event_id: int,
) -> None:
    """Move a bucket cursor forward (never backward) inside the caller's txn."""
    conn.execute(
        """
        INSERT INTO memory_reconcile_cursors(owner_scope, visibility_scope, last_event_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(owner_scope, visibility_scope) DO UPDATE SET
            last_event_id = MAX(last_event_id, excluded.last_event_id),
            updated_at = excluded.updated_at
        """,
        (owner_scope, visibility_scope, int(last_event_id), db.now_ts()),
    )


def list_events_after(
    owner_scope: str,
    visibility_scope: str,
    after_id: int,
    limit: int = 200,
) -> list[sqlite3.Row]:
    return db.query_all(
        """
        SELECT *
        FROM memory_ingest_events
        WHERE owner_scope = ? AND visibility_scope = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (owner_scope, visibility_scope, int(after_id), int(limit)),
    )


# --- backfill (seed revision=1 create events for pre-existing content) ------

def backfill_from_existing(conn: sqlite3.Connection) -> int:
    """Seed a revision=1 'create' event for every business row that has none.

    Idempotent: rows already carrying an event are skipped, so this is safe to
    run on every startup until live write-path hooks are in place.
    """
    inserted = 0

    for row in conn.execute(
        """
        SELECT id, content, created_at
        FROM posts
        WHERE id NOT IN (
            SELECT source_id FROM memory_ingest_events WHERE source_type = 'post'
        )
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall():
        record_post_mutation(
            conn,
            post_id=str(row["id"]),
            op="create",
            content=str(row["content"] or ""),
            occurred_at=float(row["created_at"]),
            created_at=float(row["created_at"]),
        )
        inserted += 1

    for row in conn.execute(
        """
        SELECT id, post_id, soul_name, role, content, created_at
        FROM comments
        WHERE CAST(id AS TEXT) NOT IN (
            SELECT source_id FROM memory_ingest_events WHERE source_type = 'comment_message'
        )
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall():
        record_comment_mutation(
            conn,
            comment_id=int(row["id"]),
            post_id=str(row["post_id"]),
            soul_name=str(row["soul_name"]),
            role=str(row["role"]),
            op="create",
            content=str(row["content"] or ""),
            occurred_at=float(row["created_at"]),
            created_at=float(row["created_at"]),
        )
        inserted += 1

    for row in conn.execute(
        """
        SELECT cm.id AS id, cm.content AS content, cm.created_at AS created_at,
               ct.soul_name AS soul_name
        FROM chat_messages cm
        JOIN chat_threads ct ON ct.id = cm.thread_id
        WHERE CAST(cm.id AS TEXT) NOT IN (
            SELECT source_id FROM memory_ingest_events WHERE source_type = 'chat_message'
        )
        ORDER BY cm.created_at ASC, cm.id ASC
        """
    ).fetchall():
        record_chat_mutation(
            conn,
            message_id=int(row["id"]),
            soul_name=str(row["soul_name"]),
            op="create",
            content=str(row["content"] or ""),
            occurred_at=float(row["created_at"]),
            created_at=float(row["created_at"]),
        )
        inserted += 1

    return inserted
