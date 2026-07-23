"""SQLite ledger for keeping vector indexes in sync with source facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from core import db, logging_service

SOURCE_REVISION_KEY = "vector_source_revision"
STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
AUDIT_READY = "ready"
AUDIT_PENDING = "pending"
AUDIT_FAILED = "failed"
ORPHAN_REVISION = -1


@dataclass(frozen=True)
class VectorDoc:
    doc_id: str
    doc_type: str
    source_table: str
    source_id: str
    content: str
    metadata: dict[str, Any]

    @property
    def content_hash(self) -> str:
        return content_hash(self.content, self.metadata)


@dataclass(frozen=True)
class CollectionState:
    collection_name: str
    embedding_config_hash: str
    embedding_model: str
    embedding_base_url: str
    audit_status: str
    source_revision: int
    synced_revision: int
    ready: bool
    pending_count: int
    failed_count: int
    missing_count: int
    stale_count: int
    indexed_count: int
    total_count: int

    @property
    def query_ready(self) -> bool:
        return (
            self.ready
            and self.audit_status == AUDIT_READY
            and self.synced_revision >= self.source_revision
            and self.pending_count == 0
            and self.failed_count == 0
            and self.missing_count == 0
            and self.stale_count == 0
        )


def content_hash(content: str, metadata: dict[str, Any]) -> str:
    payload = {
        "content": str(content or ""),
        "metadata": _stable_json(metadata),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_post_doc(post_id: str, content: str) -> VectorDoc | None:
    body = str(content or "").strip()
    source_id = str(post_id or "").strip()
    if not body or not source_id:
        return None
    return VectorDoc(
        doc_id=f"post-{source_id}",
        doc_type="post",
        source_table="posts",
        source_id=source_id,
        content=body,
        metadata={"type": "post", "post_id": source_id},
    )


def build_comment_doc(comment_id: int, post_id: str, soul_name: str, role: str, seq: int, content: str) -> VectorDoc | None:
    body = str(content or "").strip()
    if not body:
        return None
    return VectorDoc(
        doc_id=f"comment-{int(comment_id)}",
        doc_type="comment",
        source_table="comments",
        source_id=str(int(comment_id)),
        content=body,
        metadata={
            "type": "comment",
            "comment_id": int(comment_id),
            "post_id": str(post_id),
            "soul_name": str(soul_name),
            "role": str(role),
            "seq": int(seq),
        },
    )


def build_chat_doc(message_id: int, thread_id: int, soul_name: str, role: str, content: str) -> VectorDoc | None:
    body = str(content or "").strip()
    if not body:
        return None
    return VectorDoc(
        doc_id=f"chat-{int(message_id)}",
        doc_type="chat",
        source_table="chat_messages",
        source_id=str(int(message_id)),
        content=body,
        metadata={
            "type": "chat",
            "message_id": int(message_id),
            "thread_id": int(thread_id),
            "soul_name": str(soul_name),
            "role": str(role),
        },
    )


def build_post_vision_doc(post_id: str, content: str, attachment_ids: list[str]) -> VectorDoc | None:
    body = str(content or "").strip()
    source_id = str(post_id or "").strip()
    if not body or not source_id:
        return None
    clean_attachment_ids = [str(item) for item in attachment_ids if str(item).strip()]
    return VectorDoc(
        doc_id=f"post-vision-{source_id}",
        doc_type="post_vision",
        source_table="vision_cache",
        source_id=source_id,
        content=body,
        metadata={
            "type": "post_vision",
            "post_id": source_id,
            "attachment_ids": ",".join(clean_attachment_ids),
        },
    )


def build_unit_doc(
    unit_id: str,
    content: str,
    owner_scope: str,
    visibility_scope: str,
    unit_type: str,
) -> VectorDoc | None:
    body = str(content or "").strip()
    uid = str(unit_id or "").strip()
    if not body or not uid:
        return None
    return VectorDoc(
        doc_id=f"unit-{uid}",
        doc_type="unit",
        source_table="memory_units",
        source_id=uid,
        content=body,
        metadata={
            "type": "unit",
            "unit_id": uid,
            "owner_scope": str(owner_scope),
            "visibility_scope": str(visibility_scope),
            "unit_type": str(unit_type),
        },
    )


def build_tombstone_doc(
    unit_id: str,
    claim: str,
    owner_scope: str,
    visibility_scope: str,
    reason: str,
) -> VectorDoc | None:
    """Vector entry for a false-retracted belief's normalized claim (P2): the
    reconciler's add-time similarity guard queries these to block zombie
    re-derivation even when the model rephrases. Declarative like every other
    doc: restoring the unit drops the row from expected_docs_from_sqlite and the
    entry retires on the next rebuild."""
    body = str(claim or "").strip()
    uid = str(unit_id or "").strip()
    if not body or not uid:
        return None
    return VectorDoc(
        doc_id=f"tombstone-{uid}",
        doc_type="tombstone",
        source_table="memory_units",
        source_id=uid,
        content=body,
        metadata={
            "type": "tombstone",
            "unit_id": uid,
            "owner_scope": str(owner_scope),
            "visibility_scope": str(visibility_scope),
            "reason": str(reason),
        },
    )


def upsert_doc(doc: VectorDoc, *, conn: sqlite3.Connection | None = None) -> int:
    if conn is None:
        with db.transaction() as tx:
            return upsert_doc(doc, conn=tx)
    existing = conn.execute(
        "SELECT content_hash, source_revision FROM vector_docs WHERE doc_id = ?",
        (doc.doc_id,),
    ).fetchone()
    if existing is not None and str(existing["content_hash"]) == doc.content_hash:
        revision = int(existing["source_revision"])
        _enqueue_doc_for_collections(conn, doc, revision)
        return revision
    revision = _next_revision_conn(conn)
    _upsert_doc_at_revision(conn, doc, revision)
    _enqueue_doc_for_collections(conn, doc, revision)
    return revision


def delete_doc(doc_id: str, *, conn: sqlite3.Connection | None = None) -> int:
    if conn is None:
        with db.transaction() as tx:
            return delete_doc(doc_id, conn=tx)
    revision = _next_revision_conn(conn)
    _delete_doc_at_revision(conn, doc_id, revision)
    _enqueue_delete_for_collections(conn, doc_id, revision)
    return revision


def delete_docs(doc_ids: list[str], *, conn: sqlite3.Connection | None = None) -> int:
    ids = [str(doc_id).strip() for doc_id in doc_ids if str(doc_id).strip()]
    if not ids:
        return current_source_revision()
    if conn is None:
        with db.transaction() as tx:
            return delete_docs(ids, conn=tx)
    revision = _next_revision_conn(conn)
    for doc_id in ids:
        _delete_doc_at_revision(conn, doc_id, revision)
        _enqueue_delete_for_collections(conn, doc_id, revision)
    return revision


def replace_doc(doc: VectorDoc | None, doc_id: str, *, conn: sqlite3.Connection | None = None) -> int:
    if conn is None:
        with db.transaction() as tx:
            return replace_doc(doc, doc_id, conn=tx)
    if doc is None:
        return delete_doc(doc_id, conn=conn)
    return upsert_doc(doc, conn=conn)


def current_source_revision() -> int:
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (SOURCE_REVISION_KEY,))
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def ensure_collection(
    *,
    collection_name: str,
    embedding_config_hash: str,
    embedding_model: str,
    embedding_base_url: str,
) -> CollectionState:
    now = db.now_ts()
    with db.transaction() as conn:
        source_revision = _current_source_revision_conn(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO vector_index_collections(
                collection_name, embedding_config_hash, embedding_model, embedding_base_url,
                synced_revision, ready, audit_status, updated_at
            )
            VALUES (?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (collection_name, embedding_config_hash, embedding_model, embedding_base_url, AUDIT_PENDING, now),
        )
        conn.execute(
            """
            UPDATE vector_index_collections
            SET embedding_config_hash = ?,
                embedding_model = ?,
                embedding_base_url = ?,
                updated_at = ?
            WHERE collection_name = ?
            """,
            (embedding_config_hash, embedding_model, embedding_base_url, now, collection_name),
        )
        _enqueue_collection_drift(conn, collection_name)
        _refresh_collection_state_conn(conn, collection_name)
    return collection_state(collection_name)


def collection_state(collection_name: str) -> CollectionState:
    row = db.query_one(
        """
        SELECT *
        FROM vector_index_collections
        WHERE collection_name = ?
        """,
        (collection_name,),
    )
    if row is None:
        source_revision = current_source_revision()
        return CollectionState(
            collection_name=collection_name,
            embedding_config_hash="",
            embedding_model="",
            embedding_base_url="",
            audit_status=AUDIT_PENDING,
            source_revision=source_revision,
            synced_revision=0,
            ready=False,
            pending_count=0,
            failed_count=0,
            missing_count=0,
            stale_count=0,
            indexed_count=0,
            total_count=_count_total_docs(),
        )
    return _collection_state_from_row(row)


def current_collection_state() -> CollectionState | None:
    try:
        from core import vectorstore

        collection_name = vectorstore.current_collection_name()
    except Exception:
        collection_name = None
    if not collection_name:
        return None
    return collection_state(collection_name)


def is_current_collection_query_ready() -> bool:
    state = current_collection_state()
    return bool(state and state.query_ready)


def process_outbox(collection_name: str | None = None, *, limit: int | None = None) -> int:
    try:
        from core import vectorstore
    except Exception:
        return 0
    if not vectorstore.is_initialized():
        return 0
    active_collection = collection_name or vectorstore.current_collection_name()
    if not active_collection:
        return 0

    sql = """
        SELECT *
        FROM vector_outbox
        WHERE collection_name = ?
          AND status IN (?, ?)
        ORDER BY id ASC
    """
    params: list[Any] = [active_collection, STATUS_PENDING, STATUS_FAILED]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))

    processed = 0
    for row in db.query_all(sql, params):
        outbox_id = int(row["id"])
        try:
            _process_outbox_row(vectorstore, row)
            processed += 1
        except Exception as exc:
            _mark_outbox_failed(outbox_id, exc)
            logging_service.log_event(
                "vector_outbox_failed",
                level="WARNING",
                collection_name=active_collection,
                doc_id=row["doc_id"],
                op=row["op"],
                error=str(exc),
            )
    audit_failed = False
    try:
        _audit_active_collection(vectorstore, active_collection)
    except Exception as exc:
        audit_failed = True
        _mark_collection_audit_failed(active_collection, exc)
        logging_service.log_event(
            "vector_collection_audit_failed",
            level="WARNING",
            collection_name=active_collection,
            error=str(exc),
        )
    if not audit_failed:
        with db.transaction() as conn:
            _enqueue_collection_drift(conn, active_collection)
            _refresh_collection_state_conn(conn, active_collection)
    return processed


def rebuild_expected_docs() -> int:
    expected = expected_docs_from_sqlite()
    with db.transaction() as conn:
        old_doc_ids = {str(row["doc_id"]) for row in conn.execute("SELECT doc_id FROM vector_docs").fetchall()}
        new_doc_ids = {doc.doc_id for doc in expected}
        changed = 0
        for doc in expected:
            existing = conn.execute(
                "SELECT content_hash FROM vector_docs WHERE doc_id = ?",
                (doc.doc_id,),
            ).fetchone()
            if existing is None or str(existing["content_hash"]) != doc.content_hash:
                revision = _next_revision_conn(conn)
                _upsert_doc_at_revision(conn, doc, revision)
                _enqueue_doc_for_collections(conn, doc, revision)
                changed += 1
        for doc_id in sorted(old_doc_ids - new_doc_ids):
            revision = _next_revision_conn(conn)
            _delete_doc_at_revision(conn, doc_id, revision)
            _enqueue_delete_for_collections(conn, doc_id, revision)
            changed += 1
        for row in conn.execute("SELECT collection_name FROM vector_index_collections").fetchall():
            _enqueue_collection_drift(conn, str(row["collection_name"]))
            _refresh_collection_state_conn(conn, str(row["collection_name"]))
    return changed


def expected_docs_from_sqlite() -> list[VectorDoc]:
    docs: list[VectorDoc] = []
    for row in db.query_all("SELECT id, content FROM posts ORDER BY created_at ASC, id ASC"):
        doc = build_post_doc(row["id"], row["content"])
        if doc is not None:
            docs.append(doc)
    for row in db.query_all(
        """
        SELECT id, post_id, soul_name, role, seq, content
        FROM comments
        ORDER BY post_id ASC, soul_name ASC, seq ASC
        """
    ):
        doc = build_comment_doc(int(row["id"]), row["post_id"], row["soul_name"], row["role"], int(row["seq"]), row["content"])
        if doc is not None:
            docs.append(doc)
    for row in db.query_all(
        """
        SELECT chat_messages.id, chat_messages.thread_id, chat_threads.soul_name,
               chat_messages.role, chat_messages.content
        FROM chat_messages
        JOIN chat_threads ON chat_threads.id = chat_messages.thread_id
        ORDER BY chat_messages.thread_id ASC, chat_messages.id ASC
        """
    ):
        doc = build_chat_doc(int(row["id"]), int(row["thread_id"]), row["soul_name"], row["role"], row["content"])
        if doc is not None:
            docs.append(doc)
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
        doc = build_post_vision_doc(row["post_id"], content, attachment_ids)
        if doc is not None:
            docs.append(doc)
    for row in db.query_all(
        """
        SELECT id, content, owner_scope, visibility_scope, type
        FROM memory_units
        WHERE status = 'active'
        ORDER BY id ASC
        """
    ):
        doc = build_unit_doc(
            row["id"], row["content"], row["owner_scope"], row["visibility_scope"], row["type"]
        )
        if doc is not None:
            docs.append(doc)
    # false tombstones with a backfilled claim: the add-time zombie guard's
    # search set. outdated tombstones are prompt-guidance only — new evidence
    # may legitimately re-establish them, so they get no blocking vector.
    for row in db.query_all(
        """
        SELECT id, normalized_claim, owner_scope, visibility_scope, retraction_reason
        FROM memory_units
        WHERE status IN ('retracted_by_user','retracted_by_model')
          AND retraction_reason = 'false'
          AND TRIM(COALESCE(normalized_claim, '')) != ''
        ORDER BY id ASC
        """
    ):
        doc = build_tombstone_doc(
            row["id"], row["normalized_claim"], row["owner_scope"],
            row["visibility_scope"], row["retraction_reason"],
        )
        if doc is not None:
            docs.append(doc)
    return docs


def _process_outbox_row(vectorstore, row) -> None:
    outbox_id = int(row["id"])
    collection_name = str(row["collection_name"])
    doc_id = str(row["doc_id"])
    op = str(row["op"])
    now = db.now_ts()
    if op == "delete":
        vectorstore.delete_document(doc_id)
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM vector_index_items WHERE collection_name = ? AND doc_id = ?",
                (collection_name, doc_id),
            )
            _mark_outbox_succeeded_conn(conn, outbox_id, now)
        return

    doc = db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", (doc_id,))
    if doc is None:
        vectorstore.delete_document(doc_id)
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM vector_index_items WHERE collection_name = ? AND doc_id = ?",
                (collection_name, doc_id),
            )
            _mark_outbox_succeeded_conn(conn, outbox_id, now)
        return

    metadata = json.loads(doc["metadata_json"])
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update(
        {
            "content_hash": doc["content_hash"],
            "source_revision": int(doc["source_revision"]),
        }
    )
    vectors = vectorstore.embed_texts([str(doc["content"])])
    # 单条 outbox 文档若被 provider 返回 0 条或 2 条 embedding，就不能安全落账。
    if len(vectors) != 1:
        raise RuntimeError(f"embedding response count mismatch: expected 1, got {len(vectors)}")
    dim, embedding = vectorstore.serialize_embedding(vectors[0])
    with db.transaction() as conn:
        dimensions = {
            int(item["dim"])
            for item in conn.execute(
                """
                SELECT DISTINCT dim
                FROM vector_index_items
                WHERE collection_name = ?
                  AND dim IS NOT NULL
                  AND embedding IS NOT NULL
                """,
                (collection_name,),
            ).fetchall()
        }
        # 同一集合已有 2 维行、provider 此次返回 3 维向量时会触发该错误。
        if dimensions and dimensions != {dim}:
            existing = ", ".join(str(value) for value in sorted(dimensions))
            raise ValueError(
                f"embedding dimension mismatch for {collection_name}: existing {existing}, new {dim}"
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO vector_index_items(
                collection_name, doc_id, content_hash, source_revision,
                indexed_at, dim, embedding
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                collection_name,
                doc_id,
                doc["content_hash"],
                int(doc["source_revision"]),
                now,
                dim,
                embedding,
            ),
        )
        _mark_outbox_succeeded_conn(conn, outbox_id, now)


def _audit_active_collection(vectorstore, collection_name: str) -> None:
    if not hasattr(vectorstore, "list_document_records"):
        return
    expected = {
        str(row["doc_id"]): {
            "content_hash": str(row["content_hash"]),
            "source_revision": int(row["source_revision"]),
        }
        for row in db.query_all(
            "SELECT doc_id, content_hash, source_revision FROM vector_docs ORDER BY doc_id"
        )
    }
    actual_records = vectorstore.list_document_records()
    actual_ids = set(actual_records)
    expected_ids = set(expected)
    stale_ids = sorted(actual_ids - expected_ids)
    now = db.now_ts()
    with db.transaction() as conn:
        for doc_id in sorted(expected_ids):
            metadata = actual_records.get(doc_id)
            expected_item = expected[doc_id]
            if not _actual_record_matches_expected(metadata, expected_item):
                conn.execute(
                    "DELETE FROM vector_index_items WHERE collection_name = ? AND doc_id = ?",
                    (collection_name, doc_id),
                )
        if stale_ids:
            vectorstore.delete_documents(stale_ids)
            for doc_id in stale_ids:
                conn.execute(
                    "DELETE FROM vector_index_items WHERE collection_name = ? AND doc_id = ?",
                    (collection_name, doc_id),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO vector_doc_tombstones(doc_id, deleted_revision, deleted_at)
                    VALUES (?, ?, ?)
                    """,
                    (doc_id, ORPHAN_REVISION, now),
                )
        _enqueue_collection_drift(conn, collection_name)
        _refresh_collection_state_conn(conn, collection_name)


