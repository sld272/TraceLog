from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed")
class ApiScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = Path(self.tmp.name) / "workspace"
        db.DB_PATH = db.WORKSPACE_DIR / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from api import deps
        from api.app import create_app

        async def fake_init_runtime():
            deps._runtime = SimpleNamespace(
                configured=False,
                client=None,
                model=None,
                vectorstore_initialized=False,
                worker=None,
            )
            return deps._runtime

        async def fake_shutdown_runtime():
            deps._runtime = None

        init_patch = patch("api.deps.init_runtime", fake_init_runtime)
        shutdown_patch = patch("api.deps.shutdown_runtime", fake_shutdown_runtime)
        init_patch.start()
        shutdown_patch.start()
        self.addCleanup(init_patch.stop)
        self.addCleanup(shutdown_patch.stop)
        return TestClient(create_app())

    def test_unconfigured_backend_status_and_event_read_are_available(self) -> None:
        with self._client() as client:
            status = client.get("/schedule/status")
            events = client.get("/schedule/events?start=2026-07-16&end=2026-07-16")
            create = client.post(
                "/schedule/events",
                json={"subject": "未连接写入", "date": "2026-07-16"},
            )

        self.assertEqual(200, status.status_code)
        self.assertFalse(status.json()["configured"])
        self.assertFalse(status.json()["connected"])
        self.assertEqual(200, events.status_code)
        self.assertEqual([], events.json())
        self.assertEqual("false", events.headers["x-schedule-connected"])
        self.assertEqual(409, create.status_code)

    def test_posts_activity_returns_raw_timestamps_without_bucketing(self) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("p1", "2026-07-16T23:30:00+08:00", "当天", 1.0, 1.0),
        )
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("p2", "2026-07-17T00:30:00+08:00", "次日", 2.0, 2.0),
        )

        with self._client() as client:
            response = client.get("/posts/activity?start=2026-07-16&end=2026-07-16")

        self.assertEqual(200, response.status_code)
        self.assertEqual([{"id": "p1", "ts": "2026-07-16T23:30:00+08:00"}], response.json())

    def test_client_id_endpoint_returns_only_configuration_and_tail(self) -> None:
        client_id = "00000000-0000-0000-0000-123456789abc"

        with self._client() as client:
            saved = client.post("/schedule/auth/client-id", json={"client_id": client_id})
            fetched = client.get("/schedule/auth/client-id")
            status = client.get("/schedule/status")

        expected = {"configured": True, "client_id_tail": "9abc"}
        self.assertEqual(expected, saved.json())
        self.assertEqual(expected, fetched.json())
        self.assertNotIn(client_id, str(saved.json()))
        self.assertTrue(status.json()["configured"])
        self.assertFalse(status.json()["connected"])


class ScheduleSyncLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        from api import deps

        if deps._schedule_sync_task is not None:  # type: ignore[attr-defined]
            await deps.shutdown_runtime()

    async def test_shutdown_cleanly_cancels_periodic_sync_task(self) -> None:
        from api import deps

        started = asyncio.Event()

        async def fake_run_sync(func, *args, **kwargs):
            del func, args, kwargs
            started.set()
            return {"ok": False, "status": "not_connected"}

        deps._runtime = None  # type: ignore[attr-defined]
        deps._schedule_sync_task = None  # type: ignore[attr-defined]
        with patch("api.deps.run_sync", fake_run_sync):
            deps._start_schedule_sync_task()  # type: ignore[attr-defined]
            task = deps._schedule_sync_task  # type: ignore[attr-defined]
            await asyncio.wait_for(started.wait(), timeout=1.0)
            await deps.shutdown_runtime()

        self.assertIsNotNone(task)
        self.assertTrue(task.done())
        self.assertIsNone(deps._schedule_sync_task)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
