from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from core.embedding_client import EmbeddingClient


class EmbeddingClientTest(unittest.TestCase):
    def test_embed_texts_batches_at_64_and_restores_response_order(self) -> None:
        batch_sizes: list[int] = []

        class FakeEmbeddings:
            def create(self, *, input, model, encoding_format):
                self.model = model
                self.encoding_format = encoding_format
                batch_sizes.append(len(input))
                data = [
                    SimpleNamespace(index=index, embedding=[float(index), 1.0])
                    for index in reversed(range(len(input)))
                ]
                return SimpleNamespace(data=data)

        fake_openai = SimpleNamespace(embeddings=FakeEmbeddings())
        with patch("core.embedding_client.OpenAI", return_value=fake_openai):
            client = EmbeddingClient(
                api_key="key",
                base_url="https://example.invalid/v1",
                model="embedding",
            )
            vectors = client.embed_texts([f"text-{index}" for index in range(130)])

        self.assertEqual([64, 64, 2], batch_sizes)
        self.assertEqual(130, len(vectors))
        np.testing.assert_array_equal(np.asarray([0.0, 1.0], dtype=np.float32), vectors[0])
        np.testing.assert_array_equal(np.asarray([1.0, 1.0], dtype=np.float32), vectors[-1])


if __name__ == "__main__":
    unittest.main()