def _actual_record_matches_expected(metadata: dict | None, expected: dict[str, Any]) -> bool:
    if not isinstance(metadata, dict):
        return False
    if str(metadata.get("content_hash") or "") != str(expected["content_hash"]):
        return False
    try:
        actual_revision = int(metadata.get("source_revision"))
    except (TypeError, ValueError):
        return False
    return actual_revision >= int(expected["source_revision"])


def _mark_collection_audit_failed(collection_name: str, exc: Exception) -> None:
    now = db.now_ts()
    db.execute(
        """
        UPDATE vector_index_collections
        SET ready = 0,
            audit_status = ?,
            updated_at = ?
        WHERE collection_name = ?
        """,
        (AUDIT_FAILED, now, collection_name),
    )


def _doc_from_payload(payload: dict[str, Any]) -> VectorDoc | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    doc_type = str(metadata.get("type") or payload.get("type") or "")
    content = str(payload.get("content") or "")
    try:
        if doc_type == "post":
            return build_post_doc(str(metadata.get("post_id") or ""), content)
        if doc_type == "comment":
            return build_comment_doc(
                int(metadata["comment_id"]),
                str(metadata["post_id"]),
                str(metadata["soul_name"]),
                str(metadata["role"]),
                int(metadata["seq"]),
                content,
            )
        if doc_type == "chat":
            return build_chat_doc(
                int(metadata["message_id"]),
                int(metadata["thread_id"]),
                str(metadata["soul_name"]),
                str(metadata["role"]),
                content,
            )
        if doc_type == "post_vision":
            attachment_ids = [item for item in str(metadata.get("attachment_ids") or "").split(",") if item]
            return build_post_vision_doc(str(metadata.get("post_id") or ""), content, attachment_ids)
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _collection_state_from_row(row) -> CollectionState:
    collection_name = str(row["collection_name"])
    pending_count = _count_outbox(collection_name, STATUS_PENDING)
    failed_count = _count_outbox(collection_name, STATUS_FAILED)
    missing_count = _count_missing(collection_name)
    stale_count = _count_stale(collection_name)
    indexed_count = _count_indexed(collection_name)
    total_count = _count_total_docs()
    return CollectionState(
        collection_name=collection_name,
        embedding_config_hash=str(row["embedding_config_hash"]),
        embedding_model=str(row["embedding_model"]),
        embedding_base_url=str(row["embedding_base_url"]),
        audit_status=str(row["audit_status"]),
        source_revision=current_source_revision(),
        synced_revision=int(row["synced_revision"]),
        ready=bool(int(row["ready"])),
        pending_count=pending_count,
        failed_count=failed_count,
        missing_count=missing_count,
        stale_count=stale_count,
        indexed_count=indexed_count,
        total_count=total_count,
    )


