from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
