from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db, goal_service
from core.graph.auth import GraphAuth, GraphAuthError
from core.schedule_service import ScheduleService
from tests.test_graph_auth import FakePublicClientApplication, FakeSerializableCache


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

    def test_default_client_id_configures_backend_without_login(self) -> None:
        with self._client() as client:
            status = client.get("/schedule/status")
            client_id = client.get("/schedule/auth/client-id")
            events = client.get("/schedule/events?start=2026-07-16&end=2026-07-16")
            create = client.post(
                "/schedule/events",
                json={"subject": "未连接写入", "date": "2026-07-16"},
            )

        self.assertEqual(200, status.status_code)
        self.assertTrue(status.json()["configured"])
        self.assertFalse(status.json()["connected"])
        self.assertEqual(
            {"configured": True, "using_default": True, "client_id_tail": "b173"},
            client_id.json(),
        )
        self.assertEqual(200, events.status_code)
        self.assertEqual([], events.json())
        self.assertEqual("true", events.headers["x-schedule-configured"])
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

        expected = {
            "configured": True,
            "using_default": False,
            "client_id_tail": "9abc",
        }
        self.assertEqual(expected, saved.json())
        self.assertEqual(expected, fetched.json())
        self.assertNotIn(client_id, str(saved.json()))
        self.assertTrue(status.json()["configured"])
        self.assertFalse(status.json()["connected"])

    def test_custom_client_id_switch_and_restore_clear_login_and_schedule_cache(self) -> None:
        token_cache_path = db.WORKSPACE_DIR / "graph_token_cache.json"

        def seed_login_state(event_id: str) -> None:
            token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            token_cache_path.write_text("cached-token", encoding="utf-8")
            db.execute(
                """
                INSERT INTO schedule_events(
                    id, subject, start_ts, end_ts, start_local, end_local, synced_at
                ) VALUES (?, '缓存日程', 1, 2, '2026-07-16T09:00:00',
                          '2026-07-16T10:00:00', 1)
                """,
                (event_id,),
            )
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('graph.delta_link', 'delta')"
            )

        seed_login_state("before-custom")
        custom_id = "00000000-0000-0000-0000-123456789abc"

        with self._client() as client:
            saved = client.post("/schedule/auth/client-id", json={"client_id": custom_id})

            self.assertEqual(200, saved.status_code, saved.text)
            self.assertFalse(token_cache_path.exists())
            self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM schedule_events")["count"])
            self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = 'graph.delta_link'"))

            seed_login_state("before-default")
            restored = client.delete("/schedule/auth/client-id")

        self.assertEqual(
            {"configured": True, "using_default": True, "client_id_tail": "b173"},
            restored.json(),
        )
        self.assertFalse(token_cache_path.exists())
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM schedule_events")["count"])
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = 'graph.client_id'"))
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = 'graph.delta_link'"))

    def test_interactive_login_persists_fake_msal_token_syncs_and_supports_status_alias(self) -> None:
        auth = GraphAuth(
            app_factory=FakePublicClientApplication,
            cache_factory=FakeSerializableCache,
        )
        sync_calls: list[bool] = []

        class SyncOnlyScheduleService:
            def __init__(self, *, auth=None):
                self.auth = auth

            def sync(self):
                sync_calls.append(True)
                return {"ok": True}

        with (
            patch("api.routes.schedule.GraphAuth", return_value=auth),
            patch("api.routes.schedule.ScheduleService", SyncOnlyScheduleService),
            self._client() as client,
        ):
            started = client.post("/schedule/auth/interactive-start")
            status = self._wait_for_auth_status(client)
            legacy_status = client.get("/schedule/auth/device-status")

        self.assertEqual(200, started.status_code, started.text)
        self.assertEqual({"status": "pending"}, started.json())
        self.assertEqual("ok", status["status"])
        self.assertEqual("person@example.com", status["account"]["username"])
        self.assertEqual(status, legacy_status.json())
        self.assertEqual([True], sync_calls)
        self.assertTrue((db.WORKSPACE_DIR / "graph_token_cache.json").exists())

    def test_interactive_failure_marks_device_code_fallback(self) -> None:
        class FailingInteractiveAuth:
            def complete_interactive_flow(self, *, exit_condition=None):
                del exit_condition
                raise GraphAuthError("AADSTS50011: redirect URI mismatch")

        with (
            patch("api.routes.schedule.GraphAuth", return_value=FailingInteractiveAuth()),
            self._client() as client,
        ):
            started = client.post("/schedule/auth/interactive-start")
            status = self._wait_for_auth_status(client)

        self.assertEqual(200, started.status_code)
        self.assertEqual("error", status["status"])
        self.assertIn("AADSTS50011", status["error"])
        self.assertEqual("device_code", status["fallback"])

    def test_interactive_and_device_flows_are_mutually_exclusive(self) -> None:
        interactive_started = threading.Event()
        interactive_release = threading.Event()
        device_started = threading.Event()
        device_release = threading.Event()

        class BlockingAuth:
            def complete_interactive_flow(self, *, exit_condition=None):
                interactive_started.set()
                self._wait(interactive_release, exit_condition)
                return {"username": "interactive@example.com"}

            def start_device_flow(self):
                return {
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 900,
                }

            def complete_device_flow(self, flow, *, exit_condition=None):
                del flow
                device_started.set()
                self._wait(device_release, exit_condition)
                return {"username": "device@example.com"}

            @staticmethod
            def _wait(release, exit_condition):
                while not release.wait(0.01):
                    if exit_condition is not None and exit_condition():
                        raise GraphAuthError("登录已取消")

        class SyncOnlyScheduleService:
            def __init__(self, *, auth=None):
                self.auth = auth

            def sync(self):
                return {"ok": True}

        with (
            patch("api.routes.schedule.GraphAuth", return_value=BlockingAuth()),
            patch("api.routes.schedule.ScheduleService", SyncOnlyScheduleService),
            self._client() as client,
        ):
            self.assertEqual(200, client.post("/schedule/auth/interactive-start").status_code)
            self.assertTrue(interactive_started.wait(1.0))
            device_conflict = client.post("/schedule/auth/device-start")
            interactive_release.set()
            self._wait_for_auth_status(client)

            self.assertEqual(200, client.post("/schedule/auth/device-start").status_code)
            self.assertTrue(device_started.wait(1.0))
            interactive_conflict = client.post("/schedule/auth/interactive-start")
            device_release.set()
            self._wait_for_auth_status(client)

        self.assertEqual(409, device_conflict.status_code)
        self.assertEqual(409, interactive_conflict.status_code)

    @staticmethod
    def _wait_for_auth_status(client, timeout: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get("/schedule/auth/status")
            if response.json().get("status") != "pending":
                return response.json()
            time.sleep(0.01)
        raise AssertionError("登录状态在测试超时前仍为 pending")

    def test_create_event_goal_id_links_the_created_event(self) -> None:
        class ConnectedAuth:
            def client_id(self):
                return "client-id"

            def get_access_token(self):
                return "access-token"

        class CreatingGraph:
            def create_event(self, payload):
                return {
                    "id": "created-api",
                    "subject": payload["subject"],
                    "start": payload["start"],
                    "end": payload["end"],
                    "isAllDay": payload["isAllDay"],
                }

        goal = goal_service.create_goal("完成 P4", None, "short")
        service = ScheduleService(
            auth=ConnectedAuth(),
            graph_factory=lambda token_provider: CreatingGraph(),
            clock=lambda: 1.0,
        )

        with patch("api.routes.schedule.ScheduleService", return_value=service):
            with self._client() as client:
                response = client.post(
                    "/schedule/events",
                    json={
                        "subject": "API 创建日程",
                        "date": "2026-07-16",
                        "goal_id": goal["id"],
                    },
                )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            [{"goal_id": goal["id"], "goal_title": "完成 P4"}],
            response.json()["goal_links"],
        )


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