def _count_outbox(collection_name: str, status: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS count FROM vector_outbox WHERE collection_name = ? AND status = ?",
        (collection_name, status),
    )
    return int(row["count"]) if row is not None else 0


def _count_missing(collection_name: str) -> int:
    row = db.query_one(
        """
        SELECT COUNT(*) AS count
        FROM vector_docs
        LEFT JOIN vector_index_items
          ON vector_index_items.collection_name = ?
         AND vector_index_items.doc_id = vector_docs.doc_id
        WHERE vector_index_items.doc_id IS NULL
           OR vector_index_items.dim IS NULL
           OR vector_index_items.embedding IS NULL
        """,
        (collection_name,),
    )
    return int(row["count"]) if row is not None else 0


def _count_stale(collection_name: str) -> int:
    row = db.query_one(
        """
        SELECT COUNT(*) AS count
        FROM vector_docs
        JOIN vector_index_items
          ON vector_index_items.collection_name = ?
         AND vector_index_items.doc_id = vector_docs.doc_id
        WHERE vector_index_items.content_hash != vector_docs.content_hash
           OR vector_index_items.source_revision < vector_docs.source_revision
        """,
        (collection_name,),
    )
    return int(row["count"]) if row is not None else 0


def _count_indexed(collection_name: str) -> int:
    row = db.query_one(
        """
        SELECT COUNT(*) AS count
        FROM vector_docs
        JOIN vector_index_items
          ON vector_index_items.collection_name = ?
         AND vector_index_items.doc_id = vector_docs.doc_id
        WHERE vector_index_items.dim IS NOT NULL
          AND vector_index_items.embedding IS NOT NULL
          AND vector_index_items.content_hash = vector_docs.content_hash
          AND vector_index_items.source_revision >= vector_docs.source_revision
        """,
        (collection_name,),
    )
    return int(row["count"]) if row is not None else 0


