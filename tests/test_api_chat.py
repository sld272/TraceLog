from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import suggestion_pipeline
from tests.helpers import FakeStreamingClient


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed")
class ApiChatStreamTest(unittest.TestCase):
    def setUp(self) -> None:
        # keep the suggestion pipeline off so it doesn't drive the fake client
        suggestions_off = patch.dict(
            os.environ,
            {suggestion_pipeline.GOAL_SUGGESTIONS_ENABLED_ENV: "0"},
        )
        suggestions_off.start()
        self.addCleanup(suggestions_off.stop)

    @contextmanager
    def _temp_db(self):
        from core import db, soul_service

        with tempfile.TemporaryDirectory() as tmp:
            old_workspace = db.WORKSPACE_DIR
            old_db_path = db.DB_PATH
            old_souls_dir = soul_service.SOULS_DIR
            workspace = Path(tmp) / "workspace"
            db.WORKSPACE_DIR = workspace
            db.DB_PATH = workspace / "state.db"
            soul_service.SOULS_DIR = workspace / "souls"
            try:
                db.init_db()
                workspace.mkdir(parents=True, exist_ok=True)
                soul_service.sync_souls()
                yield
            finally:
                db.WORKSPACE_DIR = old_workspace
                db.DB_PATH = old_db_path
                soul_service.SOULS_DIR = old_souls_dir

    def _client(self, fake_client):
        from fastapi.testclient import TestClient
        from api import deps
        from api.app import create_app

        async def fake_init_runtime():
            deps._runtime = SimpleNamespace(  # type: ignore[attr-defined]
                configured=True,
                client=fake_client,
                model="test-model",
                vectorstore_initialized=False,
                worker=SimpleNamespace(),
            )
            return deps._runtime

        async def fake_shutdown_runtime():
            deps._runtime = None  # type: ignore[attr-defined]

        init_patch = patch("api.deps.init_runtime", fake_init_runtime)
        shutdown_patch = patch("api.deps.shutdown_runtime", fake_shutdown_runtime)
        init_patch.start()
        shutdown_patch.start()
        self.addCleanup(init_patch.stop)
        self.addCleanup(shutdown_patch.stop)
        return TestClient(create_app())

    def test_stream_endpoint_emits_delta_then_done_frames(self) -> None:
        with self._temp_db():
            stub = FakeStreamingClient(["你好", "呀"])
            with self._client(stub) as client:
                with client.stream(
                    "POST", "/chat/拾迹者/messages/stream", json={"content": "在吗"}
                ) as response:
                    status_code = response.status_code
                    body = "".join(response.iter_text())

        self.assertEqual(200, status_code)
        self.assertIn("event: delta", body)
        self.assertIn('"text": "你好"', body)
        self.assertIn('"text": "呀"', body)
        self.assertIn("event: done", body)
        self.assertIn('"ok": true', body)
        self.assertIn('"reply": "你好呀"', body)
        # done is the terminal frame, after every delta
        self.assertLess(body.index("event: delta"), body.index("event: done"))
        self.assertTrue(body.rstrip().endswith("\n\n") or body.endswith("\n\n"))

    def test_non_stream_fallback_reuses_completed_stream_request(self) -> None:
        with self._temp_db():
            stub = FakeStreamingClient(["你好", "呀"])
            payload = {"content": "在吗", "request_id": "browser-turn-1"}
            with self._client(stub) as client:
                with client.stream(
                    "POST", "/chat/拾迹者/messages/stream", json=payload
                ) as response:
                    self.assertEqual(200, response.status_code)
                    self.assertIn("event: done", "".join(response.iter_text()))
                calls_after_stream = len(stub.calls)
                fallback = client.post("/chat/拾迹者/messages", json=payload)

        self.assertEqual(200, fallback.status_code)
        data = fallback.json()
        self.assertEqual(calls_after_stream, len(stub.calls))
        self.assertEqual(["user", "assistant"], [item["role"] for item in data["messages"]])
        self.assertEqual("你好呀", data["result"]["reply"])

    def test_stream_endpoint_rejects_blank_content(self) -> None:
        with self._temp_db():
            with self._client(FakeStreamingClient()) as client:
                response = client.post("/chat/拾迹者/messages/stream", json={"content": "   "})

        self.assertEqual(422, response.status_code)

    def test_stream_endpoint_requires_model_configuration(self) -> None:
        from api import deps

        with self._temp_db():
            with self._client(FakeStreamingClient()) as client:
                deps._runtime.configured = False  # type: ignore[attr-defined]
                deps._runtime.client = None  # type: ignore[attr-defined]
                deps._runtime.model = None  # type: ignore[attr-defined]
                response = client.post("/chat/拾迹者/messages/stream", json={"content": "在吗"})

        self.assertEqual(409, response.status_code)


if __name__ == "__main__":
    unittest.main()
