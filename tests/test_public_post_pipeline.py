from __future__ import annotations

import tempfile
import unittest
import io
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core import attachment_service, db, query_rewriter, retrieval, tool_config_service, vector_index_service
from core.app_services import event_service, job_service, public_post_pipeline
from tests.helpers import require_not_none


class FakeVectorStore:
    def __init__(self) -> None:
        self.indexed: list[tuple[str, str]] = []
        self.document_records: dict[str, dict] = {}

    def is_initialized(self) -> bool:
        return True

    def current_collection_name(self) -> str:
        return "tracelog_test"

    def index_document(self, doc_id: str, content: str, metadata: dict) -> None:
        self.indexed.append((str(metadata.get("post_id") or doc_id), content))
        self.document_records[doc_id] = dict(metadata)

    def delete_documents(self, doc_ids: list[str]) -> None:
        for doc_id in doc_ids:
            self.document_records.pop(doc_id, None)

    def list_document_records(self) -> dict[str, dict]:
        return {doc_id: dict(metadata) for doc_id, metadata in self.document_records.items()}


class PublicPostPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_rewrite = query_rewriter.rewrite_query
        self.old_search = retrieval.hybrid_search
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
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
        query_rewriter.rewrite_query = self.old_rewrite
        retrieval.hybrid_search = self.old_search
        self.tmp.cleanup()

    def test_create_post_returns_immediately_and_enqueues_jobs(self) -> None:
        created = public_post_pipeline.create_post("今天想练歌")
        jobs = db.query_all("SELECT type FROM jobs ORDER BY id ASC")
        events = event_service.list_post_events(created.post_id)

        self.assertEqual(created.post_id, require_not_none(db.query_one("SELECT id FROM posts"))["id"])
        self.assertEqual(
            [
                "index_post_embedding",
                "generate_post_replies",
                "run_todo_tool",
                "run_light_reflection",
                "maybe_trigger_global_deep_reflection",
            ],
            [row["type"] for row in jobs],
        )
        self.assertEqual([job["id"] for job in db.query_all("SELECT id FROM jobs ORDER BY id ASC")], created.job_ids)
        self.assertEqual(["post_created"], [event["event_type"] for event in events])

    def test_create_post_can_use_historical_created_at_and_still_enqueue_jobs(self) -> None:
        created_at = datetime(2026, 4, 3, 22, 15, tzinfo=timezone(timedelta(hours=8)))

        created = public_post_pipeline.create_post("今天在仙林图书馆复习", created_at=created_at)
        post = require_not_none(db.query_one("SELECT id, ts FROM posts WHERE id = ?", (created.post_id,)))
        jobs = job_service.list_jobs_for_post(created.post_id)

        self.assertEqual("20260403-001", post["id"])
        self.assertEqual("2026-04-03T22:15:00+08:00", post["ts"])
        self.assertEqual(
            [
                "index_post_embedding",
                "generate_post_replies",
                "run_todo_tool",
                "run_light_reflection",
                "maybe_trigger_global_deep_reflection",
            ],
            [job["type"] for job in jobs],
        )

    def test_create_post_omits_todo_job_when_tool_disabled(self) -> None:
        tool_config_service.set_tool_enabled("todo", False)

        created = public_post_pipeline.create_post("今天想练歌")
        jobs = job_service.list_jobs_for_post(created.post_id)

        self.assertNotIn("run_todo_tool", [job["type"] for job in jobs])

    def test_image_only_post_enqueues_reply_without_text_indexing_jobs(self) -> None:
        attachment = attachment_service.upload_image(_image_bytes(), content_type="image/png")

        created = public_post_pipeline.create_post("", [attachment.id])
        jobs = job_service.list_jobs_for_post(created.post_id)

        self.assertEqual(
            ["generate_post_replies", "run_light_reflection", "maybe_trigger_global_deep_reflection"],
            [job["type"] for job in jobs],
        )
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", (f"pending_vector_doc:post-{created.post_id}",)))

    def test_index_post_embedding_job_indexes_and_emits_events(self) -> None:
        fake_vectorstore = FakeVectorStore()
        created = public_post_pipeline.create_post("今天想练歌")
        job = require_not_none(job_service.claim_next_pending())

        with (
            patch("core.vectorstore.is_initialized", fake_vectorstore.is_initialized),
            patch("core.vectorstore.current_collection_name", fake_vectorstore.current_collection_name),
            patch("core.vectorstore.index_document", fake_vectorstore.index_document),
            patch("core.vectorstore.delete_documents", fake_vectorstore.delete_documents),
            patch("core.vectorstore.list_document_records", fake_vectorstore.list_document_records),
        ):
            public_post_pipeline.execute_job(job, client=None, model="fake")  # type: ignore[arg-type]

        self.assertEqual([(created.post_id, "今天想练歌")], fake_vectorstore.indexed)
        self.assertEqual(
            ["post_created", "embedding_started", "embedding_succeeded"],
            [event["event_type"] for event in event_service.list_post_events(created.post_id)],
        )

    def test_reply_job_without_souls_emits_no_reply_success(self) -> None:
        created = public_post_pipeline.create_post("今天想练歌")
        first = require_not_none(job_service.claim_next_pending())
        job_service.mark_succeeded(first["id"])
        job = require_not_none(job_service.claim_next_pending())
        query_rewriter.rewrite_query = lambda *args, **kwargs: query_rewriter.RewrittenQuery(
            raw_query="今天想练歌",
            semantic_query="今天想练歌",
            keywords=[],
            used_rewrite=False,
        )
        retrieval.hybrid_search = lambda *args, **kwargs: []

        public_post_pipeline.execute_job(job, client=None, model="fake")  # type: ignore[arg-type]
        event_types = [event["event_type"] for event in event_service.list_post_events(created.post_id)]

        self.assertIn("reply_started", event_types)
        self.assertIn("reply_succeeded", event_types)

    def test_reply_job_excludes_current_post_from_retrieval(self) -> None:
        created = public_post_pipeline.create_post("今天想练歌")
        first = require_not_none(job_service.claim_next_pending())
        job_service.mark_succeeded(first["id"])
        job = require_not_none(job_service.claim_next_pending())
        captured: dict[str, object] = {}
        query_rewriter.rewrite_query = lambda *args, **kwargs: query_rewriter.RewrittenQuery(
            raw_query="今天想练歌",
            semantic_query="今天想练歌",
            keywords=[],
            used_rewrite=False,
        )

        def fake_search(*args, **kwargs):
            captured["exclusion"] = kwargs.get("exclusion")
            return []

        retrieval.hybrid_search = fake_search

        public_post_pipeline.execute_job(job, client=None, model="fake")  # type: ignore[arg-type]

        exclusion = captured["exclusion"]
        self.assertIsInstance(exclusion, retrieval.RetrievalExclusion)
        self.assertEqual(frozenset({created.post_id}), exclusion.post_ids)

    def test_reply_job_fails_when_soul_reply_fails(self) -> None:
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            ("拾迹者", "souls/拾迹者.md", 1.0, 1.0),
        )
        soul_dir = self.workspace / "souls"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "拾迹者.md").write_text("拾迹者人格", encoding="utf-8")
        created = public_post_pipeline.create_post("今天想练歌")
        first = require_not_none(job_service.claim_next_pending())
        job_service.mark_succeeded(first["id"])
        job = require_not_none(job_service.claim_next_pending())
        query_rewriter.rewrite_query = lambda *args, **kwargs: query_rewriter.RewrittenQuery(
            raw_query="今天想练歌",
            semantic_query="今天想练歌",
            keywords=[],
            used_rewrite=False,
        )
        retrieval.hybrid_search = lambda *args, **kwargs: []

        with self.assertRaisesRegex(RuntimeError, "reply generation failed"):
            public_post_pipeline.execute_job(job, client=None, model="bad-model")  # type: ignore[arg-type]

        event_types = [event["event_type"] for event in event_service.list_post_events(created.post_id)]
        self.assertIn("reply_failed", event_types)
        self.assertIsNone(
            db.query_one(
                "SELECT content FROM comments WHERE post_id = ? AND soul_name = ?",
                (created.post_id, "拾迹者"),
            )
        )


if __name__ == "__main__":
    unittest.main()


def _image_bytes() -> bytes:
    image = Image.new("RGB", (8, 8), color=(10, 20, 30))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
