from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_reconcile_runner,
    memory_unit_service as mus,
    memory_view_service as mvs,
)
from core.app_services import job_service, public_post_pipeline
from core.llm import reflection_router


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
            return memory_reconcile_runner.ReconcileRunResult([], [], False)

        with patch("core.memory_reconcile_runner.run_pending_reconcile", fake_run):
            public_post_pipeline.execute_job(job, client=object(), model="m")
        self.assertEqual(calls, ["api"])

    def test_execute_job_raises_aggregate_failure_after_refreshing_derivatives(self) -> None:
        job_id = job_service.enqueue_memory_reconcile_once()
        job = job_service.get_job(int(job_id))
        client = object()
        failure = memory_reconcile_runner.ReconcileBucketFailure(
            "global", "public", "llm timeout"
        )
        result = memory_reconcile_runner.ReconcileRunResult([], [failure], True)

        with (
            patch("core.memory_reconcile_runner.run_pending_reconcile", return_value=result),
            patch("core.memory_view_producer.refresh_views_after_reconcile") as refresh,
            patch("core.vector_index_service.rebuild_expected_docs") as rebuild,
        ):
            with self.assertRaisesRegex(
                public_post_pipeline.MemoryReconcileRunError,
                r"global/public: llm timeout",
            ):
                public_post_pipeline.execute_job(job, client=client, model="m")

        refresh.assert_called_once_with(client, "m")
        rebuild.assert_called_once_with()
        self.assertEqual(
            db.query_all(
                "SELECT id FROM jobs WHERE type = ? AND status = ?",
                (job_service.TYPE_RUN_MEMORY_RECONCILE, job_service.STATUS_PENDING),
            )[0]["id"],
            job_id,
        )

    def test_bounded_backlog_enqueues_one_continuation_and_next_job_drains_it(self) -> None:
        with db.transaction() as conn:
            event_ids = [
                mes.record_post_mutation(
                    conn,
                    post_id=f"p{index}",
                    op="create",
                    content=f"content {index}",
                    occurred_at=float(index),
                ).id
                for index in range(201)
            ]
        job_service.enqueue_memory_reconcile_once()
        first = job_service.claim_next_pending()

        with (
            patch.object(
                reflection_router,
                "call_memory_reconcile",
                return_value={"summary": "", "ops": []},
            ),
            patch("core.memory_view_producer.refresh_views_after_reconcile"),
            patch("core.vector_index_service.rebuild_expected_docs"),
        ):
            public_post_pipeline.execute_job(first, client=object(), model="m")

        continuations = db.query_all(
            "SELECT * FROM jobs WHERE type = ? AND status = ? ORDER BY id",
            (job_service.TYPE_RUN_MEMORY_RECONCILE, job_service.STATUS_PENDING),
        )
        self.assertEqual(len(continuations), 1)
        payload = job_service.get_job(int(continuations[0]["id"]))["payload"]
        self.assertEqual(payload["trigger"], "continuation")
        self.assertEqual(payload["previous_job_id"], first["id"])
        self.assertEqual(mes.get_cursor("global", "public"), event_ids[199])

        job_service.mark_succeeded(int(first["id"]))
        second = job_service.claim_next_pending()
        with (
            patch.object(
                reflection_router,
                "call_memory_reconcile",
                return_value={"summary": "", "ops": []},
            ),
            patch("core.memory_view_producer.refresh_views_after_reconcile"),
            patch("core.vector_index_service.rebuild_expected_docs"),
        ):
            public_post_pipeline.execute_job(second, client=object(), model="m")
        job_service.mark_succeeded(int(second["id"]))

        self.assertEqual(mes.get_cursor("global", "public"), event_ids[-1])
        self.assertEqual(mes.buckets_with_pending_events(), [])
        self.assertEqual(
            db.query_all(
                "SELECT id FROM jobs WHERE type = ? AND status = ?",
                (job_service.TYPE_RUN_MEMORY_RECONCILE, job_service.STATUS_PENDING),
            ),
            [],
        )

    def test_existing_pending_reconcile_satisfies_continuation(self) -> None:
        current_id = job_service.enqueue_memory_reconcile_once()
        current = job_service.claim_next_pending()
        pending_id = job_service.enqueue_memory_reconcile_once({"trigger": "write_during_run"})
        result = memory_reconcile_runner.ReconcileRunResult([], [], True)

        with (
            patch("core.memory_reconcile_runner.run_pending_reconcile", return_value=result),
            patch("core.memory_view_producer.refresh_views_after_reconcile"),
            patch("core.vector_index_service.rebuild_expected_docs"),
        ):
            public_post_pipeline.execute_job(current, client=object(), model="m")

        pending = db.query_all(
            "SELECT id FROM jobs WHERE type = ? AND status = ? ORDER BY id",
            (job_service.TYPE_RUN_MEMORY_RECONCILE, job_service.STATUS_PENDING),
        )
        self.assertEqual([int(row["id"]) for row in pending], [pending_id])
        self.assertEqual(current["id"], current_id)

    def test_reconcile_job_end_to_end_builds_unit_and_view(self) -> None:
        # The full v2 spine through one job: evidence -> reconcile -> unit ->
        # recompute slice -> view synthesis -> fresh portrait.
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0)
        job_id = job_service.enqueue_memory_reconcile_once()
        job = job_service.get_job(int(job_id))

        def fake_reconcile(client, model, *, boundary_text, events_text, active_units_text, tombstones_text, trace_context=None):
            ids = [int(t.split("event_id=")[1].split(" ")[0]) for t in events_text.split("- ") if "event_id=" in t]
            return {
                "summary": "s",
                "ops": [{
                    "op": "add", "type": "insight", "content": "用户在准备考研",
                    "confidence": 0.9, "tier": "core", "importance": 0.85,
                    "evidence_event_ids": ids,
                }],
            }

        with patch.object(reflection_router, "call_memory_reconcile", fake_reconcile), \
                patch.object(reflection_router, "call_view_synthesis", lambda *a, **k: "你正在备考考研。"):
            public_post_pipeline.execute_job(job, client=object(), model="m")

        units = mus.list_active_units_in_bucket("global", "public")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["content"], "用户在准备考研")
        view = mvs.get_view("global", "public", mvs.VIEW_USER_MD)
        self.assertIsNotNone(view)
        self.assertEqual(view["status"], "fresh")
        self.assertIn("备考考研", view["content_md"])


if __name__ == "__main__":
    unittest.main()
