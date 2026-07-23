from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from core import db, vector_index_service, vectorstore
from scripts import migrate_chroma_to_sqlite, vector_ab_compare


class VectorMigrationScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        (self.workspace / "chroma_db").mkdir()
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

    def test_migration_writes_normalized_blobs_and_refreshes_ready_state(self) -> None:
        post = vector_index_service.build_post_doc("p-1", "公开记录")
        unit = vector_index_service.build_unit_doc(
            "u-1",
            "喜欢安静",
            "global",
            "public",
            "preference",
        )
        self.assertIsNotNone(post)
        self.assertIsNotNone(unit)
        vector_index_service.upsert_doc(post)
        vector_index_service.upsert_doc(unit)
        fake_chroma = self._fake_chroma(
            ids=["post-p-1", "unit-u-1"],
            vectors=[[3.0, 4.0], [0.0, 2.0]],
        )

        with patch.dict(sys.modules, {"chromadb": fake_chroma}):
            summary = migrate_chroma_to_sqlite.migrate_chroma_to_sqlite(
                workspace=self.workspace,
                collection_name="tracelog_test",
            )

        self.assertEqual(2, summary.migrated_count)
        self.assertEqual(2, summary.dim)
        self.assertEqual(2, summary.sampled_count)
        rows = db.query_all(
            """
            SELECT doc_id, dim, embedding
            FROM vector_index_items
            WHERE collection_name = ?
            ORDER BY doc_id
            """,
            ("tracelog_test",),
        )
        self.assertEqual(["post-p-1", "unit-u-1"], [row["doc_id"] for row in rows])
        self.assertTrue(all(row["dim"] == 2 for row in rows))
        np.testing.assert_allclose(
            np.asarray([0.6, 0.8], dtype=np.float32),
            np.frombuffer(rows[0]["embedding"], dtype="<f4"),
        )
        np.testing.assert_allclose(
            np.asarray([0.0, 1.0], dtype=np.float32),
            np.frombuffer(rows[1]["embedding"], dtype="<f4"),
        )
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)
        statuses = {
            row["status"]
            for row in db.query_all(
                "SELECT status FROM vector_outbox WHERE collection_name = ?",
                ("tracelog_test",),
            )
        }
        self.assertEqual({"succeeded"}, statuses)

    def test_migration_dimension_failure_leaves_vector_items_unchanged(self) -> None:
        post = vector_index_service.build_post_doc("p-1", "公开记录")
        unit = vector_index_service.build_unit_doc(
            "u-1",
            "喜欢安静",
            "global",
            "public",
            "preference",
        )
        self.assertIsNotNone(post)
        self.assertIsNotNone(unit)
        vector_index_service.upsert_doc(post)
        vector_index_service.upsert_doc(unit)
        fake_chroma = self._fake_chroma(
            ids=["post-p-1", "unit-u-1"],
            vectors=[[1.0, 0.0], [1.0, 0.0, 0.0]],
        )

        with patch.dict(sys.modules, {"chromadb": fake_chroma}):
            with self.assertRaises(migrate_chroma_to_sqlite.MigrationError):
                migrate_chroma_to_sqlite.migrate_chroma_to_sqlite(
                    workspace=self.workspace,
                    collection_name="tracelog_test",
                )

        self.assertEqual(
            [],
            db.query_all(
                "SELECT doc_id FROM vector_index_items WHERE collection_name = ?",
                ("tracelog_test",),
            ),
        )
        statuses = {
            row["status"]
            for row in db.query_all(
                "SELECT status FROM vector_outbox WHERE collection_name = ?",
                ("tracelog_test",),
            )
        }
        self.assertEqual({"pending"}, statuses)

    def test_migration_count_failure_happens_before_sqlite_writes(self) -> None:
        post = vector_index_service.build_post_doc("p-1", "公开记录")
        self.assertIsNotNone(post)
        vector_index_service.upsert_doc(post)
        fake_chroma = self._fake_chroma(
            ids=["post-p-1"],
            vectors=[[1.0, 0.0]],
            count=2,
        )

        with patch.dict(sys.modules, {"chromadb": fake_chroma}):
            with self.assertRaises(migrate_chroma_to_sqlite.MigrationError):
                migrate_chroma_to_sqlite.migrate_chroma_to_sqlite(
                    workspace=self.workspace,
                    collection_name="tracelog_test",
                )

        self.assertEqual(
            [],
            db.query_all(
                "SELECT doc_id FROM vector_index_items WHERE collection_name = ?",
                ("tracelog_test",),
            ),
        )

    def test_migration_rolls_back_column_changes_when_sql_validation_fails(self) -> None:
        post = vector_index_service.build_post_doc("p-1", "公开记录")
        self.assertIsNotNone(post)
        vector_index_service.upsert_doc(post)
        with db.transaction() as conn:
            conn.execute("ALTER TABLE vector_index_items DROP COLUMN embedding")
            conn.execute("ALTER TABLE vector_index_items DROP COLUMN dim")
        fake_chroma = self._fake_chroma(ids=[], vectors=[])

        with patch.dict(sys.modules, {"chromadb": fake_chroma}):
            with self.assertRaises(migrate_chroma_to_sqlite.MigrationError):
                migrate_chroma_to_sqlite.migrate_chroma_to_sqlite(
                    workspace=self.workspace,
                    collection_name="tracelog_test",
                )

        columns = {
            row["name"]
            for row in db.query_all("PRAGMA table_info(vector_index_items)")
        }
        self.assertNotIn("dim", columns)
        self.assertNotIn("embedding", columns)

    def _fake_chroma(
        self,
        *,
        ids: list[str],
        vectors: list[list[float]],
        count: int | None = None,
    ):
        class FakeCollection:
            def get(self, *, include):
                self.include = include
                return {"ids": ids, "embeddings": vectors}

            def count(self):
                return len(ids) if count is None else count

        class FakeClient:
            def __init__(self, *, path):
                self.path = path

            def get_collection(self, *, name):
                self.name = name
                return FakeCollection()

        return SimpleNamespace(PersistentClient=FakeClient)


