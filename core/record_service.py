"""Post persistence service."""

from __future__ import annotations

import json
from datetime import datetime

from core import db, logging_service


def save_post(content: str, *, index_immediately: bool = True, track_embedding: bool = True) -> str:
    """Save a post to SQLite, then try to index it in ChromaDB."""
    now = datetime.now().astimezone()
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
        if track_embedding:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (
                    f"pending_embedding:{post_id}",
                    _pending_embedding_payload(post_id, body, "pending before embedding"),
                ),
            )

    if index_immediately and track_embedding:
        try:
            index_post_embedding(post_id)
        except KeyboardInterrupt:
            raise
        except Exception:
            pass

    return post_id


def index_post_embedding(post_id: str) -> None:
    """Index one saved post into ChromaDB and clear its pending marker."""
    row = db.query_one(
        "SELECT content FROM posts WHERE id = ?",
        (post_id,),
    )
    if row is None:
        raise ValueError(f"post 不存在：{post_id}")
    body = row["content"]
    try:
        vectorstore = _vectorstore()
        if not vectorstore.is_initialized():
            raise RuntimeError("vectorstore is not initialized")
        vectorstore.index_post(post_id, body)
        _clear_pending_vector_doc(f"post-{post_id}")
        _clear_pending_embedding(post_id)
        logging_service.log_event("post_indexed", post_id=post_id, content_length=len(body))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _mark_pending_embedding(post_id, body, str(exc))
        _mark_pending_vector_doc(
            f"post-{post_id}",
            body,
            {"type": "post", "post_id": post_id},
            str(exc),
        )
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
        return
    doc_id = f"comment-{comment_id}"
    metadata = {
        "type": "comment",
        "comment_id": int(comment_id),
        "post_id": post_id,
        "soul_name": soul_name,
        "role": role,
        "seq": int(seq),
    }
    try:
        vectorstore = _vectorstore()
        if not vectorstore.is_initialized():
            raise RuntimeError("vectorstore is not initialized")
        vectorstore.index_comment(comment_id, post_id, soul_name, role, seq, body)
        _clear_pending_vector_doc(doc_id)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _mark_pending_vector_doc(doc_id, body, metadata, str(exc))
        logging_service.log_event(
            "vector_doc_index_failed",
            level="WARNING",
            doc_id=doc_id,
            type="comment",
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_index_comment"),
        )


def index_chat_message_embedding(message_id: int, thread_id: int, soul_name: str, role: str, content: str) -> None:
    body = content.strip()
    if not body:
        return
    doc_id = f"chat-{message_id}"
    metadata = {
        "type": "chat",
        "message_id": int(message_id),
        "thread_id": int(thread_id),
        "soul_name": soul_name,
        "role": role,
    }
    try:
        vectorstore = _vectorstore()
        if not vectorstore.is_initialized():
            raise RuntimeError("vectorstore is not initialized")
        vectorstore.index_chat_message(message_id, thread_id, soul_name, role, body)
        _clear_pending_vector_doc(doc_id)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _mark_pending_vector_doc(doc_id, body, metadata, str(exc))
        logging_service.log_event(
            "vector_doc_index_failed",
            level="WARNING",
            doc_id=doc_id,
            type="chat",
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_index_chat"),
        )


def index_post_vision_embedding(post_id: str, content: str, attachment_ids: list[str]) -> None:
    body = content.strip()
    if not body:
        return
    doc_id = f"post-vision-{post_id}"
    metadata = {
        "type": "post_vision",
        "post_id": post_id,
        "attachment_ids": ",".join(attachment_ids),
    }
    try:
        vectorstore = _vectorstore()
        if not vectorstore.is_initialized():
            raise RuntimeError("vectorstore is not initialized")
        vectorstore.index_post_vision(post_id, body, attachment_ids)
        _clear_pending_vector_doc(doc_id)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _mark_pending_vector_doc(doc_id, body, metadata, str(exc))
        logging_service.log_event(
            "vector_doc_index_failed",
            level="WARNING",
            doc_id=doc_id,
            type="post_vision",
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_index_post_vision"),
        )


def delete_post_embedding(post_id: str) -> None:
    _delete_vector_doc(f"post-{post_id}", "post", post_id=post_id)
    _clear_pending_embedding(post_id)
    _clear_pending_vector_doc(f"post-{post_id}")


def delete_post_vision_embedding(post_id: str) -> None:
    _delete_vector_doc(f"post-vision-{post_id}", "post_vision", post_id=post_id)
    _clear_pending_vector_doc(f"post-vision-{post_id}")


