from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, memory_read
from core.app_services import job_service, public_post_pipeline


class MemoryReconcileJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _job_types(self) -> list[str]:
        return [r["type"] for r in db.query_all("SELECT type FROM jobs ORDER BY id ASC")]

    def test_enqueue_once_dedupes_pending(self) -> None:
        first = job_service.enqueue_memory_reconcile_once({"trigger": "post"})
        second = job_service.enqueue_memory_reconcile_once({"trigger": "comment"})
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # a pending reconcile job already covers it
        pending = db.query_all(
            "SELECT id FROM jobs WHERE type = ? AND status = ?",
            (job_service.TYPE_RUN_MEMORY_RECONCILE, job_service.STATUS_PENDING),
        )
        self.assertEqual(len(pending), 1)

    def test_enqueue_once_allows_new_job_after_drain(self) -> None:
        first = job_service.enqueue_memory_reconcile_once()
        job_service.mark_succeeded(int(first))
        second = job_service.enqueue_memory_reconcile_once()
        self.assertIsNotNone(second)

    def test_v2_write_mode_enqueues_reconcile_not_reflection(self) -> None:
        with patch.dict(os.environ, {memory_read.WRITE_MODE_ENV: "reconcile"}):
            public_post_pipeline.create_post("今天在准备考研，压力有点大")
        types = self._job_types()
        self.assertIn(job_service.TYPE_RUN_MEMORY_RECONCILE, types)
        self.assertNotIn(job_service.TYPE_RUN_LIGHT_REFLECTION, types)
        self.assertNotIn(job_service.TYPE_MAYBE_TRIGGER_GLOBAL_DEEP_REFLECTION, types)

    def test_legacy_write_mode_keeps_reflection(self) -> None:
        # default (no env set) is legacy: unchanged behaviour
        public_post_pipeline.create_post("今天在准备考研，压力有点大")
        types = self._job_types()
        self.assertIn(job_service.TYPE_RUN_LIGHT_REFLECTION, types)
        self.assertNotIn(job_service.TYPE_RUN_MEMORY_RECONCILE, types)

    def test_execute_job_dispatches_reconcile_to_runner(self) -> None:
        job_id = job_service.enqueue_memory_reconcile_once()
        job = job_service.get_job(int(job_id))
        calls = []

        def fake_run(client, model, *, trigger="manual", **kwargs):
            calls.append(trigger)
            return []

        with patch("core.memory_reconcile_runner.run_pending_reconcile", fake_run):
            public_post_pipeline.execute_job(job, client=object(), model="m")
        self.assertEqual(calls, ["api"])


if __name__ == "__main__":
    unittest.main()