def _count_total_docs() -> int:
    row = db.query_one("SELECT COUNT(*) AS count FROM vector_docs")
    return int(row["count"]) if row is not None else 0


def _refresh_collection_state_conn(conn: sqlite3.Connection, collection_name: str) -> None:
    source_revision = _current_source_revision_conn(conn)
    pending = _count_outbox_conn(conn, collection_name, STATUS_PENDING)
    failed = _count_outbox_conn(conn, collection_name, STATUS_FAILED)
    missing = _count_missing_conn(conn, collection_name)
    stale = _count_stale_conn(conn, collection_name)
    ready = pending == 0 and failed == 0 and missing == 0 and stale == 0
    synced_revision = source_revision if ready else _synced_revision_floor_conn(conn, collection_name)
    now = db.now_ts()
    conn.execute(
        """
        UPDATE vector_index_collections
        SET synced_revision = ?,
            ready = ?,
            audit_status = ?,
            last_audited_at = ?,
            updated_at = ?
        WHERE collection_name = ?
        """,
        (synced_revision, 1 if ready else 0, AUDIT_READY if ready else AUDIT_PENDING, now, now, collection_name),
    )


def _enqueue_collection_drift(conn: sqlite3.Connection, collection_name: str) -> None:
    for row in conn.execute(
        """
        SELECT vector_docs.*
        FROM vector_docs
        LEFT JOIN vector_index_items
          ON vector_index_items.collection_name = ?
         AND vector_index_items.doc_id = vector_docs.doc_id
        WHERE vector_index_items.doc_id IS NULL
           OR vector_index_items.dim IS NULL
           OR vector_index_items.embedding IS NULL
           OR vector_index_items.content_hash != vector_docs.content_hash
           OR vector_index_items.source_revision < vector_docs.source_revision
        ORDER BY vector_docs.source_revision ASC, vector_docs.doc_id ASC
        """,
        (collection_name,),
    ).fetchall():
        _enqueue_outbox_conn(
            conn,
            collection_name,
            str(row["doc_id"]),
            "upsert",
            str(row["content_hash"]),
            int(row["source_revision"]),
        )
    for row in conn.execute(
        """
        SELECT vector_index_items.doc_id, vector_index_items.source_revision
        FROM vector_index_items
        LEFT JOIN vector_docs ON vector_docs.doc_id = vector_index_items.doc_id
        WHERE vector_index_items.collection_name = ?
          AND vector_docs.doc_id IS NULL
        ORDER BY vector_index_items.source_revision ASC, vector_index_items.doc_id ASC
        """,
        (collection_name,),
    ).fetchall():
        _enqueue_outbox_conn(
            conn,
            collection_name,
            str(row["doc_id"]),
            "delete",
            None,
            int(row["source_revision"]),
        )


