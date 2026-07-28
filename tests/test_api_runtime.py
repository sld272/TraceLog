from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from api import deps
from core import db, memory_events_service as mes, memory_reconcile_runner
from core.app_services import job_service, public_post_pipeline
from core.app_services.api_runtime import JobWorker


class JobWorkerCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_orphan_attachments_once_logs_removed_count(self) -> None:
        worker = JobWorker(client=object(), model="test", orphan_attachment_max_age=5)

        with (
            patch(
                "core.app_services.api_runtime.attachment_service.cleanup_orphan_attachments",
                return_value=2,
            ) as cleanup,
            patch("core.app_services.api_runtime.logging_service.log_event") as log_event,
        ):
            removed = await worker.cleanup_orphan_attachments_once()

        self.assertEqual(2, removed)
        cleanup.assert_called_once_with(max_age_seconds=5.0)
        log_event.assert_called_once_with("orphan_attachments_cleaned", removed_count=2, max_age_seconds=5.0)

    async def test_cleanup_orphan_attachments_once_logs_failure_without_crashing(self) -> None:
        worker = JobWorker(client=object(), model="test", orphan_attachment_max_age=5)

        with (
            patch(
                "core.app_services.api_runtime.attachment_service.cleanup_orphan_attachments",
                side_effect=RuntimeError("boom"),
            ),
            patch("core.app_services.api_runtime.logging_service.log_event") as log_event,
        ):
            removed = await worker.cleanup_orphan_attachments_once()

        self.assertEqual(0, removed)
        log_event.assert_called_once_with(
            "orphan_attachments_cleanup_failed",
            level="WARNING",
            max_age_seconds=5.0,
            error="boom",
        )

    async def test_cleanup_loop_runs_once_before_waiting_for_interval(self) -> None:
        worker = JobWorker(client=object(), model="test", orphan_attachment_cleanup_interval=60)
        calls = 0

        async def cleanup_once() -> int:
            nonlocal calls
            calls += 1
            worker._stop.set()
            return 0

        with patch.object(worker, "cleanup_orphan_attachments_once", cleanup_once):
            await worker._run_orphan_attachment_cleanup()

        self.assertEqual(1, calls)

    async def test_start_resets_running_jobs_once_for_all_worker_tasks(self) -> None:
        class FakeTask:
            def done(self) -> bool:
                return False

        created_tasks = []

        def fake_create_task(coro):
            coro.close()
            created_tasks.append(coro)
            return FakeTask()

        worker = JobWorker(client=object(), model="test", concurrency=3)

        with (
            patch("core.app_services.api_runtime._active_job_ids_snapshot", return_value=set()),
            patch(
                "core.app_services.api_runtime.job_service.reset_orphaned_running_to_pending",
                return_value=0,
            ) as reset,
            patch("core.app_services.api_runtime.asyncio.create_task", side_effect=fake_create_task),
        ):
            worker.start()

        self.assertEqual(4, len(created_tasks))
        reset.assert_called_once_with(set())

    async def test_run_does_not_reset_running_jobs_per_task(self) -> None:
        worker = JobWorker(client=object(), model="test")

        def claim_none_and_stop():
            worker._stop.set()
            return None

        async def sleep_noop(delay: float) -> None:
            del delay

        with (
            patch("core.app_services.api_runtime.job_service.reset_orphaned_running_to_pending") as reset,
            patch("core.app_services.api_runtime.job_service.claim_next_pending", side_effect=claim_none_and_stop),
            patch("core.app_services.api_runtime.asyncio.sleep", side_effect=sleep_noop),
        ):
            await worker._run()

        reset.assert_not_called()

    async def test_run_uses_reconcile_specific_retry_handler(self) -> None:
        worker = JobWorker(client=object(), model="test")
        job = {
            "id": 7,
            "type": "run_memory_reconcile",
            "status": "running",
            "payload": {},
        }
        claims = iter([job, None])

        def claim():
            item = next(claims)
            if item is None:
                worker._stop.set()
            return item

        async def sleep_noop(delay: float) -> None:
            del delay

        with (
            patch("core.app_services.api_runtime.job_service.claim_next_pending", side_effect=claim),
            patch(
                "core.app_services.api_runtime.public_post_pipeline.execute_job",
                side_effect=RuntimeError("memory reconcile failed"),
            ),
            patch(
                "core.app_services.api_runtime.job_service.mark_memory_reconcile_failed_or_retry"
            ) as reconcile_retry,
            patch("core.app_services.api_runtime.job_service.mark_failed_or_retry") as generic_retry,
            patch("core.app_services.api_runtime.public_post_pipeline.maybe_emit_pipeline_done_for_job"),
            patch("core.app_services.api_runtime.asyncio.sleep", side_effect=sleep_noop),
        ):
            await worker._run()

        reconcile_retry.assert_called_once_with(7, "memory reconcile failed")
        generic_retry.assert_not_called()


class JobWorkerHandoffTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    async def asyncTearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def _wait_for(self, predicate) -> None:
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("condition did not become true")

    async def test_timeout_handoff_keeps_live_sync_job_owned_until_terminal_pipeline_done(self) -> None:
        job_id = job_service.enqueue(job_service.TYPE_INDEX_POST_EMBEDDING, {"post_id": "p1"})
        started = threading.Event()
        release = threading.Event()
        executions: list[int] = []
        old_worker = JobWorker(
            client=object(),
            model="old-model",
            poll_interval=0.01,
            orphan_attachment_cleanup_interval=3600,
        )
        new_worker = JobWorker(
            client=object(),
            model="new-model",
            poll_interval=0.01,
            orphan_attachment_cleanup_interval=3600,
        )

        def blocking_execute(job, client, model) -> None:
            del client, model
            executions.append(int(job["id"]))
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test job was not released")

        try:
            with (
                patch(
                    "core.app_services.api_runtime.public_post_pipeline.execute_job",
                    side_effect=blocking_execute,
                ),
                patch(
                    "core.app_services.api_runtime.public_post_pipeline.maybe_emit_pipeline_done_for_job"
                ) as pipeline_done,
                patch(
                    "core.app_services.api_runtime.attachment_service.cleanup_orphan_attachments",
                    return_value=0,
                ),
            ):
                old_worker.start()
                self.assertTrue(await asyncio.to_thread(started.wait, 1))

                await old_worker.stop(timeout=0.01)
                running = job_service.get_job(job_id)
                self.assertEqual(job_service.STATUS_RUNNING, running["status"])
                self.assertEqual(1, running["attempts"])
                pipeline_done.assert_not_called()

                new_worker.start()
                await asyncio.sleep(0.05)
                self.assertEqual([job_id], executions)
                still_running = job_service.get_job(job_id)
                self.assertEqual(job_service.STATUS_RUNNING, still_running["status"])
                self.assertEqual(1, still_running["attempts"])

                release.set()
                await self._wait_for(
                    lambda: job_service.get_job(job_id)["status"] == job_service.STATUS_SUCCEEDED
                )
                self.assertEqual([job_id], executions)
                pipeline_done.assert_called_once()
                self.assertEqual(job_id, int(pipeline_done.call_args.args[0]["id"]))
        finally:
            release.set()
            await old_worker.stop(timeout=1)
            await new_worker.stop(timeout=1)

    async def test_start_recovers_running_job_without_a_live_owner(self) -> None:
        job_id = job_service.enqueue(job_service.TYPE_INDEX_POST_EMBEDDING, {"post_id": "p2"})
        claimed = job_service.claim_next_pending()
        self.assertIsNotNone(claimed)
        self.assertEqual(job_id, int(claimed["id"]))
        self.assertEqual(job_service.STATUS_RUNNING, job_service.get_job(job_id)["status"])
        executions: list[int] = []
        worker = JobWorker(
            client=object(),
            model="test-model",
            poll_interval=0.01,
            orphan_attachment_cleanup_interval=3600,
        )

        def execute_recovered(job, client, model) -> None:
            del client, model
            executions.append(int(job["id"]))

        try:
            with (
                patch(
                    "core.app_services.api_runtime.public_post_pipeline.execute_job",
                    side_effect=execute_recovered,
                ),
                patch("core.app_services.api_runtime.public_post_pipeline.maybe_emit_pipeline_done_for_job"),
                patch(
                    "core.app_services.api_runtime.attachment_service.cleanup_orphan_attachments",
                    return_value=0,
                ),
            ):
                worker.start()
                await self._wait_for(
                    lambda: job_service.get_job(job_id)["status"] == job_service.STATUS_SUCCEEDED
                )

            recovered = job_service.get_job(job_id)
            self.assertEqual([job_id], executions)
            self.assertEqual(2, recovered["attempts"])
        finally:
            await worker.stop(timeout=1)


class MemoryReconcileWorkerStateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    async def asyncTearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_failed_reconcile_is_requeued_without_advancing_cursor(self) -> None:
        with db.transaction() as conn:
            event_id = mes.record_post_mutation(
                conn,
                post_id="p1",
                op="create",
                content="待处理证据",
                occurred_at=1.0,
            ).id
        job_id = job_service.enqueue_memory_reconcile_once()
        worker = JobWorker(client=object(), model="test")
        failure = public_post_pipeline.MemoryReconcileRunError(
            [
                memory_reconcile_runner.ReconcileBucketFailure(
                    "global", "public", "llm timeout"
                )
            ]
        )
        original_mark = job_service.mark_memory_reconcile_failed_or_retry

        def mark_and_stop(failed_job_id: int, error: str) -> None:
            original_mark(failed_job_id, error)
            worker._stop.set()

        with (
            patch(
                "core.app_services.api_runtime.public_post_pipeline.execute_job",
                side_effect=failure,
            ),
            patch(
                "core.app_services.api_runtime.job_service.mark_memory_reconcile_failed_or_retry",
                side_effect=mark_and_stop,
            ),
            patch("core.app_services.api_runtime.public_post_pipeline.maybe_emit_pipeline_done_for_job"),
        ):
            await worker._run()

        job = job_service.get_job(int(job_id))
        self.assertEqual(job_service.STATUS_PENDING, job["status"])
        self.assertEqual(1, job["attempts"])
        self.assertIn("global/public: llm timeout", job["error"])
        self.assertEqual(0, mes.get_cursor("global", "public"))
        self.assertGreater(event_id, 0)


class ApiRuntimeReloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        deps._runtime = None  # type: ignore[attr-defined]

    async def test_reload_keeps_existing_runtime_when_new_runtime_fails(self) -> None:
        class ExistingWorker:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        worker = ExistingWorker()
        existing_runtime = deps.ApiRuntime(
            config={"model": "old-model"},
            client=object(),
            model="old-model",
            worker=worker,  # type: ignore[arg-type]
            vectorstore_initialized=True,
            configured=True,
        )
        deps._runtime = existing_runtime  # type: ignore[attr-defined]

        with (
            patch("api.deps._load_api_config", return_value={"api_key": "sk", "base_url": "https://example.invalid/v1", "model": "new-model", "embedding_model": "embed"}),
            patch("api.deps.logging_service.update_config"),
            patch("api.deps.workspace_service.migrate_workspace_permissions") as migrate_permissions,
            patch("api.deps.workspace_service.init_workspace"),
            patch("api.deps._is_model_configured", return_value=True),
            patch("api.deps._build_configured_runtime", side_effect=RuntimeError("reload boom")),
        ):
            with self.assertRaisesRegex(RuntimeError, "reload boom"):
                await deps.reload_runtime()

        self.assertIs(existing_runtime, deps._runtime)  # type: ignore[attr-defined]
        self.assertFalse(worker.stopped)
        migrate_permissions.assert_called_once_with()

    async def test_reload_stops_previous_worker_before_starting_new_worker(self) -> None:
        events: list[str] = []

        class ExistingWorker:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                events.append("old.stop")
                self.stopped = True

        class NewWorker:
            def __init__(self) -> None:
                self.started = False

            def start(self) -> None:
                events.append("new.start")
                self.started = True

        worker = ExistingWorker()
        new_worker = NewWorker()
        existing_runtime = deps.ApiRuntime(
            config={"model": "old-model"},
            client=object(),
            model="old-model",
            worker=worker,  # type: ignore[arg-type]
            vectorstore_initialized=True,
            configured=True,
        )
        new_runtime = deps.ApiRuntime(
            config={"model": "new-model"},
            client=object(),
            model="new-model",
            worker=new_worker,  # type: ignore[arg-type]
            vectorstore_initialized=True,
            configured=True,
        )
        deps._runtime = existing_runtime  # type: ignore[attr-defined]

        with (
            patch("api.deps._load_api_config", return_value={"api_key": "sk", "base_url": "https://example.invalid/v1", "model": "new-model", "embedding_model": "embed"}),
            patch("api.deps.logging_service.update_config"),
            patch("api.deps.workspace_service.migrate_workspace_permissions") as migrate_permissions,
            patch("api.deps.workspace_service.init_workspace"),
            patch("api.deps._is_model_configured", return_value=True),
            patch("api.deps._build_configured_runtime", return_value=new_runtime),
            patch("api.deps._enqueue_startup_retries"),
        ):
            reloaded = await deps.reload_runtime()

        self.assertIs(new_runtime, reloaded)
        self.assertIs(new_runtime, deps._runtime)  # type: ignore[attr-defined]
        self.assertTrue(worker.stopped)
        self.assertTrue(new_worker.started)
        self.assertEqual(["old.stop", "new.start"], events)
        migrate_permissions.assert_called_once_with()

    async def test_api_config_normalizes_proactive_message_for_hot_reload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "api_key": "sk",
                        "base_url": "https://example.invalid/v1",
                        "model": "model",
                        "embedding_model": "embedding",
                        "proactive_message": {
                            "enabled": True,
                            "silence_days": 999,
                            "notify_desktop": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch("api.deps.CONFIG_FILE", str(config_path)):
                config = deps._load_api_config()

        self.assertEqual(
            {
                "enabled": True,
                "silence_days": 60,
                "notify_desktop": False,
            },
            config["proactive_message"],
        )


if __name__ == "__main__":
    unittest.main()
