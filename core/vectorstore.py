"""
TraceLog 拾迹 — Vector Store Layer
ChromaDB + OpenAIEmbeddingFunction 封装
"""

import hashlib
import json
from dataclasses import dataclass
from typing import cast

from core import db, logging_service

_collection = None
_embedding_diagnostics: dict | None = None
_collection_name: str | None = None
_embedding_config_hash: str | None = None
BASE_DIR = str(db.BASE_DIR)
CHROMA_DB_DIR = str(db.WORKSPACE_DIR / "chroma_db")


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
    return _collection is not None


def current_collection_name() -> str | None:
    return _collection_name if _collection is not None else None


def current_embedding_config_hash() -> str | None:
    return _embedding_config_hash if _collection is not None else None


def indexed_count() -> int:
    if _collection is None:
        return 0
    try:
        return int(_collection.count())
    except Exception:
        return 0


def init_vectorstore(
    api_key: str,
    base_url: str,
    embedding_model: str,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> VectorStoreInitResult:
    """初始化 ChromaDB 向量存储。失败时抛异常，由调用层决定如何处理。"""
    global _collection, _embedding_diagnostics, _collection_name, _embedding_config_hash
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
        import chromadb
        from chromadb.api.types import Embeddable, EmbeddingFunction
        from chromadb.utils.embedding_functions.openai_embedding_function import OpenAIEmbeddingFunction

        embed_fn = OpenAIEmbeddingFunction(
            api_key=actual_api_key,
            api_base=actual_base_url,
            model_name=actual_embedding_model,
        )
        client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
        _collection = client.get_or_create_collection(
            name=collection_name,
            metadata=_collection_metadata(
                embedding_model=actual_embedding_model,
                embedding_base_url=actual_base_url,
            ),
            embedding_function=cast(EmbeddingFunction[Embeddable], embed_fn),
        )
    except Exception as e:
        _collection = None
        _collection_name = None
        _embedding_config_hash = None
        error_fields = _external_api_error_fields(e, diagnostics)
        logging_service.log_event("external_api_error", level="ERROR", **error_fields)
        raise VectorStoreInitError(_format_diagnostic_message(error_fields)) from e
    _collection_name = collection_name
    _embedding_config_hash = embedding_config_hash
    return VectorStoreInitResult(
        collection_name=collection_name,
        indexed_count=int(_collection.count()),
        path=CHROMA_DB_DIR,
    )


def index_post(post_id: str, content: str):
    """将帖子正文写入向量索引。"""
    index_document(
        doc_id=f"post-{post_id}",
        content=content,
        metadata={"type": "post", "post_id": post_id},
    )


def index_post_vision(post_id: str, content: str, attachment_ids: list[str]) -> None:
    """Index visual understanding for one public post as a related retrieval doc."""
    index_document(
        doc_id=f"post-vision-{post_id}",
        content=content,
        metadata={
            "type": "post_vision",
            "post_id": post_id,
            "attachment_ids": ",".join(attachment_ids),
        },
    )


def index_comment(comment_id: int, post_id: str, soul_name: str, role: str, seq: int, content: str) -> None:
    """Index one public comment or comment follow-up message."""
    index_document(
        doc_id=f"comment-{comment_id}",
        content=content,
        metadata={
            "type": "comment",
            "comment_id": int(comment_id),
            "post_id": post_id,
            "soul_name": soul_name,
            "role": role,
            "seq": int(seq),
        },
    )


def index_chat_message(message_id: int, thread_id: int, soul_name: str, role: str, content: str) -> None:
    """Index one private chat message."""
    index_document(
        doc_id=f"chat-{message_id}",
        content=content,
        metadata={
            "type": "chat",
            "message_id": int(message_id),
            "thread_id": int(thread_id),
            "soul_name": soul_name,
            "role": role,
        },
    )


def delete_document(doc_id: str) -> None:
    """Delete one vector document if the vector store is initialized."""
    delete_documents([doc_id])


def delete_documents(doc_ids: list[str]) -> None:
    """Delete vector documents by id if the vector store is initialized."""
    if _collection is None:
        return
    ids = [doc_id for doc_id in doc_ids if isinstance(doc_id, str) and doc_id.strip()]
    if not ids:
        return
    _collection.delete(ids=ids)


def list_document_ids() -> list[str]:
    """Return document ids currently present in the active vector collection."""
    return list(list_document_records().keys())


def list_document_records() -> dict[str, dict]:
    """Return ids and metadata currently present in the active vector collection."""
    if _collection is None:
        return {}
    try:
        result = _collection.get(include=["metadatas"])
    except Exception:
        result = _collection.get()
    ids = result.get("ids") if isinstance(result, dict) else []
    metadatas = result.get("metadatas") if isinstance(result, dict) else []
    records: dict[str, dict] = {}
    for index, doc_id in enumerate(ids or []):
        if not isinstance(doc_id, str) or not doc_id.strip():
            continue
        raw_metadata = metadatas[index] if isinstance(metadatas, list) and index < len(metadatas) else None
        records[doc_id] = raw_metadata if isinstance(raw_metadata, dict) else {}
    return records


def index_document(doc_id: str, content: str, metadata: dict) -> None:
    if _collection is None:
        return
    body = content.strip()
    if not body:
        return
    _collection.upsert(ids=[doc_id], documents=[body], metadatas=[metadata])


def query_post_ids(query: str, n_results: int = 20) -> list[str]:
    """语义检索相关帖子，返回按相关性排序的 post_id 列表。"""
    return [hit.post_id for hit in query_post_hits(query, n_results)]


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
    if _collection is None:
        return []
    try:
        from core import vector_index_service

        if not vector_index_service.is_current_collection_query_ready():
            logging_service.log_event(
                "vector_query_skipped",
                level="INFO",
                reason="vector_index_not_ready",
                collection_name=current_collection_name(),
            )
            return []
    except Exception:
        return []
    try:
        count = _collection.count()
    except Exception as exc:
        _log_vector_query_failed(exc)
        return []
    if count == 0:
        return []
    n = min(n_results, count)
    query_kwargs = {"query_texts": [query], "n_results": n}
    if where:
        query_kwargs["where"] = where
    try:
        results = _collection.query(**query_kwargs, include=["distances", "metadatas", "documents"])
    except Exception as first_exc:
        try:
            results = _collection.query(**query_kwargs)
        except Exception as exc:
            _log_vector_query_failed(exc, first_exception=first_exc)
            return []
    if results and results.get("ids") and len(results["ids"]) > 0:
        ids = results["ids"][0]
        distances = _first_result_list(results.get("distances"))
        metadatas = _first_result_list_any(results.get("metadatas"))
        documents = _first_result_list_any(results.get("documents"))
        hits: list[VectorDocHit] = []
        for index, doc_id in enumerate(ids):
            distance = distances[index] if distances is not None and index < len(distances) else None
            raw_metadata = metadatas[index] if metadatas is not None and index < len(metadatas) else None
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            doc_type = str(metadata.get("type") or _infer_doc_type(str(doc_id)))
            source_id = _source_id(str(doc_id), doc_type, metadata)
            document = documents[index] if documents is not None and index < len(documents) else None
            hits.append(
                VectorDocHit(
                    doc_id=str(doc_id),
                    type=doc_type,
                    source_id=source_id,
                    rank=index + 1,
                    distance=distance,
                    metadata=metadata,
                    document=str(document) if document is not None else None,
                )
            )
        return hits
    return []


def _first_result_list(value) -> list[float] | None:
    if not value or len(value) == 0:
        return None
    return value[0]


def _first_result_list_any(value) -> list | None:
    if not value or len(value) == 0:
        return None
    return value[0]


def _infer_doc_type(doc_id: str) -> str:
    if doc_id.startswith("comment-"):
        return "comment"
    if doc_id.startswith("chat-"):
        return "chat"
    return "post"


def _source_id(doc_id: str, doc_type: str, metadata: dict) -> str:
    if doc_type in {"post", "post_vision"}:
        return str(metadata.get("post_id") or doc_id.removeprefix("post-"))
    if doc_type == "comment":
        return str(metadata.get("comment_id") or doc_id.removeprefix("comment-"))
    if doc_type == "chat":
        return str(metadata.get("message_id") or doc_id.removeprefix("chat-"))
    return doc_id


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
        "embedding_model": str(embedding_model or "").strip(),
        "embedding_base_url": _normalize_configured_base_url(embedding_base_url),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _collection_metadata(*, embedding_model: str, embedding_base_url: str) -> dict[str, str]:
    return {
        "app": "tracelog",
        "embedding_function": "openai",
        "embedding_model": str(embedding_model or "").strip(),
        "embedding_base_url": _normalize_configured_base_url(embedding_base_url),
        "embedding_config_hash": _embedding_config_fingerprint(
            embedding_model=embedding_model,
            embedding_base_url=embedding_base_url,
        ),
    }


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
