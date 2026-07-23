from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from core import db, logging_service, vector_index_service, vectorstore


class FakeEmbeddingClient:
    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.error_for: set[str] = set()
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        self.calls.append(list(texts))
        if any(text in self.error_for for text in texts):
            raise RuntimeError("embedding endpoint unavailable")
        return [
            np.asarray(self.vectors.get(text, [1.0, 0.0]), dtype=np.float32)
            for text in texts
        ]


class VectorStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        logging_service.init_logging({"enabled": True})
        self.old_embedding_client = vectorstore._embedding_client
        self.old_embedding_diagnostics = vectorstore._embedding_diagnostics
        self.old_collection_name = vectorstore._collection_name
        self.old_embedding_config_hash = vectorstore._embedding_config_hash

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        vectorstore._embedding_client = self.old_embedding_client
        vectorstore._embedding_diagnostics = self.old_embedding_diagnostics
        vectorstore._collection_name = self.old_collection_name
        vectorstore._embedding_config_hash = self.old_embedding_config_hash
        self.tmp.cleanup()

    def test_init_failure_raises_without_exiting_process(self) -> None:
        with patch("core.vectorstore.EmbeddingClient", side_effect=ImportError("openai unavailable")):
            with self.assertRaises(vectorstore.VectorStoreInitError) as raised:
                vectorstore.init_vectorstore(
                    api_key="test-key",
                    base_url="https://example.invalid/v1",
                    embedding_model="test-embedding",
                )

        self.assertFalse(vectorstore.is_initialized())
        message = str(raised.exception)
        self.assertRegex(message, r"collection_name=tracelog_[0-9a-f]{12}")
        self.assertIn("embedding_config_hash=", message)
        self.assertIn("embedding_model=test-embedding", message)
        self.assertIn("embedding_base_url=https://example.invalid/v1", message)
        self.assertIn("embedding_base_url_source=base_url", message)
        self.assertIn("ImportError: openai unavailable", message)
        self.assertIn("配置 embedding_base_url", message)
        event = self._last_log_event("external_api_error")
        self.assertEqual("vectorstore_init", event["operation"])
        self.assertEqual("ImportError", event["exception_type"])

    def test_init_uses_configured_base_url_without_adding_v1(self) -> None:
        captured: list[dict] = []

        class CapturingEmbeddingClient(FakeEmbeddingClient):
            def __init__(self, **kwargs) -> None:
                super().__init__()
                captured.append(kwargs)

        with patch("core.vectorstore.EmbeddingClient", CapturingEmbeddingClient):
            first = vectorstore.init_vectorstore(
                api_key="main-key",
                base_url="https://api.openai.com",
                embedding_model="text-embedding-3-small",
            )
            second = vectorstore.init_vectorstore(
                api_key="main-key",
                base_url="https://api.deepseek.com",
                embedding_model="text-embedding-3-small",
                embedding_base_url="https://api.openai.com/v1",
                embedding_api_key="embedding-key",
            )
            third = vectorstore.init_vectorstore(
                api_key="rotated-main-key",
                base_url="https://api.deepseek.com",
                embedding_model="text-embedding-3-small",
                embedding_base_url="https://api.openai.com/v1",
                embedding_api_key="rotated-embedding-key",
            )

        self.assertEqual("https://api.openai.com", captured[0]["base_url"])
        self.assertEqual("main-key", captured[0]["api_key"])
        self.assertEqual("https://api.openai.com/v1", captured[1]["base_url"])
        self.assertEqual("embedding-key", captured[1]["api_key"])
        self.assertEqual("rotated-embedding-key", captured[2]["api_key"])
        self.assertNotEqual(first.collection_name, second.collection_name)
        self.assertEqual(second.collection_name, third.collection_name)
        state = vector_index_service.collection_state(second.collection_name)
        self.assertEqual("text-embedding-3-small", state.embedding_model)
        self.assertEqual("https://api.openai.com/v1", state.embedding_base_url)
        self.assertEqual(str(db.DB_PATH), third.path)

    def test_collection_name_isolated_by_embedding_model_and_base_url(self) -> None:
        first = vectorstore._collection_name_for_embedding_config(
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.openai.com/v1",
        )
        same_after_trim = vectorstore._collection_name_for_embedding_config(
            embedding_model=" text-embedding-3-small ",
            embedding_base_url="https://api.openai.com/v1/",
        )
        different_model = vectorstore._collection_name_for_embedding_config(
            embedding_model="text-embedding-3-large",
            embedding_base_url="https://api.openai.com/v1",
        )
        different_base_url = vectorstore._collection_name_for_embedding_config(
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://embedding.example/v1",
        )

        self.assertRegex(first, r"^tracelog_[0-9a-f]{12}$")
        self.assertEqual(first, same_after_trim)
        self.assertNotEqual(first, different_model)
        self.assertNotEqual(first, different_base_url)

    def test_embedding_fingerprint_includes_cosine_space(self) -> None:
        old_payload = {
            "embedding_function": "openai",
            "embedding_model": "text-embedding-3-small",
            "embedding_base_url": "https://api.openai.com/v1",
        }
        old_fingerprint = vectorstore.hashlib.sha256(
            json.dumps(old_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        new_fingerprint = vectorstore._embedding_config_fingerprint(
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://api.openai.com/v1",
        )

        self.assertNotEqual(old_fingerprint, new_fingerprint)

    def test_outbox_stores_normalized_little_endian_float32_blob(self) -> None:
        client = self._activate({"今天想练歌": [3.0, 4.0]})
        doc = vector_index_service.build_post_doc("p-1", "今天想练歌")
        self.assertIsNotNone(doc)
        vector_index_service.upsert_doc(doc)

        self.assertEqual(1, vector_index_service.process_outbox())

        item = db.query_one(
            """
            SELECT dim, embedding
            FROM vector_index_items
            WHERE collection_name = ? AND doc_id = ?
            """,
            ("tracelog_test", "post-p-1"),
        )
        self.assertIsNotNone(item)
        self.assertEqual(2, item["dim"])
        stored = np.frombuffer(item["embedding"], dtype="<f4")
        np.testing.assert_allclose(np.asarray([0.6, 0.8], dtype=np.float32), stored)
        self.assertEqual([["今天想练歌"]], client.calls)
        self.assertTrue(vector_index_service.collection_state("tracelog_test").query_ready)

    def test_outbox_rejects_dimension_change_within_collection(self) -> None:
        self._activate({"二维": [1.0, 0.0], "三维": [1.0, 0.0, 0.0]})
        first = vector_index_service.build_post_doc("p-1", "二维")
        second = vector_index_service.build_post_doc("p-2", "三维")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        vector_index_service.upsert_doc(first)
        self.assertEqual(1, vector_index_service.process_outbox())
        vector_index_service.upsert_doc(second)

        self.assertEqual(0, vector_index_service.process_outbox())

        failed = db.query_one(
            "SELECT status, error FROM vector_outbox WHERE doc_id = ?",
            ("post-p-2",),
        )
        self.assertEqual("failed", failed["status"])
        self.assertIn("dimension mismatch", failed["error"])
        self.assertEqual(1, vectorstore.indexed_count())

    def test_query_post_hits_returns_exact_cosine_ranks_and_distances(self) -> None:
        self._activate(
            {
                "焦虑": [1.0, 0.0],
                "很相关": [1.0, 0.0],
                "较相关": [0.8, 0.6],
                "无关": [0.0, 1.0],
            }
        )
        self._index_docs(
            vector_index_service.build_post_doc("p-1", "很相关"),
            vector_index_service.build_post_vision_doc("p-2", "较相关", ["a-1"]),
            vector_index_service.build_post_doc("p-3", "无关"),
        )

        hits = vectorstore.query_post_hits("焦虑", n_results=2)

        self.assertEqual(["p-1", "p-2"], [hit.post_id for hit in hits])
        self.assertEqual([1, 2], [hit.rank for hit in hits])
        self.assertAlmostEqual(0.0, hits[0].distance)
        self.assertAlmostEqual(0.2, hits[1].distance, places=6)

    def test_query_documents_translates_existing_type_filters_to_sql(self) -> None:
        self._activate(
            {
                "偏好": [1.0, 0.0],
                "unit": [1.0, 0.0],
                "chat": [0.9, 0.1],
                "post": [0.8, 0.2],
                "tombstone": [0.7, 0.3],
            }
        )
        self._index_docs(
            vector_index_service.build_unit_doc("u-1", "unit", "global", "public", "preference"),
            vector_index_service.build_chat_doc(1, 1, "拾迹者", "user", "chat"),
            vector_index_service.build_post_doc("p-1", "post"),
            vector_index_service.build_tombstone_doc("u-2", "tombstone", "global", "public", "false"),
        )

        unit_hits = vectorstore.query_documents("偏好", where={"type": "unit"})
        evidence_hits = vectorstore.query_documents(
            "偏好",
            where={"type": {"$in": ["post", "chat"]}},
        )

        self.assertEqual(["unit-u-1"], [hit.doc_id for hit in unit_hits])
        self.assertEqual(["chat-1", "post-p-1"], [hit.doc_id for hit in evidence_hits])
        self.assertEqual("unit", unit_hits[0].type)
        self.assertEqual("u-1", unit_hits[0].source_id)
        self.assertEqual("unit", unit_hits[0].document)

    def test_query_documents_skips_vector_search_when_collection_not_ready(self) -> None:
        client = self._activate({"焦虑": [1.0, 0.0]})

        with patch("core.vector_index_service.is_current_collection_query_ready", return_value=False):
            hits = vectorstore.query_documents("焦虑", n_results=20)

        self.assertEqual([], hits)
        self.assertEqual([], client.calls)

    def test_query_post_hits_logs_when_embedding_query_fails(self) -> None:
        client = self._activate({"帖子": [1.0, 0.0]})
        self._index_docs(vector_index_service.build_post_doc("p-1", "帖子"))
        client.error_for.add("焦虑")
        vectorstore._embedding_diagnostics = {
            "embedding_model": "text-embedding-3-small",
            "embedding_base_url": "https://api.deepseek.com",
            "embedding_base_url_source": "base_url",
        }

        hits = vectorstore.query_post_hits("焦虑", n_results=20)
        event = self._last_log_event("vector_query_failed")

        self.assertEqual([], hits)
        self.assertEqual("vector_query_posts", event["operation"])
        self.assertEqual("RuntimeError", event["exception_type"])
        self.assertEqual("embedding endpoint unavailable", event["exception_message"])
        self.assertIn("配置 embedding_base_url", " ".join(event["suggestions"]))

    def _activate(self, vectors: dict[str, list[float]]) -> FakeEmbeddingClient:
        client = FakeEmbeddingClient(vectors)
        vectorstore._embedding_client = client
        vectorstore._collection_name = "tracelog_test"
        vectorstore._embedding_config_hash = "hash"
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )
        return client

    def _index_docs(self, *docs) -> None:
        for doc in docs:
            self.assertIsNotNone(doc)
            vector_index_service.upsert_doc(doc)
        self.assertEqual(len(docs), vector_index_service.process_outbox())

    def _last_log_event(self, event_name: str) -> dict:
        log_path = self.workspace / "logs" / "current.jsonl"
        records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matches = [record for record in records if record.get("event") == event_name]
        self.assertTrue(matches)
        return matches[-1]


if __name__ == "__main__":
    unittest.main()
