from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.app_services.public_post_pipeline import CreatedPost


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed")
class ApiPostsTest(unittest.TestCase):
    @contextmanager
    def _temp_db(self):
        from core import db

        with tempfile.TemporaryDirectory() as tmp:
            old_workspace = db.WORKSPACE_DIR
            old_db_path = db.DB_PATH
            workspace = Path(tmp) / "workspace"
            db.WORKSPACE_DIR = workspace
            db.DB_PATH = workspace / "state.db"
            try:
                db.init_db()
                yield
            finally:
                db.WORKSPACE_DIR = old_workspace
                db.DB_PATH = old_db_path

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

    def test_patch_post_updates_content(self) -> None:
        from core.app_services.post_mutation import EditPostResult

        with patch(
            "api.routes.posts.post_mutation.edit_post",
            return_value=EditPostResult(
                post_id="p-1",
                content="修改后",
                updated_at=2.0,
            ),
        ):
            with self._client() as client:
                response = client.patch("/posts/p-1", json={"content": "修改后"})

        self.assertEqual(200, response.status_code)
        self.assertEqual("修改后", response.json()["content"])

    def test_search_posts_returns_wrapped_keyword_shape_in_retrieval_order(self) -> None:
        from core import db, retrieval

        with self._temp_db():
            db.execute(
                """
                INSERT INTO posts(id, ts, content, importance, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("p-1", "2026-06-01T10:00:00+08:00", "第一条 alpha", 0.4, 1.0, 1.0),
            )
            db.execute(
                """
                INSERT INTO posts(id, ts, content, importance, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("p-2", "2026-06-02T10:00:00+08:00", "第二条 alpha", 0.8, 2.0, 2.0),
            )
            db.execute(
                """
                INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("拾迹者", "/tmp/default.md", 1, 1, 1.0, 1.0),
            )
            db.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("p-2", "拾迹者", "assistant", "回应", 0, 3.0),
            )
            with patch(
                "api.routes.posts.retrieval.user_search_posts",
                return_value=retrieval.UserSearchResult(
                    [
                        retrieval.UserSearchHit("p-2", "keyword"),
                        retrieval.UserSearchHit("p-1", "keyword"),
                    ],
                    semantic_available=True,
                ),
            ) as search:
                with self._client() as client:
                    response = client.get("/posts/search?q=alpha&limit=2")

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("keyword", body["mode"])
        self.assertTrue(body["semantic_available"])
        self.assertEqual(["p-2", "p-1"], [item["post_id"] for item in body["items"]])
        self.assertEqual(["keyword", "keyword"], [item["match"] for item in body["items"]])
        self.assertEqual(1, body["items"][0]["comment_count"])
        self.assertIn("pipeline_status", body["items"][0])
        self.assertEqual([], body["items"][0]["attachments"])
        search.assert_called_once_with("alpha", k=2, semantic=False)

    def test_search_posts_hybrid_mode_returns_wrapped_shape(self) -> None:
        from core import db, retrieval

        with self._temp_db():
            db.execute(
                """
                INSERT INTO posts(id, ts, content, importance, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("p-1", "2026-06-01T10:00:00+08:00", "语义命中", 0.4, 1.0, 1.0),
            )
            with patch(
                "api.routes.posts.retrieval.user_search_posts",
                return_value=retrieval.UserSearchResult(
                    [retrieval.UserSearchHit("p-1", "semantic")],
                    semantic_available=True,
                ),
            ) as search:
                with self._client() as client:
                    response = client.get("/posts/search?q=低落&limit=2&mode=hybrid")

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("hybrid", body["mode"])
        self.assertTrue(body["semantic_available"])
        self.assertEqual(["p-1"], [item["post_id"] for item in body["items"]])
        self.assertEqual("semantic", body["items"][0]["match"])
        search.assert_called_once_with("低落", k=2, semantic=True)

    def test_search_posts_empty_query_is_422(self) -> None:
        with self._temp_db():
            with self._client() as client:
                response = client.get("/posts/search?q=")

        self.assertEqual(422, response.status_code)

    def test_search_route_does_not_fall_through_to_post_id(self) -> None:
        from core import retrieval

        with self._temp_db():
            with patch(
                "api.routes.posts.retrieval.user_search_posts",
                return_value=retrieval.UserSearchResult([], semantic_available=False),
            ):
                with self._client() as client:
                    response = client.get("/posts/search?q=missing")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"items": [], "semantic_available": False, "mode": "keyword"}, response.json())

    def test_search_posts_invalid_mode_is_422(self) -> None:
        with self._temp_db():
            with self._client() as client:
                response = client.get("/posts/search?q=alpha&mode=invalid")

        self.assertEqual(422, response.status_code)

    def test_list_posts_cursor_uses_timestamp_and_id_tiebreaker(self) -> None:
        from core import db

        with self._temp_db():
            for post_id, ts, created_at in [
                ("p-old", "2026-06-01T09:00:00+08:00", 1.0),
                ("p-a", "2026-06-01T10:00:00+08:00", 2.0),
                ("p-b", "2026-06-01T10:00:00+08:00", 3.0),
            ]:
                db.execute(
                    """
                    INSERT INTO posts(id, ts, content, importance, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (post_id, ts, post_id, 0.5, created_at, created_at),
                )

            with self._client() as client:
                first_page = client.get("/posts?limit=1")
                cursor_item = first_page.json()[0]
                second_page = client.get(
                    "/posts",
                    params={
                        "limit": 2,
                        "before_ts": cursor_item["ts"],
                        "before_id": cursor_item["post_id"],
                    },
                )

        self.assertEqual(200, first_page.status_code)
        self.assertEqual(["p-b"], [item["post_id"] for item in first_page.json()])
        self.assertEqual(200, second_page.status_code)
        self.assertEqual(["p-a", "p-old"], [item["post_id"] for item in second_page.json()])

    def test_list_posts_cursor_requires_complete_pair(self) -> None:
        with self._temp_db():
            with self._client() as client:
                response = client.get("/posts?before_ts=2026-06-01T10:00:00%2B08:00")

        self.assertEqual(422, response.status_code)

    def test_get_post_detail_orders_comments_by_post_soul_order_snapshot(self) -> None:
        from core import db

        with self._temp_db():
            db.execute(
                """
                INSERT INTO posts(id, ts, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("p-order", "2026-06-01T10:00:00+08:00", "排序测试", 1.0, 1.0),
            )
            for name, current_sort_order, snapshot_sort_order in [("拾迹者", 9, 0), ("毒舌好友", 0, 1)]:
                db.execute(
                    """
                    INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (name, f"souls/{name}.md", current_sort_order, 1.0, 1.0),
                )
                db.execute(
                    """
                    INSERT INTO post_soul_orders(post_id, soul_name, sort_order, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("p-order", name, snapshot_sort_order, 1.0),
                )
            db.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                VALUES (?, ?, 'assistant', ?, 0, ?)
                """,
                ("p-order", "毒舌好友", "先完成", 2.0),
            )
            db.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                VALUES (?, ?, 'assistant', ?, 0, ?)
                """,
                ("p-order", "拾迹者", "后完成", 3.0),
            )

            with self._client() as client:
                response = client.get("/posts/p-order")

        self.assertEqual(200, response.status_code)
        self.assertEqual(["拾迹者", "毒舌好友"], [item["soul_name"] for item in response.json()["comments"]])

    def test_sse_event_format_includes_id_event_and_payload(self) -> None:
        from api.routes.posts import _format_sse

        payload = _format_sse(
            {
                "id": 7,
                "post_id": "p-1",
                "job_id": 3,
                "event_type": "reply_succeeded",
                "payload": {"soul_name": "拾迹者"},
                "created_at": 1.0,
            }
        )

        self.assertIn("id: 7\n", payload)
        self.assertIn("event: reply_succeeded\n", payload)
        self.assertIn('"soul_name": "拾迹者"', payload)
        self.assertTrue(payload.endswith("\n\n"))

    def test_sse_query_after_id_skips_previous_events(self) -> None:
        from core import db
        from core.app_services import event_service

        with tempfile.TemporaryDirectory() as tmp:
            old_workspace = db.WORKSPACE_DIR
            old_db_path = db.DB_PATH
            workspace = Path(tmp) / "workspace"
            db.WORKSPACE_DIR = workspace
            db.DB_PATH = workspace / "state.db"
            try:
                db.init_db()
                db.execute(
                    """
                    INSERT INTO posts(id, ts, content, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("p-sse", "2026-06-09T10:00:00+08:00", "测试 SSE", 1.0, 1.0),
                )
                old_event_id = event_service.append_post_event("p-sse", "pipeline_done", {"old": True})
                event_service.append_post_event("p-sse", "reply_failed", {"error": "boom"})
                event_service.append_post_event("p-sse", "pipeline_done", {"new": True})

                with self._client() as client:
                    with client.stream("GET", f"/posts/p-sse/events?after_id={old_event_id}") as response:
                        status_code = response.status_code
                        body = "".join(response.iter_text())
            finally:
                db.WORKSPACE_DIR = old_workspace
                db.DB_PATH = old_db_path

        self.assertEqual(200, status_code)
        self.assertIn("event: reply_failed", body)
        self.assertIn('"error": "boom"', body)
        self.assertIn('"new": true', body)
        self.assertNotIn('"old": true', body)

    def test_sse_last_event_id_overrides_query_after_id(self) -> None:
        from api.routes import posts as post_routes

        captured: dict[str, object] = {}

        async def fake_run_sync(func, *args, **kwargs):
            if func is post_routes._post_exists:
                return True
            return func(*args, **kwargs)

        def fake_event_stream(post_id: str, after_id: int):
            captured["post_id"] = post_id
            captured["after_id"] = after_id

            async def empty_stream():
                if False:
                    yield ""

            return empty_stream()

        with (
            patch("api.routes.posts.run_sync", fake_run_sync),
            patch("api.routes.posts._event_stream", fake_event_stream),
        ):
            response = asyncio.run(
                post_routes.stream_post_events("p-sse", last_event_id="9", after_id=2)
            )

        self.assertEqual("text/event-stream", response.media_type)
        self.assertEqual({"post_id": "p-sse", "after_id": 9}, captured)

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
