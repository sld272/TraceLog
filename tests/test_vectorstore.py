from __future__ import annotations

import builtins
import unittest
from unittest.mock import patch

from core import vectorstore


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
        self.old_collection = vectorstore._collection

    def tearDown(self) -> None:
        vectorstore._collection = self.old_collection

    def test_init_failure_raises_without_exiting_process(self) -> None:
        original_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "chromadb":
                raise ImportError("chromadb unavailable")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(vectorstore.VectorStoreInitError):
                vectorstore.init_vectorstore(
                    api_key="test-key",
                    base_url="https://example.invalid/v1",
                    embedding_model="test-embedding",
                )

        self.assertFalse(vectorstore.is_initialized())

    def test_query_post_hits_returns_ranks_and_distances(self) -> None:
        vectorstore._collection = FakeCollection(
            {"ids": [["p-1", "p-2"]], "distances": [[0.2, 0.7]]}
        )

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

        hits = vectorstore.query_post_hits("焦虑", n_results=20)

        self.assertEqual(
            [
                vectorstore.VectorHit("p-1", 1, None),
                vectorstore.VectorHit("p-2", 2, None),
            ],
            hits,
        )

    def test_query_post_ids_preserves_existing_behavior(self) -> None:
        vectorstore._collection = FakeCollection(
            {"ids": [["p-1", "p-2"]], "distances": [[0.2, 0.7]]}
        )

        self.assertEqual(["p-1", "p-2"], vectorstore.query_post_ids("焦虑", n_results=20))


if __name__ == "__main__":
    unittest.main()
