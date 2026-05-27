"""
TraceLog 拾迹 — Vector Store Layer
ChromaDB + OpenAIEmbeddingFunction 封装
"""

from dataclasses import dataclass
from typing import cast

from core import db, logging_service

_collection = None
_embedding_diagnostics: dict | None = None
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


class VectorStoreInitError(RuntimeError):
    """Raised when the vector store cannot be initialized."""


def is_initialized() -> bool:
    return _collection is not None


def init_vectorstore(
    api_key: str,
    base_url: str,
    embedding_model: str,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> VectorStoreInitResult:
    """初始化 ChromaDB 向量存储。失败时抛异常，由调用层决定如何处理。"""
    global _collection, _embedding_diagnostics
    embedding_base_url_source = "embedding_base_url" if embedding_base_url else "base_url"
    embedding_api_key_source = "embedding_api_key" if embedding_api_key else "api_key"
    actual_api_key = embedding_api_key or api_key
    actual_base_url = _normalize_configured_base_url(embedding_base_url or base_url)
    diagnostics = _embedding_config_diagnostics(
        operation="vectorstore_init",
        embedding_model=embedding_model,
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
            model_name=embedding_model,
        )
        client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
        _collection = client.get_or_create_collection(
            name="posts",
            embedding_function=cast(EmbeddingFunction[Embeddable], embed_fn),
        )
    except Exception as e:
        _collection = None
        error_fields = _external_api_error_fields(e, diagnostics)
        logging_service.log_event("external_api_error", level="ERROR", **error_fields)
        raise VectorStoreInitError(_format_diagnostic_message(error_fields)) from e
    return VectorStoreInitResult(
        collection_name="posts",
        indexed_count=int(_collection.count()),
        path=CHROMA_DB_DIR,
    )


def index_post(post_id: str, content: str):
    """将帖子正文写入向量索引。"""
    if _collection is None:
        return
    body = content.strip()
    if not body:
        return
    _collection.upsert(ids=[post_id], documents=[body])


def search_relevant_posts(query: str, n_results: int = 3) -> list[str]:
    """语义检索相关帖子，返回 post_id 列表。"""
    return query_post_ids(query, n_results)


def query_post_ids(query: str, n_results: int = 20) -> list[str]:
    """语义检索相关帖子，返回按相关性排序的 post_id 列表。"""
    return [hit.post_id for hit in query_post_hits(query, n_results)]


def query_post_hits(query: str, n_results: int = 20) -> list[VectorHit]:
    """语义检索相关帖子，返回排序与可选 distance 信号。"""
    if _collection is None:
        return []
    try:
        count = _collection.count()
    except Exception as exc:
        _log_vector_query_failed(exc)
        return []
    if count == 0:
        return []
    n = min(n_results, count)
    try:
        results = _collection.query(query_texts=[query], n_results=n, include=["distances"])
    except Exception as first_exc:
        try:
            results = _collection.query(query_texts=[query], n_results=n)
        except Exception as exc:
            _log_vector_query_failed(exc, first_exception=first_exc)
            return []
    if results and results.get("ids") and len(results["ids"]) > 0:
        ids = results["ids"][0]
        distances = _first_result_list(results.get("distances"))
        hits: list[VectorHit] = []
        for index, post_id in enumerate(ids):
            distance = distances[index] if distances is not None and index < len(distances) else None
            hits.append(VectorHit(post_id=str(post_id), rank=index + 1, distance=distance))
        return hits
    return []


def _first_result_list(value) -> list[float] | None:
    if not value or len(value) == 0:
        return None
    return value[0]


def _normalize_configured_base_url(base_url: str | None) -> str:
    return str(base_url or "").strip().rstrip("/")


def _embedding_config_diagnostics(
    *,
    operation: str,
    embedding_model: str,
    embedding_base_url: str,
    embedding_base_url_source: str,
    llm_base_url: str,
    embedding_api_key_source: str,
) -> dict:
    return {
        "operation": operation,
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
