from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, query_rewriter, record_service, retrieval, tool_config_service
from core.app_services import event_service, job_service, public_post_pipeline
from tests.helpers import require_not_none


class FakeVectorStore:
    def __init__(self) -> None:
        self.indexed: list[tuple[str, str]] = []

    def is_initialized(self) -> bool:
        return True

    def index_post(self, post_id: str, content: str) -> None:
        self.indexed.append((post_id, content))


class PublicPostPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_vectorstore = record_service._vectorstore
        self.old_rewrite = query_rewriter.rewrite_query
        self.old_search = retrieval.hybrid_search
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        record_service._vectorstore = self.old_vectorstore
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

    def test_create_post_omits_todo_job_when_tool_disabled(self) -> None:
        tool_config_service.set_tool_enabled("todo", False)

        created = public_post_pipeline.create_post("今天想练歌")
        jobs = job_service.list_jobs_for_post(created.post_id)

        self.assertNotIn("run_todo_tool", [job["type"] for job in jobs])

    def test_index_post_embedding_job_indexes_and_emits_events(self) -> None:
        fake_vectorstore = FakeVectorStore()
        record_service._vectorstore = lambda: fake_vectorstore
        created = public_post_pipeline.create_post("今天想练歌")
        job = require_not_none(job_service.claim_next_pending())

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


if __name__ == "__main__":
    unittest.main()