def delete_comment_embedding(comment_id: int) -> None:
    doc_id = f"comment-{int(comment_id)}"
    _delete_vector_doc(doc_id, "comment", comment_id=int(comment_id))
    _clear_pending_vector_doc(doc_id)


def delete_chat_message_embedding(message_id: int) -> None:
    doc_id = f"chat-{int(message_id)}"
    _delete_vector_doc(doc_id, "chat", message_id=int(message_id))
    _clear_pending_vector_doc(doc_id)


def delete_vector_docs(doc_ids: list[str]) -> None:
    ids = [doc_id for doc_id in doc_ids if isinstance(doc_id, str) and doc_id.strip()]
    if not ids:
        return
    try:
        vectorstore = _vectorstore()
        if vectorstore.is_initialized():
            vectorstore.delete_documents(ids)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logging_service.log_event(
            "vector_doc_delete_failed",
            level="WARNING",
            doc_ids=ids,
            error=str(exc),
            **_external_api_error_fields(exc, operation="vector_delete_docs"),
        )
    for doc_id in ids:
        _clear_pending_vector_doc(doc_id)


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


def retry_pending_embeddings(limit: int | None = None) -> int:
    """Retry pending ChromaDB indexing jobs. Returns the number fixed."""
    vectorstore = _vectorstore()
    if not vectorstore.is_initialized():
        return 0

    sql = """
        SELECT key, value
        FROM meta
        WHERE key LIKE 'pending_embedding:%'
        ORDER BY key
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)

    fixed = 0
    for row in db.query_all(sql, params):
        payload = None
        try:
            payload = json.loads(row["value"])
            post_id = payload["post_id"]
            content = payload["content"]
            vectorstore.index_post(post_id, content)
            db.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
            logging_service.log_event("post_indexed", post_id=post_id, retry=True, content_length=len(content))
            fixed += 1
        except Exception as exc:
            logging_service.log_event(
                "post_index_failed",
                level="WARNING",
                post_id=payload.get("post_id") if isinstance(payload, dict) else None,
                retry=True,
                error=str(exc),
                **_external_api_error_fields(exc, operation="vector_index_post"),
            )
            continue
    return fixed


def retry_pending_vector_docs(limit: int | None = None) -> int:
    vectorstore = _vectorstore()
    if not vectorstore.is_initialized():
        return 0

    sql = """
        SELECT key, value
        FROM meta
        WHERE key LIKE 'pending_vector_doc:%'
        ORDER BY key
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)

    fixed = 0
    for row in db.query_all(sql, params):
        try:
            payload = json.loads(row["value"])
            metadata = payload["metadata"]
            doc_type = metadata.get("type")
            if doc_type == "post":
                vectorstore.index_post(metadata["post_id"], payload["content"])
            elif doc_type == "comment":
                vectorstore.index_comment(
                    int(metadata["comment_id"]),
                    metadata["post_id"],
                    metadata["soul_name"],
                    metadata["role"],
                    int(metadata["seq"]),
                    payload["content"],
                )
            elif doc_type == "chat":
                vectorstore.index_chat_message(
                    int(metadata["message_id"]),
                    int(metadata["thread_id"]),
                    metadata["soul_name"],
                    metadata["role"],
                    payload["content"],
                )
            elif doc_type == "post_vision":
                attachment_ids = [
                    item for item in str(metadata.get("attachment_ids") or "").split(",") if item
                ]
                vectorstore.index_post_vision(metadata["post_id"], payload["content"], attachment_ids)
            else:
                continue
            db.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
            fixed += 1
        except Exception as exc:
            logging_service.log_event(
                "vector_doc_index_failed",
                level="WARNING",
                retry=True,
                key=row["key"],
                error=str(exc),
                **_external_api_error_fields(exc, operation="vector_index_retry"),
            )
    return fixed


