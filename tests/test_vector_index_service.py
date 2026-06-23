from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, memory_unit_service, vector_index_service


class VectorIndexServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        workspace = Path(self.tmp.name) / "workspace"
        db.WORKSPACE_DIR = workspace
        db.DB_PATH = workspace / "state.db"
        db.init_db()
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_upsert_doc_writes_ledger_and_outbox(self) -> None:
        doc = vector_index_service.build_post_doc("p-1", "今天想练歌")
        self.assertIsNotNone(doc)
        vector_index_service.upsert_doc(doc)
        stored = db.query_one(
            "SELECT * FROM vector_docs WHERE doc_id = ?", ("post-p-1",)
        )
        queued = db.query_one(
            "SELECT * FROM vector_outbox WHERE doc_id = ?", ("post-p-1",)
        )
        self.assertEqual("今天想练歌", stored["content"])
        self.assertEqual("upsert", queued["op"])

    def test_rebuild_expected_docs_tracks_sqlite_posts(self) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES ('p-1', '2026-06-22T00:00:00+08:00', '公开记录', 1, 1)
            """
        )
        self.assertEqual(1, vector_index_service.rebuild_expected_docs())
        self.assertIsNotNone(
            db.query_one("SELECT 1 FROM vector_docs WHERE doc_id = 'post-p-1'")
        )

    def test_active_memory_unit_is_an_expected_vector_doc(self) -> None:
        unit_id = memory_unit_service.add_unit(
            owner_scope="global",
            visibility_scope="public",
            source_channel="user",
            type="preference",
            content="用户喜欢安静的学习环境",
            source="user_authored",
            actor="user",
        )
        vector_index_service.rebuild_expected_docs()
        row = db.query_one(
            "SELECT * FROM vector_docs WHERE doc_id = ?", (f"unit-{unit_id}",)
        )
        self.assertIsNotNone(row)
        self.assertEqual("unit", row["doc_type"])

    def test_retracted_unit_leaves_expected_vector_docs(self) -> None:
        unit_id = memory_unit_service.add_unit(
            owner_scope="global",
            visibility_scope="public",
            source_channel="user",
            type="preference",
            content="用户喜欢安静的学习环境",
            source="user_authored",
            actor="user",
        )
        vector_index_service.rebuild_expected_docs()
        memory_unit_service.retract_unit(unit_id, by="user", reason="false")
        vector_index_service.rebuild_expected_docs()
        self.assertIsNone(
            db.query_one(
                "SELECT 1 FROM vector_docs WHERE doc_id = ?", (f"unit-{unit_id}",)
            )
        )


if __name__ == "__main__":
    unittest.main()
