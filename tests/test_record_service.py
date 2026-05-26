from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import db, record_service
from tests.helpers import require_not_none


class FakeVectorStore:
    def __init__(self, *, initialized: bool = True, error: BaseException | None = None) -> None:
        self.initialized = initialized
        self.error = error
        self.indexed: list[tuple[str, str]] = []

    def is_initialized(self) -> bool:
        return self.initialized

    def index_post(self, post_id: str, content: str) -> None:
        if self.error is not None:
            raise self.error
        self.indexed.append((post_id, content))


class RecordServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_vectorstore = record_service._vectorstore

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        record_service._vectorstore = self.old_vectorstore
        self.tmp.cleanup()

    def test_save_post_indexes_and_clears_pending_embedding(self) -> None:
        fake_vectorstore = FakeVectorStore()
        record_service._vectorstore = lambda: fake_vectorstore

        post_id = record_service.save_post("今天想练歌")

        self.assertEqual([(post_id, "今天想练歌")], fake_vectorstore.indexed)
        post = require_not_none(db.query_one("SELECT content FROM posts WHERE id = ?", (post_id,)))
        self.assertEqual("今天想练歌", post["content"])
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", (f"pending_embedding:{post_id}",)))

    def test_save_post_keeps_pending_embedding_on_index_error(self) -> None:
        fake_vectorstore = FakeVectorStore(error=RuntimeError("embedding failed"))
        record_service._vectorstore = lambda: fake_vectorstore

        post_id = record_service.save_post("今天想练歌")
        pending = require_not_none(db.query_one("SELECT value FROM meta WHERE key = ?", (f"pending_embedding:{post_id}",)))
        payload = json.loads(pending["value"])

        self.assertEqual(post_id, payload["post_id"])
        self.assertEqual("今天想练歌", payload["content"])
        self.assertEqual("embedding failed", payload["error"])
        self.assertIsNotNone(db.query_one("SELECT content FROM posts WHERE id = ?", (post_id,)))

    def test_save_post_preserves_pending_embedding_and_reraises_keyboard_interrupt(self) -> None:
        fake_vectorstore = FakeVectorStore(error=KeyboardInterrupt())
        record_service._vectorstore = lambda: fake_vectorstore

        with self.assertRaises(KeyboardInterrupt):
            record_service.save_post("今天想练歌")

        post = require_not_none(db.query_one("SELECT id, content FROM posts"))
        pending = require_not_none(db.query_one("SELECT value FROM meta WHERE key = ?", (f"pending_embedding:{post['id']}",)))
        payload = json.loads(pending["value"])
        self.assertEqual(post["id"], payload["post_id"])
        self.assertEqual("今天想练歌", payload["content"])
        self.assertEqual("pending before embedding", payload["error"])

    def test_retry_pending_embeddings_indexes_and_clears_pending(self) -> None:
        fake_vectorstore = FakeVectorStore()
        record_service._vectorstore = lambda: fake_vectorstore
        self._insert_pending("p-1", "待补索引")

        fixed = record_service.retry_pending_embeddings()

        self.assertEqual(1, fixed)
        self.assertEqual([("p-1", "待补索引")], fake_vectorstore.indexed)
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_embedding:p-1",)))

    def test_retry_pending_embeddings_keeps_pending_on_error(self) -> None:
        fake_vectorstore = FakeVectorStore(error=RuntimeError("still failing"))
        record_service._vectorstore = lambda: fake_vectorstore
        self._insert_pending("p-1", "待补索引")

        fixed = record_service.retry_pending_embeddings()

        self.assertEqual(0, fixed)
        self.assertIsNotNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_embedding:p-1",)))

    def _insert_pending(self, post_id: str, content: str) -> None:
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
