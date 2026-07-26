from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api import deps
from core import goal_schedule_service, soul_proactive_service


class ScheduleAutomationRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_automation_runs_even_when_remote_sync_fails(self) -> None:
        old_runtime = deps._runtime
        config = {
            "proactive_message": {
                "enabled": True,
                "silence_days": 7,
                "notify_desktop": True,
            }
        }
        deps._runtime = SimpleNamespace(
            client="client",
            model="model",
            config=config,
        )
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
                (
                    soul_proactive_service.run_proactive_message_best_effort.__name__,
                    (config, "client", "model"),
                ),
            ],
            calls,
        )
        log_event.assert_called_once_with(
            "schedule_sync_failed",
            level="WARNING",
            error_type="RuntimeError",
        )

    async def test_disabled_proactive_feature_never_enters_delivery_path(
        self,
    ) -> None:
        cases = (
            ("config", False, ""),
            ("env", True, "1"),
        )
        for label, config_enabled, env_value in cases:
            with self.subTest(label=label):
                old_runtime = deps._runtime
                config = {
                    "proactive_message": {
                        "enabled": config_enabled,
                        "silence_days": 7,
                        "notify_desktop": True,
                    }
                }
                deps._runtime = SimpleNamespace(
                    client="client",
                    model="model",
                    config=config,
                )
                calls: list[str] = []

                async def fake_run_sync(func, *args, **kwargs):
                    calls.append(func.__name__)
                    return None

                try:
                    with (
                        patch.dict(
                            os.environ,
                            {
                                soul_proactive_service.PROACTIVE_MESSAGE_DISABLED_ENV: env_value
                            },
                        ),
                        patch(
                            "api.deps.run_sync",
                            side_effect=fake_run_sync,
                        ),
                    ):
                        await deps._run_schedule_maintenance_once()
                finally:
                    deps._runtime = old_runtime

                self.assertNotIn(
                    soul_proactive_service.run_proactive_message_best_effort.__name__,
                    calls,
                )


if __name__ == "__main__":
    unittest.main()
