from __future__ import annotations

import builtins
import unittest
from unittest.mock import patch

from core import vectorstore


class VectorStoreTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
