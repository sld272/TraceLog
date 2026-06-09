from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.app_services.public_post_pipeline import CreatedPost


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed")
class ApiPostsTest(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from api import deps
        from api.app import create_app

        async def fake_init_runtime():
            deps._runtime = SimpleNamespace(  # type: ignore[attr-defined]
                configured=True,
                client=object(),
                model="test-model",
                vectorstore_initialized=False,
                worker=SimpleNamespace(),
            )
            return deps._runtime

        async def fake_shutdown_runtime():
            deps._runtime = None  # type: ignore[attr-defined]

        self.init_patch = patch("api.deps.init_runtime", fake_init_runtime)
        self.shutdown_patch = patch("api.deps.shutdown_runtime", fake_shutdown_runtime)
        self.init_patch.start()
        self.shutdown_patch.start()
        self.addCleanup(self.init_patch.stop)
        self.addCleanup(self.shutdown_patch.stop)
        return TestClient(create_app())

    def test_post_posts_returns_queued(self) -> None:
        with patch(
            "api.routes.posts.public_post_pipeline.create_post",
            return_value=CreatedPost(post_id="20260531-001", job_ids=[1, 2]),
        ):
            with self._client() as client:
                response = client.post("/posts", json={"content": "今天想练歌"})

        self.assertEqual(200, response.status_code)
        self.assertEqual({"post_id": "20260531-001", "status": "queued", "job_ids": [1, 2]}, response.json())

    def test_post_posts_rejects_blank_content(self) -> None:
        with self._client() as client:
            response = client.post("/posts", json={"content": "   "})

        self.assertEqual(422, response.status_code)

    def test_post_posts_requires_model_configuration(self) -> None:
        from api import deps

        with self._client() as client:
            deps._runtime.configured = False  # type: ignore[attr-defined]
            deps._runtime.client = None  # type: ignore[attr-defined]
            deps._runtime.model = None  # type: ignore[attr-defined]
            response = client.post("/posts", json={"content": "今天想练歌"})

        self.assertEqual(409, response.status_code)
        self.assertIn("请先在设置页完成模型配置", response.json()["detail"])

    def test_sse_event_format_includes_id_event_and_payload(self) -> None:
        from api.routes.posts import _format_sse

        payload = _format_sse(
            {
                "id": 7,
                "post_id": "p-1",
                "job_id": 3,
                "event_type": "reply_succeeded",
                "payload": {"soul_name": "默认"},
                "created_at": 1.0,
            }
        )

        self.assertIn("id: 7\n", payload)
        self.assertIn("event: reply_succeeded\n", payload)
        self.assertIn('"soul_name": "默认"', payload)
        self.assertTrue(payload.endswith("\n\n"))

    def test_api_config_missing_file_fails_clearly(self) -> None:
        from api import deps

        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "config.json")
            with patch("api.deps.CONFIG_FILE", missing):
                with self.assertRaisesRegex(RuntimeError, "API 模式需要先配置"):
                    deps._load_api_config()

                config = deps._load_api_config(strict=False)
                self.assertFalse(deps._is_model_configured(config))

    def test_job_worker_concurrency_is_bounded(self) -> None:
        from api import deps

        self.assertEqual(1, deps._job_worker_concurrency({"job_worker_concurrency": 0}))
        self.assertEqual(3, deps._job_worker_concurrency({"job_worker_concurrency": "3"}))
        self.assertEqual(4, deps._job_worker_concurrency({"job_worker_concurrency": 99}))


if __name__ == "__main__":
    unittest.main()