def _enqueue_doc_for_collections(conn: sqlite3.Connection, doc: VectorDoc, revision: int) -> None:
    for row in conn.execute("SELECT collection_name FROM vector_index_collections").fetchall():
        _enqueue_outbox_conn(conn, str(row["collection_name"]), doc.doc_id, "upsert", doc.content_hash, revision)


def _enqueue_delete_for_collections(conn: sqlite3.Connection, doc_id: str, revision: int) -> None:
    for row in conn.execute("SELECT collection_name FROM vector_index_collections").fetchall():
        collection_name = str(row["collection_name"])
        conn.execute(
            """
            UPDATE vector_outbox
            SET status = ?,
                source_revision = ?,
                updated_at = ?,
                finished_at = NULL
            WHERE collection_name = ?
              AND doc_id = ?
              AND op = 'upsert'
              AND status IN (?, ?)
            """,
            (
                STATUS_SUCCEEDED,
                revision,
                db.now_ts(),
                collection_name,
                doc_id,
                STATUS_PENDING,
                STATUS_FAILED,
            ),
        )
        _enqueue_outbox_conn(conn, collection_name, doc_id, "delete", None, revision)


def _enqueue_outbox_conn(
    conn: sqlite3.Connection,
    collection_name: str,
    doc_id: str,
    op: str,
    target_hash: str | None,
    revision: int,
) -> None:
    existing = conn.execute(
        """
        SELECT id, status, target_hash, source_revision
        FROM vector_outbox
        WHERE collection_name = ?
          AND doc_id = ?
          AND op = ?
          AND status IN (?, ?)
        ORDER BY id ASC
        LIMIT 1
        """,
        (collection_name, doc_id, op, STATUS_PENDING, STATUS_FAILED),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["status"]) == STATUS_FAILED
            and (existing["target_hash"] is None if target_hash is None else str(existing["target_hash"]) == target_hash)
            and int(existing["source_revision"]) == int(revision)
        ):
            return
        conn.execute(
            """
            UPDATE vector_outbox
            SET status = ?,
                target_hash = ?,
                source_revision = ?,
                updated_at = ?,
                error = NULL,
                finished_at = NULL
            WHERE id = ?
            """,
            (
                STATUS_PENDING,
                target_hash,
                revision,
                db.now_ts(),
                int(existing["id"]),
            ),
        )
        return
    cursor = conn.execute(
        """
        UPDATE vector_outbox
        SET status = ?,
            target_hash = ?,
            source_revision = ?,
            updated_at = ?,
            error = NULL,
            finished_at = NULL
        WHERE collection_name = ?
          AND doc_id = ?
          AND op = ?
          AND status IN (?, ?)
        """,
        (
            STATUS_PENDING,
            target_hash,
            revision,
            db.now_ts(),
            collection_name,
            doc_id,
            op,
            STATUS_PENDING,
            STATUS_FAILED,
        ),
    )
    if cursor.rowcount > 0:
        return
    now = db.now_ts()
    conn.execute(
        """
        INSERT INTO vector_outbox(
            collection_name, doc_id, op, target_hash, source_revision,
            status, attempts, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (collection_name, doc_id, op, target_hash, revision, STATUS_PENDING, now, now),
    )


def _upsert_doc_at_revision(conn: sqlite3.Connection, doc: VectorDoc, revision: int) -> None:
    now = db.now_ts()
    conn.execute(
        """
        INSERT OR REPLACE INTO vector_docs(
            doc_id, doc_type, source_table, source_id, content,
            content_hash, metadata_json, source_revision, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc.doc_id,
            doc.doc_type,
            doc.source_table,
            doc.source_id,
            doc.content,
            doc.content_hash,
            _stable_json(doc.metadata),
            revision,
            now,
        ),
    )
    conn.execute("DELETE FROM vector_doc_tombstones WHERE doc_id = ?", (doc.doc_id,))


