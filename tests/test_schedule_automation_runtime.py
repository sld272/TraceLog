from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api import deps
from core import goal_schedule_service


class ScheduleAutomationRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_automation_runs_even_when_remote_sync_fails(self) -> None:
        old_runtime = deps._runtime
        deps._runtime = SimpleNamespace(client="client", model="model")
        calls: list[tuple[str, tuple]] = []

        async def fake_run_sync(func, *args, **kwargs):
            calls.append((func.__name__, args))
            if func.__name__ == "sync":
                raise RuntimeError("sync failed")
            return None

        try:
            with (
                patch("api.deps.run_sync", side_effect=fake_run_sync),
                patch("api.deps.logging_service.log_event") as log_event,
            ):
                await deps._run_schedule_maintenance_once()
        finally:
            deps._runtime = old_runtime

        self.assertEqual(
            [
                ("sync", ()),
                (
                    goal_schedule_service.run_automation_best_effort.__name__,
                    ("client", "model"),
                ),
            ],
            calls,
        )
        log_event.assert_called_once_with(
            "schedule_sync_failed",
            level="WARNING",
            error_type="RuntimeError",
        )


if __name__ == "__main__":
    unittest.main()
