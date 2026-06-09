from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

from core import db, vector_index_service


class FakeVectorStore:
    def __init__(
        self,
        *,
        initialized: bool = True,
        error: BaseException | None = None,
        document_ids: list[str] | None = None,
    ) -> None:
        self.initialized = initialized
        self.error = error
        self.upserted: list[tuple[str, str, dict]] = []
        self.deleted: list[str] = []
        self.document_ids = list(document_ids or [])
        self.document_records: dict[str, dict] = {doc_id: {} for doc_id in self.document_ids}
        self.collection_name = "tracelog_test"

    def is_initialized(self) -> bool:
        return self.initialized

    def current_collection_name(self) -> str:
        return self.collection_name

    def index_document(self, doc_id: str, content: str, metadata: dict) -> None:
        if self.error is not None:
            raise self.error
        self.upserted.append((doc_id, content, metadata))
        if doc_id not in self.document_ids:
            self.document_ids.append(doc_id)
        self.document_records[doc_id] = dict(metadata)

    def delete_document(self, doc_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.deleted.append(doc_id)
        if doc_id in self.document_ids:
            self.document_ids.remove(doc_id)
        self.document_records.pop(doc_id, None)

    def delete_documents(self, doc_ids: list[str]) -> None:
        if self.error is not None:
            raise self.error
        for doc_id in doc_ids:
            self.delete_document(doc_id)

    def list_document_ids(self) -> list[str]:
        if self.error is not None:
            raise self.error
        return list(self.document_ids)

    def list_document_records(self) -> dict[str, dict]:
        if self.error is not None:
            raise self.error
        return {doc_id: dict(metadata) for doc_id, metadata in self.document_records.items()}


class VectorIndexServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_upsert_doc_is_idempotent_until_content_changes(self) -> None:
        doc = vector_index_service.build_post_doc("p-1", "内容")
        assert doc is not None

        first = vector_index_service.upsert_doc(doc)
        second = vector_index_service.upsert_doc(doc)
        updated_doc = vector_index_service.build_post_doc("p-1", "新内容")
        assert updated_doc is not None
        third = vector_index_service.upsert_doc(updated_doc)

        self.assertEqual(1, first)
        self.assertEqual(1, second)
        self.assertEqual(2, third)
        self.assertEqual(2, vector_index_service.current_source_revision())

    def test_ensure_collection_and_process_outbox_marks_ready(self) -> None:
        doc = vector_index_service.build_post_doc("p-1", "内容")
        assert doc is not None
        vector_index_service.upsert_doc(doc)
        state = vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )

        self.assertFalse(state.query_ready)
        with self._fake_vectorstore(FakeVectorStore()) as fake:
            processed = vector_index_service.process_outbox("tracelog_test")
        state = vector_index_service.collection_state("tracelog_test")

        self.assertEqual(1, processed)
        self.assertEqual("post-p-1", fake.upserted[0][0])
        self.assertEqual(doc.content_hash, fake.upserted[0][2]["content_hash"])
        self.assertTrue(state.query_ready)
        item = db.query_one("SELECT content_hash FROM vector_index_items WHERE collection_name = ? AND doc_id = ?", ("tracelog_test", "post-p-1"))
        self.assertIsNotNone(item)

    def test_failed_outbox_keeps_collection_not_ready(self) -> None:
        doc = vector_index_service.build_post_doc("p-1", "内容")
        assert doc is not None
        vector_index_service.upsert_doc(doc)
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )

        with self._fake_vectorstore(FakeVectorStore(error=RuntimeError("down"))):
            processed = vector_index_service.process_outbox("tracelog_test")
        state = vector_index_service.collection_state("tracelog_test")

        self.assertEqual(0, processed)
        self.assertFalse(state.query_ready)
        self.assertEqual(1, state.failed_count)

    def test_collection_audit_failure_keeps_query_not_ready(self) -> None:
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )
        fake = FakeVectorStore(error=RuntimeError("audit down"))

        with self._fake_vectorstore(fake):
            vector_index_service.process_outbox("tracelog_test")

        state = vector_index_service.collection_state("tracelog_test")
        self.assertFalse(state.query_ready)
        self.assertEqual(vector_index_service.AUDIT_FAILED, state.audit_status)

    def test_delete_doc_creates_delete_outbox_and_removes_index_item(self) -> None:
        doc = vector_index_service.build_post_doc("p-1", "内容")
        assert doc is not None
        vector_index_service.upsert_doc(doc)
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )
        with self._fake_vectorstore(FakeVectorStore()) as fake:
            vector_index_service.process_outbox("tracelog_test")
            vector_index_service.delete_doc("post-p-1")
            processed = vector_index_service.process_outbox("tracelog_test")

        self.assertEqual(1, processed)
        self.assertEqual(["post-p-1"], fake.deleted)
        self.assertIsNone(db.query_one("SELECT * FROM vector_index_items WHERE doc_id = ?", ("post-p-1",)))
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)

    def test_switching_back_to_old_collection_detects_stale_sqlite_revision(self) -> None:
        first_doc = vector_index_service.build_post_doc("p-1", "旧内容")
        assert first_doc is not None
        vector_index_service.upsert_doc(first_doc)
        vector_index_service.ensure_collection(
            collection_name="collection_a",
            embedding_config_hash="hash-a",
            embedding_model="embedding-a",
            embedding_base_url="https://example.invalid/a",
        )
        fake_a = FakeVectorStore()
        fake_a.collection_name = "collection_a"
        with self._fake_vectorstore(fake_a):
            vector_index_service.process_outbox("collection_a")
        self.assertTrue(vector_index_service.collection_state("collection_a").query_ready)

        second_doc = vector_index_service.build_post_doc("p-1", "新内容")
        assert second_doc is not None
        vector_index_service.upsert_doc(second_doc)
        vector_index_service.ensure_collection(
            collection_name="collection_b",
            embedding_config_hash="hash-b",
            embedding_model="embedding-b",
            embedding_base_url="https://example.invalid/b",
        )
        fake_b = FakeVectorStore()
        fake_b.collection_name = "collection_b"
        with self._fake_vectorstore(fake_b):
            vector_index_service.process_outbox("collection_b")

        state_before = vector_index_service.ensure_collection(
            collection_name="collection_a",
            embedding_config_hash="hash-a",
            embedding_model="embedding-a",
            embedding_base_url="https://example.invalid/a",
        )
        pending = db.query_all(
            """
            SELECT *
            FROM vector_outbox
            WHERE collection_name = ? AND doc_id = ? AND status = ?
            """,
            ("collection_a", "post-p-1", "pending"),
        )

        self.assertFalse(state_before.query_ready)
        self.assertEqual(1, len(pending))

        with self._fake_vectorstore(fake_a):
            vector_index_service.process_outbox("collection_a")
        self.assertTrue(vector_index_service.collection_state("collection_a").query_ready)
        self.assertEqual("新内容", fake_a.upserted[-1][1])

    def test_process_outbox_audits_and_deletes_orphan_collection_ids(self) -> None:
        doc = vector_index_service.build_post_doc("p-1", "内容")
        assert doc is not None
        vector_index_service.upsert_doc(doc)
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )
        fake = FakeVectorStore(document_ids=["orphan-doc"])

        with self._fake_vectorstore(fake):
            vector_index_service.process_outbox("tracelog_test")

        self.assertIn("orphan-doc", fake.deleted)
        self.assertIsNotNone(db.query_one("SELECT * FROM vector_doc_tombstones WHERE doc_id = ?", ("orphan-doc",)))
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)

    def test_process_outbox_audits_stale_collection_metadata(self) -> None:
        doc = vector_index_service.build_post_doc("p-1", "内容")
        assert doc is not None
        vector_index_service.upsert_doc(doc)
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )
        fake = FakeVectorStore()

        with self._fake_vectorstore(fake):
            vector_index_service.process_outbox("tracelog_test")
            self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)
            fake.document_records["post-p-1"] = {"content_hash": "old", "source_revision": 0}
            processed = vector_index_service.process_outbox("tracelog_test")

        state = vector_index_service.collection_state("tracelog_test")
        pending = db.query_one(
            "SELECT * FROM vector_outbox WHERE collection_name = ? AND doc_id = ? AND status = ?",
            ("tracelog_test", "post-p-1", "pending"),
        )

        self.assertEqual(0, processed)
        self.assertFalse(state.query_ready)
        self.assertIsNotNone(pending)

        with self._fake_vectorstore(fake):
            fixed = vector_index_service.process_outbox("tracelog_test")

        self.assertEqual(1, fixed)
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)
        self.assertEqual(doc.content_hash, fake.document_records["post-p-1"]["content_hash"])

    def test_migrate_legacy_pending_vector_doc_to_outbox(self) -> None:
        db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            (
                "pending_vector_doc:post-p-1",
                json.dumps(
                    {
                        "content": "旧内容",
                        "metadata": {"type": "post", "post_id": "p-1"},
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        migrated = vector_index_service.migrate_legacy_pending_vector_docs()

        self.assertEqual(1, migrated)
        self.assertIsNotNone(db.query_one("SELECT * FROM vector_docs WHERE doc_id = ?", ("post-p-1",)))
        self.assertIsNone(db.query_one("SELECT * FROM meta WHERE key = ?", ("pending_vector_doc:post-p-1",)))

    def _fake_vectorstore(self, fake: FakeVectorStore):
        @contextmanager
        def manager():
            with (
                patch("core.vectorstore.is_initialized", fake.is_initialized),
                patch("core.vectorstore.current_collection_name", fake.current_collection_name),
                patch("core.vectorstore.index_document", fake.index_document),
                patch("core.vectorstore.delete_document", fake.delete_document),
                patch("core.vectorstore.delete_documents", fake.delete_documents),
                patch("core.vectorstore.list_document_ids", fake.list_document_ids),
                patch("core.vectorstore.list_document_records", fake.list_document_records),
            ):
                yield fake

        return manager()


if __name__ == "__main__":
    unittest.main()
