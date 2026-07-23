"""Thin OpenAI-compatible embedding client used by the SQLite vector store."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from openai import OpenAI

EMBEDDING_BATCH_SIZE = 64


class EmbeddingClient:
    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def embed_texts(self, texts: Sequence[str]) -> list[np.ndarray]:
        vectors: list[np.ndarray] = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = list(texts[start : start + EMBEDDING_BATCH_SIZE])
            response = self._client.embeddings.create(
                input=batch,
                model=self._model,
                encoding_format="float",
            )
            ordered = sorted(response.data, key=lambda item: int(item.index))
            vectors.extend(np.asarray(item.embedding, dtype=np.float32) for item in ordered)
        return vectors