class VectorAbCompareTest(unittest.TestCase):
    def test_capture_records_all_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "state.db").touch()
            queries = root / "queries.json"
            out = root / "capture.json"
            queries.write_text(
                json.dumps(
                    {
                        "queries": [
                            "焦虑",
                            {"id": "mixed", "query": "career 选择"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            post_hit = vectorstore.VectorHit("p-1", 1, 0.2)
            doc_hit = vectorstore.VectorDocHit(
                "unit-u-1",
                "unit",
                "u-1",
                1,
                0.1,
                {"type": "unit"},
                "喜欢安静",
            )
            with (
                patch(
                    "scripts.vector_ab_compare.load_config",
                    return_value={
                        "api_key": "key",
                        "base_url": "https://example.invalid/v1",
                        "embedding_model": "embedding",
                    },
                ),
                patch(
                    "scripts.vector_ab_compare.vectorstore.init_vectorstore",
                    return_value=SimpleNamespace(collection_name="tracelog_test"),
                ),
                patch(
                    "scripts.vector_ab_compare.vectorstore.query_post_hits",
                    return_value=[post_hit],
                ) as post_query,
                patch(
                    "scripts.vector_ab_compare.vectorstore.query_documents",
                    return_value=[doc_hit],
                ) as document_query,
            ):
                payload = vector_ab_compare.capture(
                    workspace=workspace,
                    queries_path=queries,
                    out_path=out,
                )

            self.assertEqual(["q01", "mixed"], [item["id"] for item in payload["queries"]])
            self.assertEqual(2, post_query.call_count)
            self.assertEqual(2 * len(vector_ab_compare.DOCUMENT_SURFACES), document_query.call_count)
            self.assertEqual(payload, json.loads(out.read_text(encoding="utf-8")))
            self.assertEqual(
                {"doc_id": "p-1", "rank": 1, "distance": 0.2},
                payload["queries"][0]["results"]["post_hits"][0],
            )

    def test_diff_reports_added_lost_and_score_drift_above_threshold(self) -> None:
        old = self._capture_payload(
            [
                {"doc_id": "same", "rank": 1, "distance": 0.1},
                {"doc_id": "lost", "rank": 2, "distance": 0.2},
                {"doc_id": "stable", "rank": 3, "distance": 0.3},
            ]
        )
        new = self._capture_payload(
            [
                {"doc_id": "same", "rank": 2, "distance": 0.1002},
                {"doc_id": "added", "rank": 1, "distance": 0.05},
                {"doc_id": "stable", "rank": 3, "distance": 0.30001},
            ]
        )

        report = vector_ab_compare.diff_captures(old, new)

        self.assertIn("新增命中：1", report)
        self.assertIn("丢失命中：1", report)
        self.assertIn("分数漂移（>0.0001）：1", report)
        self.assertIn("`same`", report)
        self.assertNotIn("分数漂移 `stable`", report)

    def _capture_payload(self, hits: list[dict]) -> dict:
        return {
            "format_version": 1,
            "collection_name": "tracelog_test",
            "queries": [
                {
                    "id": "q01",
                    "query": "焦虑",
                    "results": {"post_hits": hits},
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
