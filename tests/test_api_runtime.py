from __future__ import annotations

import unittest
from unittest.mock import patch

from api import deps
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
            patch("api.deps.logging_service.init_logging"),
            patch("api.deps.workspace_service.init_workspace"),
            patch("api.deps._is_model_configured", return_value=True),
            patch("api.deps._start_configured_runtime", side_effect=RuntimeError("reload boom")),
        ):
            with self.assertRaisesRegex(RuntimeError, "reload boom"):
                await deps.reload_runtime()

        self.assertIs(existing_runtime, deps._runtime)  # type: ignore[attr-defined]
        self.assertFalse(worker.stopped)

    async def test_reload_swaps_runtime_after_new_runtime_is_ready(self) -> None:
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
        new_runtime = deps.ApiRuntime(
            config={"model": "new-model"},
            client=object(),
            model="new-model",
            worker=None,
            vectorstore_initialized=True,
            configured=True,
        )
        deps._runtime = existing_runtime  # type: ignore[attr-defined]

        with (
            patch("api.deps._load_api_config", return_value={"api_key": "sk", "base_url": "https://example.invalid/v1", "model": "new-model", "embedding_model": "embed"}),
            patch("api.deps.logging_service.init_logging"),
            patch("api.deps.workspace_service.init_workspace"),
            patch("api.deps._is_model_configured", return_value=True),
            patch("api.deps._start_configured_runtime", return_value=new_runtime),
        ):
            reloaded = await deps.reload_runtime()

        self.assertIs(new_runtime, reloaded)
        self.assertIs(new_runtime, deps._runtime)  # type: ignore[attr-defined]
        self.assertTrue(worker.stopped)


if __name__ == "__main__":
    unittest.main()
