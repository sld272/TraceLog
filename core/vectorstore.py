"""TraceLog SQLite vector storage and exact cosine retrieval."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np

from core import db, logging_service
from core.embedding_client import EmbeddingClient

VECTOR_DISTANCE_SPACE = "cosine"
BASE_DIR = str(db.BASE_DIR)

_embedding_client: EmbeddingClient | None = None
_embedding_diagnostics: dict | None = None
_collection_name: str | None = None
_embedding_config_hash: str | None = None


@dataclass(frozen=True)
class VectorStoreInitResult:
    collection_name: str
    indexed_count: int
    path: str


@dataclass(frozen=True)
class VectorHit:
    post_id: str
    rank: int
    distance: float | None


@dataclass(frozen=True)
class VectorDocHit:
    doc_id: str
    type: str
    source_id: str
    rank: int
    distance: float | None
    metadata: dict
    document: str | None = None


class VectorStoreInitError(RuntimeError):
    """Raised when the vector store cannot be initialized."""


def is_initialized() -> bool:
    return _embedding_client is not None


def current_collection_name() -> str | None:
    return _collection_name if is_initialized() else None


def current_embedding_config_hash() -> str | None:
    return _embedding_config_hash if is_initialized() else None


def indexed_count() -> int:
    collection_name = current_collection_name()
    if collection_name is None:
        return 0
    try:
        row = db.query_one(
            """
            SELECT COUNT(*) AS count
            FROM vector_index_items
            WHERE collection_name = ?
              AND dim IS NOT NULL
              AND embedding IS NOT NULL
            """,
            (collection_name,),
        )
        return int(row["count"]) if row is not None else 0
    except Exception:
        return 0


def init_vectorstore(
    api_key: str,
    base_url: str,
    embedding_model: str,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> VectorStoreInitResult:
    """Initialize the embedding client and active SQLite vector collection."""
    global _embedding_client, _embedding_diagnostics, _collection_name, _embedding_config_hash
    embedding_base_url_source = "embedding_base_url" if embedding_base_url else "base_url"
    embedding_api_key_source = "embedding_api_key" if embedding_api_key else "api_key"
    actual_api_key = embedding_api_key or api_key
    actual_embedding_model = str(embedding_model or "").strip()
    actual_base_url = _normalize_configured_base_url(embedding_base_url or base_url)
    collection_name = _collection_name_for_embedding_config(
        embedding_model=actual_embedding_model,
        embedding_base_url=actual_base_url,
    )
    embedding_config_hash = _embedding_config_fingerprint(
        embedding_model=actual_embedding_model,
        embedding_base_url=actual_base_url,
    )
    diagnostics = _embedding_config_diagnostics(
        operation="vectorstore_init",
        collection_name=collection_name,
        embedding_config_hash=embedding_config_hash,
        embedding_model=actual_embedding_model,
        embedding_base_url=actual_base_url,
        embedding_base_url_source=embedding_base_url_source,
        llm_base_url=_normalize_configured_base_url(base_url),
        embedding_api_key_source=embedding_api_key_source,
    )
    _embedding_diagnostics = diagnostics
    try:
        # config.json 中 api_key/base_url/embedding_model 任一为 "" 时会触发这些初始化错误。
        if not actual_api_key:
            raise ValueError("embedding API key is empty")
        if not actual_base_url:
            raise ValueError("embedding base URL is empty")
        if not actual_embedding_model:
            raise ValueError("embedding model is empty")
        client = EmbeddingClient(
            api_key=actual_api_key,
            base_url=actual_base_url,
            model=actual_embedding_model,
        )
        from core import vector_index_service

        vector_index_service.ensure_collection(
            collection_name=collection_name,
            embedding_config_hash=embedding_config_hash,
            embedding_model=actual_embedding_model,
            embedding_base_url=actual_base_url,
        )
    except Exception as exc:
        _embedding_client = None
        _collection_name = None
        _embedding_config_hash = None
        error_fields = _external_api_error_fields(exc, diagnostics)
        logging_service.log_event("external_api_error", level="ERROR", **error_fields)
        raise VectorStoreInitError(_format_diagnostic_message(error_fields)) from exc
    _embedding_client = client
    _collection_name = collection_name
    _embedding_config_hash = embedding_config_hash
    return VectorStoreInitResult(
        collection_name=collection_name,
        indexed_count=indexed_count(),
        path=str(db.DB_PATH),
    )


def embed_texts(texts: list[str]) -> list[np.ndarray]:
    client = _embedding_client
    if client is None:
        raise RuntimeError("vector store is not initialized")
    return client.embed_texts(texts)


def normalize_embedding(vector: Any) -> np.ndarray:
    normalized = np.asarray(vector, dtype="<f4").reshape(-1)
    # OpenAI-compatible endpoint 返回 [] 或 [0, 0, ...] 时无法参与余弦检索。
    if normalized.size == 0:
        raise ValueError("embedding vector is empty")
    norm = float(np.linalg.norm(normalized))
    if not np.isfinite(norm) or norm == 0.0:
        raise ValueError("embedding vector has invalid L2 norm")
    normalized = normalized / np.float32(norm)
    return np.asarray(normalized, dtype="<f4")


def serialize_embedding(vector: Any) -> tuple[int, bytes]:
    normalized = normalize_embedding(vector)
    return int(normalized.size), normalized.tobytes()


def index_post(post_id: str, content: str) -> None:
    from core import vector_index_service

    doc = vector_index_service.build_post_doc(post_id, content)
    if doc is not None:
        vector_index_service.upsert_doc(doc)
        vector_index_service.process_outbox()


def index_post_vision(post_id: str, content: str, attachment_ids: list[str]) -> None:
    from core import vector_index_service

    doc = vector_index_service.build_post_vision_doc(post_id, content, attachment_ids)
    if doc is not None:
        vector_index_service.upsert_doc(doc)
        vector_index_service.process_outbox()


def index_comment(comment_id: int, post_id: str, soul_name: str, role: str, seq: int, content: str) -> None:
    from core import vector_index_service

    doc = vector_index_service.build_comment_doc(comment_id, post_id, soul_name, role, seq, content)
    if doc is not None:
        vector_index_service.upsert_doc(doc)
        vector_index_service.process_outbox()


def index_chat_message(message_id: int, thread_id: int, soul_name: str, role: str, content: str) -> None:
    from core import vector_index_service

    doc = vector_index_service.build_chat_doc(message_id, thread_id, soul_name, role, content)
    if doc is not None:
        vector_index_service.upsert_doc(doc)
        vector_index_service.process_outbox()


def delete_document(doc_id: str) -> None:
    delete_documents([doc_id])


def delete_documents(doc_ids: list[str]) -> None:
    collection_name = current_collection_name()
    if collection_name is None:
        return
    ids = [doc_id for doc_id in doc_ids if isinstance(doc_id, str) and doc_id.strip()]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    db.execute(
        f"""
        DELETE FROM vector_index_items
        WHERE collection_name = ?
          AND doc_id IN ({placeholders})
        """,
        (collection_name, *ids),
    )


def list_document_records() -> dict[str, dict]:
    collection_name = current_collection_name()
    if collection_name is None:
        return {}
    rows = db.query_all(
        """
        SELECT
            vector_index_items.doc_id,
            vector_index_items.content_hash,
            vector_index_items.source_revision,
            vector_docs.metadata_json
        FROM vector_index_items
        JOIN vector_docs ON vector_docs.doc_id = vector_index_items.doc_id
        WHERE vector_index_items.collection_name = ?
          AND vector_index_items.dim IS NOT NULL
          AND vector_index_items.embedding IS NOT NULL
        ORDER BY vector_index_items.doc_id
        """,
        (collection_name,),
    )
    records: dict[str, dict] = {}
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        metadata.update(
            {
                "content_hash": str(row["content_hash"]),
                "source_revision": int(row["source_revision"]),
            }
        )
        records[str(row["doc_id"])] = metadata
    return records


def query_post_hits(query: str, n_results: int = 20) -> list[VectorHit]:
    """语义检索相关帖子，返回排序与可选 distance 信号。"""
    hits = query_documents(
        query,
        n_results=n_results,
        where={"$or": [{"type": {"$eq": "post"}}, {"type": {"$eq": "post_vision"}}]},
    )
    return [
        VectorHit(
            post_id=str(hit.metadata.get("post_id") or hit.source_id),
            rank=hit.rank,
            distance=hit.distance,
        )
        for hit in hits
        if hit.type in {"post", "post_vision"}
    ]


def query_documents(
    query: str,
    n_results: int = 20,
    where: dict | None = None,
) -> list[VectorDocHit]:
    """Semantic search over all TraceLog vector documents."""
    collection_name = current_collection_name()
    if collection_name is None:
        return []
    try:
        from core import vector_index_service

        if not vector_index_service.is_current_collection_query_ready():
            logging_service.log_event(
                "vector_query_skipped",
                level="INFO",
                reason="vector_index_not_ready",
                collection_name=collection_name,
            )
            return []
    except Exception:
        return []
    try:
        query_vectors = embed_texts([query])
        # 单条 query 若被 provider 返回 0 条或 2 条 embedding，就无法执行检索。
        if len(query_vectors) != 1:
            raise RuntimeError(
                f"embedding response count mismatch: expected 1, got {len(query_vectors)}"
            )
        query_vector = normalize_embedding(query_vectors[0])
        rows = _candidate_rows(collection_name, where)
        if not rows or n_results <= 0:
            return []
        matrix = np.vstack([_embedding_from_row(row) for row in rows])
        if matrix.shape[1] != query_vector.size:
            raise ValueError(
                f"query embedding dimension mismatch: index {matrix.shape[1]}, query {query_vector.size}"
            )
        similarities = matrix @ query_vector
        top_count = min(int(n_results), len(rows))
        candidate_indexes = np.argpartition(-similarities, top_count - 1)[:top_count]
        cutoff = min(float(similarities[index]) for index in candidate_indexes)
        candidate_indexes = np.flatnonzero(similarities >= cutoff)
        ordered_indexes = sorted(
            (int(index) for index in candidate_indexes),
            key=lambda index: (-float(similarities[index]), str(rows[index]["doc_id"])),
        )[:top_count]
        return [
            _hit_from_row(
                rows[index],
                rank=rank,
                distance=1.0 - float(similarities[index]),
            )
            for rank, index in enumerate(ordered_indexes, start=1)
        ]
    except Exception as exc:
        _log_vector_query_failed(exc)
        return []


def _candidate_rows(collection_name: str, where: dict | None) -> list:
    doc_types = _doc_types_for_where(where)
    sql = """
        SELECT
            vector_index_items.doc_id,
            vector_index_items.dim,
            vector_index_items.embedding,
            vector_docs.doc_type,
            vector_docs.source_id,
            vector_docs.content,
            vector_docs.metadata_json
        FROM vector_index_items
        JOIN vector_docs ON vector_docs.doc_id = vector_index_items.doc_id
        WHERE vector_index_items.collection_name = ?
          AND vector_index_items.dim IS NOT NULL
          AND vector_index_items.embedding IS NOT NULL
    """
    params: list[Any] = [collection_name]
    if doc_types is not None:
        placeholders = ",".join("?" for _ in doc_types)
        sql += f" AND vector_docs.doc_type IN ({placeholders})"
        params.extend(doc_types)
    sql += " ORDER BY vector_index_items.doc_id"
    return db.query_all(sql, params)


def _doc_types_for_where(where: dict | None) -> list[str] | None:
    if where is None:
        return None
    if "$or" in where:
        values: set[str] = set()
        for item in where["$or"]:
            item_values = _doc_types_for_where(item)
            if item_values:
                values.update(item_values)
        return sorted(values)
    type_filter = where["type"]
    if isinstance(type_filter, str):
        return [type_filter]
    if "$eq" in type_filter:
        return [str(type_filter["$eq"])]
    return sorted(str(value) for value in type_filter["$in"])


def _embedding_from_row(row) -> np.ndarray:
    dim = int(row["dim"])
    blob = bytes(row["embedding"])
    # 例如 dim=3 但 BLOB 只有 8 字节，说明 state.db 中该向量行已损坏。
    if len(blob) != dim * np.dtype("<f4").itemsize:
        raise ValueError(
            f"invalid embedding BLOB for {row['doc_id']}: expected {dim * 4} bytes, got {len(blob)}"
        )
    return np.frombuffer(blob, dtype="<f4", count=dim)


def _hit_from_row(row, *, rank: int, distance: float) -> VectorDocHit:
    metadata = json.loads(row["metadata_json"])
    return VectorDocHit(
        doc_id=str(row["doc_id"]),
        type=str(row["doc_type"]),
        source_id=str(row["source_id"]),
        rank=rank,
        distance=distance,
        metadata=metadata,
        document=str(row["content"]),
    )


def _normalize_configured_base_url(base_url: str | None) -> str:
    return str(base_url or "").strip().rstrip("/")


def _collection_name_for_embedding_config(*, embedding_model: str, embedding_base_url: str) -> str:
    fingerprint = _embedding_config_fingerprint(
        embedding_model=embedding_model,
        embedding_base_url=embedding_base_url,
    )
    return f"tracelog_{fingerprint[:12]}"


def _embedding_config_fingerprint(*, embedding_model: str, embedding_base_url: str) -> str:
    payload = {
        "embedding_function": "openai",
        "space": VECTOR_DISTANCE_SPACE,
        "embedding_model": str(embedding_model or "").strip(),
        "embedding_base_url": _normalize_configured_base_url(embedding_base_url),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _embedding_config_diagnostics(
    *,
    operation: str,
    collection_name: str,
    embedding_config_hash: str,
    embedding_model: str,
    embedding_base_url: str,
    embedding_base_url_source: str,
    llm_base_url: str,
    embedding_api_key_source: str,
) -> dict:
    return {
        "operation": operation,
        "collection_name": collection_name,
        "embedding_config_hash": embedding_config_hash,
        "embedding_model": embedding_model,
        "embedding_base_url": embedding_base_url,
        "embedding_base_url_source": embedding_base_url_source,
        "llm_base_url": llm_base_url,
        "embedding_api_key_source": embedding_api_key_source,
    }


def _external_api_error_fields(exc: Exception, diagnostics: dict | None = None) -> dict:
    fields = dict(diagnostics or {})
    fields.update(
        {
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
    )
    suggestions = _diagnostic_suggestions(fields)
    if suggestions:
        fields["suggestions"] = suggestions
    return fields


def _diagnostic_suggestions(fields: dict) -> list[str]:
    suggestions: list[str] = []
    message = str(fields.get("exception_message") or "").lower()
    exception_type = str(fields.get("exception_type") or "").lower()
    if fields.get("embedding_base_url_source") == "base_url":
        suggestions.append(
            "如果主 LLM provider 不支持 embeddings，请在 config.json 配置 embedding_base_url 和必要的 embedding_api_key。"
        )
    base_url = str(fields.get("embedding_base_url") or "")
    if base_url and "/v1" not in base_url.rstrip("/"):
        suggestions.append(
            "部分 OpenAI-compatible embeddings endpoint 需要 base_url 以 /v1 结尾，请按 provider 文档确认。"
        )
    auth_tokens = ("auth", "unauthorized", "forbidden", "401", "403", "api key")
    if any(token in f"{exception_type} {message}" for token in auth_tokens):
        suggestions.append("请检查 embedding 使用的 API key 来源是否正确。")
    return suggestions


def _format_diagnostic_message(fields: dict) -> str:
    parts = [
        f"{fields.get('operation', 'external_api')} failed",
        f"collection_name={fields.get('collection_name')}",
        f"embedding_config_hash={fields.get('embedding_config_hash')}",
        f"embedding_model={fields.get('embedding_model')}",
        f"embedding_base_url={fields.get('embedding_base_url')}",
        f"embedding_base_url_source={fields.get('embedding_base_url_source')}",
        f"llm_base_url={fields.get('llm_base_url')}",
        f"embedding_api_key_source={fields.get('embedding_api_key_source')}",
        f"{fields.get('exception_type')}: {fields.get('exception_message')}",
    ]
    suggestions = fields.get("suggestions")
    if suggestions:
        parts.append("建议：" + " ".join(str(item) for item in suggestions))
    return " | ".join(part for part in parts if part)


def _log_vector_query_failed(exc: Exception, *, first_exception: Exception | None = None) -> None:
    diagnostics = dict(_embedding_diagnostics or {})
    diagnostics["operation"] = "vector_query_posts"
    fields = _external_api_error_fields(exc, diagnostics)
    if first_exception is not None:
        fields["first_exception_type"] = type(first_exception).__name__
        fields["first_exception_message"] = str(first_exception)
    logging_service.log_event("vector_query_failed", level="WARNING", **fields)
