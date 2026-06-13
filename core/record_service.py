"""Post persistence service."""

from __future__ import annotations

import json
from datetime import datetime

from core import db, logging_service, vector_index_service


def save_post(
    content: str,
    *,
    index_immediately: bool = True,
    track_embedding: bool = True,
    created_at: datetime | None = None,
) -> str:
    """Save a post to SQLite, then try to index it in ChromaDB."""
    now = _normalize_post_time(created_at)
    post_id = _next_post_id(now.strftime("%Y%m%d"))
    body = content.strip()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, now.isoformat(), body, now.timestamp(), now.timestamp()),
        )
        _snapshot_post_soul_order(conn, post_id, now.timestamp())
        if track_embedding:
            doc = vector_index_service.build_post_doc(post_id, body)
            if doc is not None:
                vector_index_service.upsert_doc(doc, conn=conn)

    if index_immediately and track_embedding:
        try:
            index_post_embedding(post_id)
        except KeyboardInterrupt:
            raise
        except Exception:
            pass

    return post_id


def _normalize_post_time(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    return value.astimezone()


def _snapshot_post_soul_order(conn, post_id: str, created_at: float) -> None:
    rows = conn.execute(
        """
        SELECT name, sort_order
        FROM souls
        WHERE enabled = 1
        ORDER BY sort_order ASC, name ASC
        """
    ).fetchall()
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR IGNORE INTO post_soul_orders(post_id, soul_name, sort_order, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [(post_id, row["name"], int(row["sort_order"]), created_at) for row in rows],
    )


def index_post_embedding(post_id: str) -> None:
    """Index one saved post into ChromaDB and clear its pending marker."""
    row = db.query_one(
        "SELECT content FROM posts WHERE id = ?",
        (post_id,),
    )
    if row is None:
        raise ValueError(f"post 不存在：{post_id}")
    body = row["content"]
    doc = vector_index_service.build_post_doc(post_id, body)
    if doc is None:
        vector_index_service.delete_doc(f"post-{post_id}")
        return
    vector_index_service.upsert_doc(doc)
    try:
        vector_index_service.process_outbox()
        logging_service.log_event("post_indexed", post_id=post_id, content_length=len(body))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logging_service.log_event(
            "post_index_failed",
            level="WARNING",
            post_id=post_id,
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_index_post"),
        )
        raise


def index_comment_embedding(comment_id: int, post_id: str, soul_name: str, role: str, seq: int, content: str) -> None:
    body = content.strip()
    if not body:
        vector_index_service.delete_doc(f"comment-{int(comment_id)}")
        return
    doc = vector_index_service.build_comment_doc(comment_id, post_id, soul_name, role, seq, body)
    if doc is None:
        return
    vector_index_service.upsert_doc(doc)
    try:
        vector_index_service.process_outbox()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logging_service.log_event(
            "vector_doc_index_failed",
            level="WARNING",
            doc_id=doc.doc_id,
            type="comment",
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_index_comment"),
        )


def index_chat_message_embedding(message_id: int, thread_id: int, soul_name: str, role: str, content: str) -> None:
    body = content.strip()
    if not body:
        vector_index_service.delete_doc(f"chat-{int(message_id)}")
        return
    doc = vector_index_service.build_chat_doc(message_id, thread_id, soul_name, role, body)
    if doc is None:
        return
    vector_index_service.upsert_doc(doc)
    try:
        vector_index_service.process_outbox()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logging_service.log_event(
            "vector_doc_index_failed",
            level="WARNING",
            doc_id=doc.doc_id,
            type="chat",
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_index_chat"),
        )


def index_post_vision_embedding(post_id: str, content: str, attachment_ids: list[str]) -> None:
    body = content.strip()
    if not body:
        vector_index_service.delete_doc(f"post-vision-{post_id}")
        return
    doc = vector_index_service.build_post_vision_doc(post_id, body, attachment_ids)
    if doc is None:
        return
    vector_index_service.upsert_doc(doc)
    try:
        vector_index_service.process_outbox()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logging_service.log_event(
            "vector_doc_index_failed",
            level="WARNING",
            doc_id=doc.doc_id,
            type="post_vision",
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_index_post_vision"),
        )


def delete_post_embedding(post_id: str) -> None:
    vector_index_service.delete_doc(f"post-{post_id}")
    _flush_vector_outbox_safely()


def delete_post_vision_embedding(post_id: str) -> None:
    vector_index_service.delete_doc(f"post-vision-{post_id}")
    _flush_vector_outbox_safely()


def delete_comment_embedding(comment_id: int) -> None:
    vector_index_service.delete_doc(f"comment-{int(comment_id)}")
    _flush_vector_outbox_safely()


def delete_chat_message_embedding(message_id: int) -> None:
    vector_index_service.delete_doc(f"chat-{int(message_id)}")
    _flush_vector_outbox_safely()


def delete_vector_docs(doc_ids: list[str]) -> None:
    ids = [doc_id for doc_id in doc_ids if isinstance(doc_id, str) and doc_id.strip()]
    if not ids:
        return
    vector_index_service.delete_docs(ids)
    _flush_vector_outbox_safely()


def format_post(row) -> str:
    """Format one SQLite post row as markdown with frontmatter."""
    content = str(row["content"] or "")
    try:
        from core import vision_service

        vision_context = vision_service.cached_context_for_post(str(row["id"]))
    except Exception:
        vision_context = ""
    if vision_context:
        content = f"{content}\n\n{vision_context}" if content.strip() else vision_context
    frontmatter = (
        "---\n"
        f"id: \"{row['id']}\"\n"
        f"date: \"{row['ts']}\"\n"
        "author: \"current_user\"\n"
        "source: \"current_user_public_post\"\n"
        "type: \"post\"\n"
        "---\n\n"
    )
    return frontmatter + f"\n{content}\n"


def retry_pending_vector_docs(limit: int | None = None) -> int:
    migrate_pending_embeddings()
    vector_index_service.migrate_legacy_pending_vector_docs()
    vector_index_service.rebuild_expected_docs()
    return vector_index_service.process_outbox(limit=limit)


def migrate_pending_embeddings() -> int:
    """Convert legacy pending_embedding markers into the vector ledger."""
    rows = db.query_all(
        """
        SELECT key, value
        FROM meta
        WHERE key LIKE 'pending_embedding:%'
        ORDER BY key
        """
    )
    migrated = 0
    with db.transaction() as conn:
        for row in rows:
            try:
                payload = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                conn.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
                continue
            if not isinstance(payload, dict):
                conn.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
                continue
            post_id = str(payload.get("post_id") or str(row["key"])[len("pending_embedding:"):]).strip()
            content = str(payload.get("content") or "")
            if not post_id:
                conn.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
                continue
            doc = vector_index_service.build_post_doc(post_id, content)
            if doc is not None:
                vector_index_service.upsert_doc(doc, conn=conn)
            conn.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
            migrated += 1
    return migrated


def reindex_all_vector_docs() -> int:
    """Reconcile expected vector docs from SQLite and flush the active vector outbox."""
    changed = vector_index_service.rebuild_expected_docs()
    indexed = vector_index_service.process_outbox()
    logging_service.log_event("vector_docs_reindexed", changed=changed, indexed=indexed)
    return indexed


def _next_post_id(today: str) -> str:
    row = db.query_one(
        """
        SELECT id
        FROM posts
        WHERE id LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{today}-%",),
    )
    if row is None:
        return f"{today}-001"
    try:
        seq = int(str(row["id"]).split("-")[1]) + 1
    except (IndexError, ValueError):
        seq = 1
    return f"{today}-{seq:03d}"


def _flush_vector_outbox_safely() -> None:
    try:
        vector_index_service.process_outbox()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logging_service.log_event(
            "vector_outbox_flush_failed",
            level="WARNING",
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_outbox_flush"),
        )


def _external_api_error_fields(exc: Exception, *, operation: str) -> dict:
    return {
        "operation": operation,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
    }