def _delete_doc_at_revision(conn: sqlite3.Connection, doc_id: str, revision: int) -> None:
    now = db.now_ts()
    conn.execute("DELETE FROM vector_docs WHERE doc_id = ?", (doc_id,))
    conn.execute(
        """
        INSERT OR REPLACE INTO vector_doc_tombstones(doc_id, deleted_revision, deleted_at)
        VALUES (?, ?, ?)
        """,
        (doc_id, revision, now),
    )


def _next_revision_conn(conn: sqlite3.Connection) -> int:
    revision = _current_source_revision_conn(conn) + 1
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (SOURCE_REVISION_KEY, str(revision)),
    )
    return revision


def _current_source_revision_conn(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (SOURCE_REVISION_KEY,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _count_outbox_conn(conn: sqlite3.Connection, collection_name: str, status: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM vector_outbox WHERE collection_name = ? AND status = ?",
        (collection_name, status),
    ).fetchone()
    return int(row["count"]) if row is not None else 0


def _count_missing_conn(conn: sqlite3.Connection, collection_name: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM vector_docs
        LEFT JOIN vector_index_items
          ON vector_index_items.collection_name = ?
         AND vector_index_items.doc_id = vector_docs.doc_id
        WHERE vector_index_items.doc_id IS NULL
           OR vector_index_items.dim IS NULL
           OR vector_index_items.embedding IS NULL
        """,
        (collection_name,),
    ).fetchone()
    return int(row["count"]) if row is not None else 0


def _count_stale_conn(conn: sqlite3.Connection, collection_name: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM vector_docs
        JOIN vector_index_items
          ON vector_index_items.collection_name = ?
         AND vector_index_items.doc_id = vector_docs.doc_id
        WHERE vector_index_items.content_hash != vector_docs.content_hash
           OR vector_index_items.source_revision < vector_docs.source_revision
        """,
        (collection_name,),
    ).fetchone()
    return int(row["count"]) if row is not None else 0


def _synced_revision_floor_conn(conn: sqlite3.Connection, collection_name: str) -> int:
    row = conn.execute(
        """
        SELECT MIN(source_revision) AS revision
        FROM vector_index_items
        WHERE collection_name = ?
          AND dim IS NOT NULL
          AND embedding IS NOT NULL
        """,
        (collection_name,),
    ).fetchone()
    if row is None or row["revision"] is None:
        return 0
    return int(row["revision"])


def _mark_outbox_failed(outbox_id: int, exc: Exception) -> None:
    now = db.now_ts()
    db.execute(
        """
        UPDATE vector_outbox
        SET status = ?, attempts = attempts + 1, error = ?, updated_at = ?
        WHERE id = ?
        """,
        (STATUS_FAILED, str(exc), now, outbox_id),
    )


def _mark_outbox_succeeded_conn(conn: sqlite3.Connection, outbox_id: int, now: float) -> None:
    conn.execute(
        """
        UPDATE vector_outbox
        SET status = ?, error = NULL, updated_at = ?, finished_at = ?
        WHERE id = ?
        """,
        (STATUS_SUCCEEDED, now, now, outbox_id),
    )


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
