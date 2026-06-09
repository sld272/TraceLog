from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, record_service, vector_index_service
from tests.helpers import require_not_none


class FakeVectorStore:
    def __init__(self, *, initialized: bool = True, error: BaseException | None = None) -> None:
        self.initialized = initialized
        self.error = error
        self.indexed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.document_records: dict[str, dict] = {}

    def is_initialized(self) -> bool:
        return self.initialized

    def current_collection_name(self) -> str:
        return "tracelog_test"

    def index_document(self, doc_id: str, content: str, metadata: dict) -> None:
        if self.error is not None:
            raise self.error
        self.indexed.append((str(metadata.get("post_id") or doc_id), content))
        self.document_records[doc_id] = dict(metadata)

    def delete_document(self, doc_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.deleted.append(doc_id)
        self.document_records.pop(doc_id, None)

    def delete_documents(self, doc_ids: list[str]) -> None:
        if self.error is not None:
            raise self.error
        for doc_id in doc_ids:
            self.delete_document(doc_id)

    def list_document_records(self) -> dict[str, dict]:
        if self.error is not None:
            raise self.error
        return {doc_id: dict(metadata) for doc_id, metadata in self.document_records.items()}


class RecordServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.fake_vectorstore = FakeVectorStore()

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )
        self.patches = [
            patch("core.vectorstore.is_initialized", self.fake_vectorstore.is_initialized),
            patch("core.vectorstore.current_collection_name", self.fake_vectorstore.current_collection_name),
            patch("core.vectorstore.index_document", self.fake_vectorstore.index_document),
            patch("core.vectorstore.delete_document", self.fake_vectorstore.delete_document),
            patch("core.vectorstore.delete_documents", self.fake_vectorstore.delete_documents),
            patch("core.vectorstore.list_document_records", self.fake_vectorstore.list_document_records),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_save_post_indexes_and_marks_collection_ready(self) -> None:
        post_id = record_service.save_post("今天想练歌")

        self.assertEqual([(post_id, "今天想练歌")], self.fake_vectorstore.indexed)
        post = require_not_none(db.query_one("SELECT content FROM posts WHERE id = ?", (post_id,)))
        vector_doc = require_not_none(db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", (f"post-{post_id}",)))
        self.assertEqual("今天想练歌", post["content"])
        self.assertEqual("今天想练歌", vector_doc["content"])
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)

    def test_save_post_keeps_outbox_failed_on_index_error(self) -> None:
        self.fake_vectorstore.error = RuntimeError("embedding failed")

        post_id = record_service.save_post("今天想练歌")
        vector_doc = require_not_none(db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", (f"post-{post_id}",)))
        outbox = require_not_none(db.query_one("SELECT * FROM vector_outbox WHERE doc_id = ?", (f"post-{post_id}",)))
        state = vector_index_service.collection_state("tracelog_test")

        self.assertEqual("post", vector_doc["doc_type"])
        self.assertEqual("今天想练歌", vector_doc["content"])
        self.assertEqual("failed", outbox["status"])
        self.assertEqual(1, state.failed_count)
        self.assertFalse(state.query_ready)
        self.assertIsNotNone(db.query_one("SELECT content FROM posts WHERE id = ?", (post_id,)))

    def test_save_post_can_defer_embedding_for_api_pipeline(self) -> None:
        post_id = record_service.save_post("今天想练歌", index_immediately=False)
        vector_doc = require_not_none(db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", (f"post-{post_id}",)))
        outbox = require_not_none(db.query_one("SELECT * FROM vector_outbox WHERE doc_id = ?", (f"post-{post_id}",)))

        self.assertEqual([], self.fake_vectorstore.indexed)
        self.assertEqual("post", vector_doc["doc_type"])
        self.assertEqual("今天想练歌", vector_doc["content"])
        self.assertEqual("pending", outbox["status"])
        self.assertFalse(vector_index_service.collection_state("tracelog_test").query_ready)

    def test_save_post_preserves_vector_doc_and_reraises_keyboard_interrupt(self) -> None:
        self.fake_vectorstore.error = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            record_service.save_post("今天想练歌")

        post = require_not_none(db.query_one("SELECT id, content FROM posts"))
        vector_doc = require_not_none(db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", (f"post-{post['id']}",)))
        self.assertEqual("post", vector_doc["doc_type"])
        self.assertEqual("今天想练歌", vector_doc["content"])

    def test_retry_pending_vector_docs_indexes_and_clears_legacy_pending(self) -> None:
        self._insert_post("p-1", "待补索引")
        self._insert_pending_vector_doc("p-1", "待补索引")

        fixed = record_service.retry_pending_vector_docs()

        self.assertEqual(1, fixed)
        self.assertEqual([("p-1", "待补索引")], self.fake_vectorstore.indexed)
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_vector_doc:post-p-1",)))
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)

    def test_retry_pending_vector_docs_keeps_outbox_failed_on_error(self) -> None:
        self.fake_vectorstore.error = RuntimeError("still failing")
        self._insert_post("p-1", "待补索引")
        self._insert_pending_vector_doc("p-1", "待补索引")

        fixed = record_service.retry_pending_vector_docs()

        self.assertEqual(0, fixed)
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_vector_doc:post-p-1",)))
        self.assertEqual(1, vector_index_service.collection_state("tracelog_test").failed_count)

    def test_retry_pending_vector_docs_migrates_legacy_pending_embedding(self) -> None:
        self._insert_post("p-1", "待补索引")
        self._insert_legacy_pending_embedding("p-1", "待补索引")

        fixed = record_service.retry_pending_vector_docs()

        self.assertEqual(1, fixed)
        self.assertEqual([("p-1", "待补索引")], self.fake_vectorstore.indexed)
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_embedding:p-1",)))

    def test_retry_pending_vector_docs_does_not_restore_missing_sqlite_post_from_legacy_meta(self) -> None:
        self._insert_pending_vector_doc("p-1", "待补索引")

        fixed = record_service.retry_pending_vector_docs()

        self.assertEqual(1, fixed)
        self.assertEqual([], self.fake_vectorstore.indexed)
        self.assertEqual(["post-p-1"], self.fake_vectorstore.deleted)
        self.assertIsNone(db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", ("post-p-1",)))
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_vector_doc:post-p-1",)))

    def test_delete_vector_doc_goes_through_outbox(self) -> None:
        post_id = record_service.save_post("今天想练歌")
        self.fake_vectorstore.indexed.clear()

        record_service.delete_post_embedding(post_id)

        self.assertEqual([f"post-{post_id}"], self.fake_vectorstore.deleted)
        self.assertIsNone(db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", (f"post-{post_id}",)))
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)

    def _insert_pending_vector_doc(self, post_id: str, content: str) -> None:
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (
                f"pending_vector_doc:post-{post_id}",
                json.dumps(
                    {
                        "doc_id": f"post-{post_id}",
                        "type": "post",
                        "source_id": post_id,
                        "content": content,
                        "metadata": {"type": "post", "post_id": post_id},
                        "error": "test",
                        "updated_at": 1.0,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    def _insert_post(self, post_id: str, content: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-06-09T00:00:00+08:00", content, 1.0, 1.0),
        )

    def _insert_legacy_pending_embedding(self, post_id: str, content: str) -> None:
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (
                f"pending_embedding:{post_id}",
                json.dumps(
                    {
                        "post_id": post_id,
                        "content": content,
                        "error": "test",
                        "created_at": 1.0,
                    },
                    ensure_ascii=False,
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
