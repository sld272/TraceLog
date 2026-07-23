"""One-time atomic migration from ChromaDB embeddings to SQLite BLOBs."""

from __future__ import annotations

import argparse
import importlib
import random
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
# 直接运行 `python scripts/migrate_chroma_to_sqlite.py` 时项目根目录不在 sys.path。
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core import db, vector_index_service, vectorstore
from core.cli.config import load_config


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationSummary:
    collection_name: str
    migrated_count: int
    dim: int | None
    sampled_count: int


def migrate_chroma_to_sqlite(
    *,
    workspace: Path,
    collection_name: str,
) -> MigrationSummary:
    workspace = workspace.resolve()
    state_db = workspace / "state.db"
    chroma_dir = workspace / "chroma_db"
    if not state_db.is_file():
        raise MigrationError(f"找不到 SQLite 数据库：{state_db}")
    if not chroma_dir.is_dir():
        raise MigrationError(f"找不到 ChromaDB 目录：{chroma_dir}")

    ids, raw_vectors = _export_chroma(chroma_dir, collection_name)
    normalized = _normalize_export(ids, raw_vectors)

    old_workspace, old_db_path = db.WORKSPACE_DIR, db.DB_PATH
    db.WORKSPACE_DIR = workspace
    db.DB_PATH = state_db
    conn = sqlite3.connect(state_db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_embedding_columns(conn)
        docs = {
            str(row["doc_id"]): row
            for row in conn.execute(
                """
                SELECT doc_id, content_hash, source_revision
                FROM vector_docs
                ORDER BY doc_id
                """
            ).fetchall()
        }
        collection = conn.execute(
            """
            SELECT collection_name
            FROM vector_index_collections
            WHERE collection_name = ?
            """,
            (collection_name,),
        ).fetchone()
        if collection is None:
            raise MigrationError(f"SQLite 中不存在当前集合：{collection_name}")
        exported_ids = set(ids)
        document_ids = set(docs)
        if exported_ids != document_ids:
            missing = sorted(document_ids - exported_ids)
            stale = sorted(exported_ids - document_ids)
            raise MigrationError(
                "Chroma 与 vector_docs 文档集合不一致："
                f"缺失 {len(missing)} 条，多余 {len(stale)} 条"
            )

        conn.execute(
            "DELETE FROM vector_index_items WHERE collection_name = ?",
            (collection_name,),
        )
        now = db.now_ts()
        conn.executemany(
            """
            INSERT INTO vector_index_items(
                collection_name, doc_id, content_hash, source_revision,
                indexed_at, dim, embedding
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    collection_name,
                    doc_id,
                    str(docs[doc_id]["content_hash"]),
                    int(docs[doc_id]["source_revision"]),
                    now,
                    int(vector.size),
                    vector.tobytes(),
                )
                for doc_id, vector in zip(ids, normalized, strict=True)
            ],
        )
        _finish_replaced_outbox_rows(conn, collection_name, now)
        sampled_count = _verify_written_vectors(
            conn,
            collection_name=collection_name,
            ids=ids,
            source_vectors=normalized,
        )
        vector_index_service._refresh_collection_state_conn(conn, collection_name)
        state = conn.execute(
            """
            SELECT ready, audit_status
            FROM vector_index_collections
            WHERE collection_name = ?
            """,
            (collection_name,),
        ).fetchone()
        if state is None or not bool(int(state["ready"])) or str(state["audit_status"]) != "ready":
            raise MigrationError("迁移后的集合未达到 query-ready 状态")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
        db.WORKSPACE_DIR = old_workspace
        db.DB_PATH = old_db_path

    dim = int(normalized[0].size) if normalized else None
    return MigrationSummary(
        collection_name=collection_name,
        migrated_count=len(ids),
        dim=dim,
        sampled_count=sampled_count,
    )


def _export_chroma(chroma_dir: Path, collection_name: str) -> tuple[list[str], list[Any]]:
    try:
        chromadb = importlib.import_module("chromadb")
    except ImportError as exc:
        raise MigrationError(
            "当前环境未安装 chromadb；请在仍包含旧依赖的 tracelog 环境中运行迁移"
        ) from exc
    try:
        client = chromadb.PersistentClient(path=str(chroma_dir))
        collection = client.get_collection(name=collection_name)
        result = collection.get(include=["embeddings"])
        ids = [str(value) for value in (result.get("ids") or [])]
        embeddings = result.get("embeddings")
        vectors = [] if embeddings is None else list(embeddings)
        count = int(collection.count())
    except Exception as exc:
        raise MigrationError(f"无法读取 ChromaDB 集合 {collection_name}：{exc}") from exc
    if count != len(ids) or len(ids) != len(vectors):
        raise MigrationError(
            "ChromaDB 导出条数不一致："
            f"collection.count={count}, ids={len(ids)}, embeddings={len(vectors)}"
        )
    if len(set(ids)) != len(ids):
        raise MigrationError("ChromaDB 导出包含重复 doc_id")
    return ids, vectors


def _normalize_export(ids: list[str], vectors: list[Any]) -> list[np.ndarray]:
    normalized = [vectorstore.normalize_embedding(vector) for vector in vectors]
    dimensions = {int(vector.size) for vector in normalized}
    if len(dimensions) > 1:
        raise MigrationError(f"ChromaDB 导出向量维度不一致：{sorted(dimensions)}")
    if len(ids) != len(normalized):
        raise MigrationError("ChromaDB 导出的 doc_id 与向量无法一一对应")
    return normalized


def _ensure_embedding_columns(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(vector_index_items)").fetchall()
    }
    if not columns:
        raise MigrationError("state.db 缺少 vector_index_items 表")
    if "dim" not in columns:
        conn.execute("ALTER TABLE vector_index_items ADD COLUMN dim INTEGER")
    if "embedding" not in columns:
        conn.execute("ALTER TABLE vector_index_items ADD COLUMN embedding BLOB")


def _finish_replaced_outbox_rows(
    conn: sqlite3.Connection,
    collection_name: str,
    now: float,
) -> None:
    conn.execute(
        """
        UPDATE vector_outbox
        SET status = ?,
            error = NULL,
            updated_at = ?,
            finished_at = ?
        WHERE collection_name = ?
          AND status IN (?, ?)
          AND (
                (
                    op = 'upsert'
                    AND EXISTS (
                        SELECT 1
                        FROM vector_docs
                        WHERE vector_docs.doc_id = vector_outbox.doc_id
                          AND vector_docs.content_hash = vector_outbox.target_hash
                    )
                )
                OR (
                    op = 'delete'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM vector_docs
                        WHERE vector_docs.doc_id = vector_outbox.doc_id
                    )
                )
          )
        """,
        (
            vector_index_service.STATUS_SUCCEEDED,
            now,
            now,
            collection_name,
            vector_index_service.STATUS_PENDING,
            vector_index_service.STATUS_FAILED,
        ),
    )


def _verify_written_vectors(
    conn: sqlite3.Connection,
    *,
    collection_name: str,
    ids: list[str],
    source_vectors: list[np.ndarray],
) -> int:
    rows = conn.execute(
        """
        SELECT doc_id, dim, embedding
        FROM vector_index_items
        WHERE collection_name = ?
        ORDER BY doc_id
        """,
        (collection_name,),
    ).fetchall()
    if len(rows) != len(ids):
        raise MigrationError(
            f"SQLite 写入条数校验失败：预期 {len(ids)}，实际 {len(rows)}"
        )
    stored = {
        str(row["doc_id"]): np.frombuffer(
            bytes(row["embedding"]),
            dtype="<f4",
            count=int(row["dim"]),
        )
        for row in rows
    }
    source_by_id = dict(zip(ids, source_vectors, strict=True))
    sample_ids = random.Random(0).sample(ids, min(10, len(ids)))
    for doc_id in sample_ids:
        source = source_by_id[doc_id]
        actual = stored[doc_id]
        if actual.size != source.size:
            raise MigrationError(
                f"SQLite 维度校验失败：{doc_id} 预期 {source.size}，实际 {actual.size}"
            )
        cosine = float(source @ actual)
        if not np.isclose(cosine, 1.0, atol=1e-6):
            raise MigrationError(
                f"SQLite 余弦校验失败：{doc_id} cosine={cosine:.8f}"
            )
    return len(sample_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="migrate ChromaDB embeddings into state.db")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=db.WORKSPACE_DIR,
        help="包含 state.db 和 chroma_db/ 的工作区",
    )
    args = parser.parse_args()
    config = load_config()
    embedding_base_url = config.get("embedding_base_url") or config["base_url"]
    collection_name = vectorstore._collection_name_for_embedding_config(
        embedding_model=config["embedding_model"],
        embedding_base_url=embedding_base_url,
    )
    try:
        summary = migrate_chroma_to_sqlite(
            workspace=args.workspace,
            collection_name=collection_name,
        )
    except Exception as exc:
        raise SystemExit(f"迁移失败，state.db 未写入任何向量：{exc}") from exc
    print(
        f"迁移完成：集合 {summary.collection_name}，"
        f"{summary.migrated_count} 条，维度 {summary.dim or 0}，"
        f"抽样校验 {summary.sampled_count} 条。"
    )


if __name__ == "__main__":
    main()
