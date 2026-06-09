from __future__ import annotations

import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, logging_service, vectorstore


class FakeCollection:
    def __init__(self, results, *, fail_with_include: bool = False) -> None:
        self.results = results
        self.fail_with_include = fail_with_include

    def count(self) -> int:
        return len(self.results.get("ids", [[]])[0])

    def query(self, **kwargs):
        if self.fail_with_include and "include" in kwargs:
            raise RuntimeError("include unsupported")
        return self.results


class VectorStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        db.WORKSPACE_DIR = self.workspace
        logging_service.init_logging({"enabled": True})
        self.old_collection = vectorstore._collection
        self.old_embedding_diagnostics = vectorstore._embedding_diagnostics
        self.old_collection_name = vectorstore._collection_name
        self.old_embedding_config_hash = vectorstore._embedding_config_hash

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        vectorstore._collection = self.old_collection
        vectorstore._embedding_diagnostics = self.old_embedding_diagnostics
        vectorstore._collection_name = self.old_collection_name
        vectorstore._embedding_config_hash = self.old_embedding_config_hash
        self.tmp.cleanup()

    def test_init_failure_raises_without_exiting_process(self) -> None:
        original_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "chromadb":
                raise ImportError("chromadb unavailable")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
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
        self.assertIn("ImportError: chromadb unavailable", message)
        self.assertIn("配置 embedding_base_url", message)

    def test_init_uses_configured_base_url_without_adding_v1(self) -> None:
        captured: list[dict] = []
        collections: list[dict] = []

        class FakeOpenAIEmbeddingFunction:
            def __init__(self, **kwargs):
                captured.append(kwargs)

        class FakeEmbeddingFunction:
            def __class_getitem__(cls, item):
                del item
                return cls

        class FakeClient:
            def __init__(self, path):
                self.path = path

            def get_or_create_collection(self, **kwargs):
                collections.append(kwargs)
                return FakeCollection({"ids": [[]]})

        modules = {
            "chromadb": types.SimpleNamespace(PersistentClient=FakeClient),
            "chromadb.api": types.SimpleNamespace(),
            "chromadb.api.types": types.SimpleNamespace(Embeddable=object, EmbeddingFunction=FakeEmbeddingFunction),
            "chromadb.utils": types.SimpleNamespace(),
            "chromadb.utils.embedding_functions": types.SimpleNamespace(),
            "chromadb.utils.embedding_functions.openai_embedding_function": types.SimpleNamespace(
                OpenAIEmbeddingFunction=FakeOpenAIEmbeddingFunction
            ),
        }

        with patch.dict(sys.modules, modules):
            vectorstore.init_vectorstore(
                api_key="main-key",
                base_url="https://api.openai.com",
                embedding_model="text-embedding-3-small",
            )
            vectorstore.init_vectorstore(
                api_key="main-key",
                base_url="https://api.deepseek.com",
                embedding_model="text-embedding-3-small",
                embedding_base_url="https://api.openai.com/v1",
                embedding_api_key="embedding-key",
            )
            vectorstore.init_vectorstore(
                api_key="rotated-main-key",
                base_url="https://api.deepseek.com",
                embedding_model="text-embedding-3-small",
                embedding_base_url="https://api.openai.com/v1",
                embedding_api_key="rotated-embedding-key",
            )

        self.assertEqual("https://api.openai.com", captured[0]["api_base"])
        self.assertEqual("main-key", captured[0]["api_key"])
        self.assertEqual("https://api.openai.com/v1", captured[1]["api_base"])
        self.assertEqual("embedding-key", captured[1]["api_key"])
        self.assertEqual("rotated-embedding-key", captured[2]["api_key"])
        self.assertNotEqual(collections[0]["name"], collections[1]["name"])
        self.assertEqual(collections[1]["name"], collections[2]["name"])
        self.assertEqual("text-embedding-3-small", collections[0]["metadata"]["embedding_model"])
        self.assertEqual("https://api.openai.com", collections[0]["metadata"]["embedding_base_url"])
        self.assertEqual("https://api.openai.com/v1", collections[1]["metadata"]["embedding_base_url"])

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

    def test_query_post_hits_returns_ranks_and_distances(self) -> None:
        vectorstore._collection = FakeCollection(
            {"ids": [["p-1", "p-2"]], "distances": [[0.2, 0.7]]}
        )

        with patch("core.vector_index_service.is_current_collection_query_ready", return_value=True):
            hits = vectorstore.query_post_hits("焦虑", n_results=20)

        self.assertEqual(
            [
                vectorstore.VectorHit("p-1", 1, 0.2),
                vectorstore.VectorHit("p-2", 2, 0.7),
            ],
            hits,
        )

    def test_query_post_hits_falls_back_when_distances_unavailable(self) -> None:
        vectorstore._collection = FakeCollection(
            {"ids": [["p-1", "p-2"]]},
            fail_with_include=True,
        )

        with patch("core.vector_index_service.is_current_collection_query_ready", return_value=True):
            hits = vectorstore.query_post_hits("焦虑", n_results=20)

        self.assertEqual(
            [
                vectorstore.VectorHit("p-1", 1, None),
                vectorstore.VectorHit("p-2", 2, None),
            ],
            hits,
        )

    def test_query_documents_skips_vector_search_when_collection_not_ready(self) -> None:
        vectorstore._collection = FakeCollection(
            {"ids": [["p-1"]], "distances": [[0.2]]}
        )

        with patch("core.vector_index_service.is_current_collection_query_ready", return_value=False):
            hits = vectorstore.query_documents("焦虑", n_results=20)

        self.assertEqual([], hits)

    def test_query_post_hits_logs_when_query_fails_after_fallback(self) -> None:
        class FailingCollection:
            def count(self) -> int:
                return 1

            def query(self, **kwargs):
                raise RuntimeError("embedding endpoint unavailable")

        vectorstore._collection = FailingCollection()
        vectorstore._embedding_diagnostics = {
            "embedding_model": "text-embedding-3-small",
            "embedding_base_url": "https://api.deepseek.com",
            "embedding_base_url_source": "base_url",
        }

        with patch("core.vector_index_service.is_current_collection_query_ready", return_value=True):
            hits = vectorstore.query_post_hits("焦虑", n_results=20)
        event = self._last_log_event("vector_query_failed")

        self.assertEqual([], hits)
        self.assertEqual("vector_query_posts", event["operation"])
        self.assertEqual("RuntimeError", event["exception_type"])
        self.assertEqual("embedding endpoint unavailable", event["exception_message"])
        self.assertIn("配置 embedding_base_url", " ".join(event["suggestions"]))

    def test_query_post_ids_preserves_existing_behavior(self) -> None:
        vectorstore._collection = FakeCollection(
            {"ids": [["p-1", "p-2"]], "distances": [[0.2, 0.7]]}
        )

        with patch("core.vector_index_service.is_current_collection_query_ready", return_value=True):
            self.assertEqual(["p-1", "p-2"], vectorstore.query_post_ids("焦虑", n_results=20))

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
