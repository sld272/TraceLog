"""
TraceLog 拾迹 — Vector Store Layer
ChromaDB + OpenAIEmbeddingFunction 封装
"""

import os
import sys

import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

_collection = None
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DB_DIR = os.path.join(BASE_DIR, "workspace", "chroma_db")


def init_vectorstore(
    api_key: str,
    base_url: str,
    embedding_model: str,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
):
    """初始化 ChromaDB 向量存储。失败时报错退出，不允许静默降级。"""
    global _collection
    try:
        actual_api_key = embedding_api_key or api_key
        actual_base_url = embedding_base_url or base_url

        embed_fn = OpenAIEmbeddingFunction(
            api_key=actual_api_key,
            api_base=actual_base_url,
            model_name=embedding_model,
        )
        client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
        _collection = client.get_or_create_collection(
            name="posts",
            embedding_function=embed_fn,
        )
        print(f"[向量存储] 初始化成功，已索引 {_collection.count()} 篇帖子。")
    except Exception as e:
        print(f"[向量存储] 初始化失败：{e}")
        sys.exit(1)


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
    if _collection is None:
        return []
    count = _collection.count()
    if count == 0:
        return []
    n = min(n_results, count)
    results = _collection.query(query_texts=[query], n_results=n)
    if results and results.get("ids") and len(results["ids"]) > 0:
        return results["ids"][0]
    return []
