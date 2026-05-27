"""
TraceLog 拾迹 — Vector Store Layer
ChromaDB + OpenAIEmbeddingFunction 封装
"""

from dataclasses import dataclass
from typing import cast

from core import db

_collection = None
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
    global _collection
    try:
        import chromadb
        from chromadb.api.types import Embeddable, EmbeddingFunction
        from chromadb.utils.embedding_functions.openai_embedding_function import OpenAIEmbeddingFunction

        actual_api_key = embedding_api_key or api_key
        actual_base_url = _normalize_openai_base_url(embedding_base_url or base_url)

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
        raise VectorStoreInitError(str(e)) from e
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
    count = _collection.count()
    if count == 0:
        return []
    n = min(n_results, count)
    try:
        results = _collection.query(query_texts=[query], n_results=n, include=["distances"])
    except Exception:
        results = _collection.query(query_texts=[query], n_results=n)
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


def _normalize_openai_base_url(base_url: str) -> str:
    normalized = str(base_url).rstrip("/")
    if normalized in {"https://api.openai.com", "http://api.openai.com"}:
        return normalized + "/v1"
    return normalized
