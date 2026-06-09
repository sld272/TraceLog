from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db
from core.app_services import event_service, job_service
from tests.helpers import require_not_none


class JobEventServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self._insert_post("p-1")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_enqueue_claim_and_mark_job(self) -> None:
        job_id = job_service.enqueue(job_service.TYPE_INDEX_POST_EMBEDDING, {"post_id": "p-1"})

        claimed = require_not_none(job_service.claim_next_pending())
        self.assertEqual(job_id, claimed["id"])
        self.assertEqual("running", claimed["status"])
        self.assertEqual({"post_id": "p-1"}, claimed["payload"])

        job_service.mark_succeeded(job_id)
        done = require_not_none(job_service.get_job(job_id))
        self.assertEqual("succeeded", done["status"])
        self.assertIsNotNone(done["finished_at"])

    def test_mark_failed_records_error(self) -> None:
        job_id = job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": "p-1"})
        job_service.claim_next_pending()

        job_service.mark_failed(job_id, "boom")
        failed = require_not_none(job_service.get_job(job_id))

        self.assertEqual("failed", failed["status"])
        self.assertEqual("boom", failed["error"])

    def test_mark_failed_or_retry_requeues_until_max_attempts(self) -> None:
        job_id = job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": "p-1"}, max_attempts=2)
        job_service.claim_next_pending()

        job_service.mark_failed_or_retry(job_id, "first boom")
        pending = require_not_none(job_service.get_job(job_id))

        self.assertEqual("pending", pending["status"])
        self.assertEqual("first boom", pending["error"])

    def test_enqueue_defaults_to_three_attempts(self) -> None:
        job_id = job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": "p-1"})

        job = require_not_none(job_service.get_job(job_id))

        self.assertEqual(3, job["max_attempts"])

    def test_mark_failed_or_retry_does_not_retry_configuration_errors(self) -> None:
        job_id = job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": "p-1"})
        job_service.claim_next_pending()

        job_service.mark_failed_or_retry(job_id, "401 invalid api key")
        failed = require_not_none(job_service.get_job(job_id))

        self.assertEqual("failed", failed["status"])
        self.assertEqual("401 invalid api key", failed["error"])

    def test_post_events_list_after_id(self) -> None:
        first = event_service.append_post_event("p-1", "post_created", {"ok": True})
        second = event_service.append_post_event("p-1", "reply_started", {"soul_name": "默认"})

        events = event_service.list_post_events("p-1", after_id=first)

        self.assertEqual([second], [event["id"] for event in events])
        self.assertEqual("reply_started", events[0]["event_type"])
        self.assertEqual({"soul_name": "默认"}, events[0]["payload"])
        self.assertEqual("reply_started", event_service.latest_event_type("p-1"))

    def _insert_post(self, post_id: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-31T10:00:00+08:00", "测试 post", 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