def reindex_all_vector_docs() -> int:
    """Rebuild the live vector collection from SQLite facts."""
    vectorstore = _vectorstore()
    if not vectorstore.is_initialized():
        return 0
    indexed = 0
    for row in db.query_all("SELECT id, content FROM posts ORDER BY created_at ASC, id ASC"):
        try:
            vectorstore.index_post(row["id"], row["content"])
            indexed += 1
        except Exception as exc:
            _mark_pending_vector_doc(
                f"post-{row['id']}",
                row["content"],
                {"type": "post", "post_id": row["id"]},
                str(exc),
            )
    for row in db.query_all(
        """
        SELECT id, post_id, soul_name, role, seq, content
        FROM comments
        ORDER BY post_id ASC, soul_name ASC, seq ASC
        """
    ):
        try:
            vectorstore.index_comment(int(row["id"]), row["post_id"], row["soul_name"], row["role"], int(row["seq"]), row["content"])
            indexed += 1
        except Exception as exc:
            _mark_pending_vector_doc(
                f"comment-{row['id']}",
                row["content"],
                {
                    "type": "comment",
                    "comment_id": int(row["id"]),
                    "post_id": row["post_id"],
                    "soul_name": row["soul_name"],
                    "role": row["role"],
                    "seq": int(row["seq"]),
                },
                str(exc),
            )
    for row in db.query_all(
        """
        SELECT chat_messages.id, chat_messages.thread_id, chat_threads.soul_name,
               chat_messages.role, chat_messages.content
        FROM chat_messages
        JOIN chat_threads ON chat_threads.id = chat_messages.thread_id
        ORDER BY chat_messages.thread_id ASC, chat_messages.id ASC
        """
    ):
        try:
            vectorstore.index_chat_message(int(row["id"]), int(row["thread_id"]), row["soul_name"], row["role"], row["content"])
            indexed += 1
        except Exception as exc:
            _mark_pending_vector_doc(
                f"chat-{row['id']}",
                row["content"],
                {
                    "type": "chat",
                    "message_id": int(row["id"]),
                    "thread_id": int(row["thread_id"]),
                    "soul_name": row["soul_name"],
                    "role": row["role"],
                },
                str(exc),
            )
    for row in db.query_all(
        """
        SELECT
            post_attachments.post_id AS post_id,
            GROUP_CONCAT(vision_cache.attachment_id) AS attachment_ids,
            GROUP_CONCAT(vision_cache.description, '\n') AS descriptions
        FROM vision_cache
        JOIN post_attachments ON post_attachments.attachment_id = vision_cache.attachment_id
        WHERE vision_cache.status = 'ok'
        GROUP BY post_attachments.post_id
        ORDER BY post_attachments.post_id ASC
        """
    ):
        content = str(row["descriptions"] or "").strip()
        attachment_ids = [item for item in str(row["attachment_ids"] or "").split(",") if item]
        try:
            vectorstore.index_post_vision(row["post_id"], content, attachment_ids)
            indexed += 1
        except Exception as exc:
            _mark_pending_vector_doc(
                f"post-vision-{row['post_id']}",
                content,
                {
                    "type": "post_vision",
                    "post_id": row["post_id"],
                    "attachment_ids": ",".join(attachment_ids),
                },
                str(exc),
            )
    logging_service.log_event("vector_docs_reindexed", indexed=indexed)
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


def _mark_pending_embedding(post_id: str, content: str, error: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (f"pending_embedding:{post_id}", _pending_embedding_payload(post_id, content, error)),
    )


def _mark_pending_vector_doc(doc_id: str, content: str, metadata: dict, error: str) -> None:
    payload = {
        "doc_id": doc_id,
        "type": metadata.get("type"),
        "source_id": metadata.get("post_id") or metadata.get("comment_id") or metadata.get("message_id"),
        "content": content,
        "metadata": metadata,
        "error": error,
        "updated_at": db.now_ts(),
    }
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (f"pending_vector_doc:{doc_id}", json.dumps(payload, ensure_ascii=False)),
    )


def _delete_vector_doc(doc_id: str, doc_type: str, **metadata) -> None:
    try:
        vectorstore = _vectorstore()
        if vectorstore.is_initialized():
            vectorstore.delete_document(doc_id)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logging_service.log_event(
            "vector_doc_delete_failed",
            level="WARNING",
            doc_id=doc_id,
            type=doc_type,
            error=str(exc),
            **metadata,
            **_external_api_error_fields(exc, operation="vector_delete_doc"),
        )


def _pending_embedding_payload(post_id: str, content: str, error: str) -> str:
    payload = {
        "post_id": post_id,
        "content": content,
        "error": error,
        "created_at": db.now_ts(),
    }
    return json.dumps(payload, ensure_ascii=False)


def _clear_pending_embedding(post_id: str) -> None:
    db.execute("DELETE FROM meta WHERE key = ?", (f"pending_embedding:{post_id}",))


def _clear_pending_vector_doc(doc_id: str) -> None:
    db.execute("DELETE FROM meta WHERE key = ?", (f"pending_vector_doc:{doc_id}",))


def _external_api_error_fields(exc: Exception, *, operation: str) -> dict:
    return {
        "operation": operation,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
    }


def _vectorstore():
    from core import vectorstore
    return vectorstore
