from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from core import db, logging_service, profile_service, retrieval, soul_memory_service, soul_service
from core.app_services import job_service


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed")
class ApiManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_user_md_path = profile_service.USER_MD_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR
        self.old_hybrid_search = retrieval.hybrid_search
        self.config_path = Path(self.tmp.name) / "config.json"

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        retrieval.hybrid_search = lambda *args, **kwargs: []

        db.init_db()
        logging_service.init_logging({"enabled": False})
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与角色\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()
        self.config_path.write_text(
            json.dumps(
                {
                    "api_key": "sk-test-secret-123456",
                    "base_url": "https://example.invalid/v1",
                    "model": "test-model",
                    "embedding_model": "test-embedding",
                    "logging": {"enabled": False, "level": "INFO", "history_retention": 3},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        retrieval.hybrid_search = self.old_hybrid_search
        self.tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from api import deps
        from api.app import create_app

        async def fake_init_runtime():
            deps._runtime = SimpleNamespace(  # type: ignore[attr-defined]
                config={},
                client=None,
                model=None,
                vectorstore_initialized=False,
                worker=SimpleNamespace(),
            )
            return deps._runtime

        async def fake_shutdown_runtime():
            deps._runtime = None  # type: ignore[attr-defined]

        self.init_patch = patch("api.deps.init_runtime", fake_init_runtime)
        self.shutdown_patch = patch("api.deps.shutdown_runtime", fake_shutdown_runtime)
        self.config_patch = patch("api.routes.settings.CONFIG_FILE", str(self.config_path))
        self.init_patch.start()
        self.shutdown_patch.start()
        self.config_patch.start()
        self.addCleanup(self.init_patch.stop)
        self.addCleanup(self.shutdown_patch.stop)
        self.addCleanup(self.config_patch.stop)
        return TestClient(create_app())

    def test_profile_routes_read_update_and_list_revisions(self) -> None:
        new_profile = "# 用户档案\n\n## 身份与角色\nAPI 测试用户\n"

        with self._client() as client:
            get_response = client.get("/profile")
            put_response = client.put("/profile", json={"content": new_profile})
            revisions_response = client.get("/profile/revisions")

        self.assertEqual(200, get_response.status_code)
        self.assertIn("测试用户", get_response.json()["content"])
        self.assertEqual(200, put_response.status_code)
        self.assertIn("API 测试用户", put_response.json()["content"])
        self.assertEqual(200, revisions_response.status_code)
        self.assertGreaterEqual(len(revisions_response.json()), 1)

    def test_soul_routes_create_patch_and_edit_memory(self) -> None:
        name = quote("测试好友")
        memory = "# 测试好友的相处记忆\n\n## 对用户的理解\nAPI 里写入的记忆\n"

        with self._client() as client:
            create_response = client.post("/souls", json={"name": "测试好友", "description": "测试描述"})
            patch_response = client.patch(f"/souls/{name}", json={"enabled": False})
            memory_response = client.put(f"/souls/{name}/memory", json={"content": memory})
            list_response = client.get("/souls")

        self.assertEqual(200, create_response.status_code)
        self.assertEqual("测试好友", create_response.json()["name"])
        self.assertEqual(200, patch_response.status_code)
        self.assertFalse(patch_response.json()["enabled"])
        self.assertEqual(200, memory_response.status_code)
        self.assertIn("API 里写入的记忆", memory_response.json()["content"])
        self.assertIn("测试好友", [item["name"] for item in list_response.json()])

    def test_settings_routes_read_save_config_and_workspace_status(self) -> None:
        with self._client() as client:
            get_response = client.get("/settings/model")
            put_response = client.put(
                "/settings/model",
                json={
                    "api_key": "",
                    "base_url": "https://updated.invalid/v1",
                    "model": "updated-model",
                    "embedding_model": "updated-embedding",
                    "reuse_embedding_config": False,
                    "embedding_base_url": "https://embeddings.invalid/v1",
                    "job_worker_concurrency": 2,
                    "logging": {"enabled": True, "level": "DEBUG", "history_retention": 7},
                },
            )
            workspace_response = client.get("/settings/workspace")

        self.assertEqual(200, get_response.status_code)
        self.assertTrue(get_response.json()["has_api_key"])
        self.assertNotIn("sk-test-secret", json.dumps(get_response.json()))

        self.assertEqual(200, put_response.status_code)
        updated = put_response.json()
        self.assertEqual("updated-model", updated["model"])
        self.assertTrue(updated["restart_required"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("sk-test-secret-123456", saved["api_key"])
        self.assertEqual("https://updated.invalid/v1", saved["base_url"])
        self.assertEqual(2, saved["job_worker_concurrency"])

        self.assertEqual(200, workspace_response.status_code)
        status = workspace_response.json()
        self.assertTrue(status["db_exists"])
        self.assertGreaterEqual(status["counts"]["souls"], 1)

    def test_todo_routes_list_and_patch_status(self) -> None:
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "复习数学", "未完成", 1.0, 1.0),
        )

        with self._client() as client:
            list_response = client.get("/todos")
            patch_response = client.patch("/todos/todo-1", json={"status": "已完成"})

        self.assertEqual(200, list_response.status_code)
        self.assertEqual("复习数学", list_response.json()[0]["task"])
        self.assertEqual(200, patch_response.status_code)
        self.assertEqual("已完成", patch_response.json()["status"])

    def test_manual_reflection_routes_enqueue_jobs(self) -> None:
        with self._client() as client:
            global_response = client.post("/reflections/global", json={"limit": 5})
            souls_response = client.post("/reflections/souls", json={"limit_per_soul": 5})
            jobs_response = client.get("/jobs")

        self.assertEqual(200, global_response.status_code)
        self.assertEqual(200, souls_response.status_code)
        self.assertEqual(
            ["trigger_soul_deep_reflections", "trigger_global_deep_reflection"],
            [job["type"] for job in jobs_response.json()],
        )

    def test_job_routes_cancel_pending_and_retry_failed_jobs(self) -> None:
        pending_id = job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": "p-1"})
        failed_id = job_service.enqueue(job_service.TYPE_RUN_LIGHT_REFLECTION, {"post_id": "p-1"})
        job_service.mark_failed(failed_id, "boom")

        with self._client() as client:
            cancel_response = client.post(f"/jobs/{pending_id}/cancel")
            retry_response = client.post(f"/jobs/{failed_id}/retry")

        self.assertEqual(200, cancel_response.status_code)
        self.assertEqual("cancelled", cancel_response.json()["status"])
        self.assertEqual(200, retry_response.status_code)
        self.assertNotEqual(failed_id, retry_response.json()["job_id"])

    def test_chat_route_sends_message_and_persists_reply(self) -> None:
        soul_name = quote("默认")

        with patch("core.chat_service.reply_router.call_soul_chat_reply", return_value={"reply": "我在。"}):
            with self._client() as client:
                response = client.post(f"/chat/{soul_name}/messages", json={"content": "今天有点累"})

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertTrue(data["result"]["ok"])
        self.assertEqual(["user", "assistant"], [message["role"] for message in data["messages"]])
        self.assertEqual("我在。", data["result"]["reply"])

    def test_chat_message_sse_format(self) -> None:
        from api.routes.chat import _format_message_sse

        payload = _format_message_sse(
            {"id": 1, "thread_id": 2, "role": "assistant", "content": "我在。", "created_at": 1.0}
        )

        self.assertIn("id: 1\n", payload)
        self.assertIn("event: chat_message\n", payload)
        self.assertIn('"content": "我在。"', payload)

    def test_comment_route_sends_message_to_selected_soul_conversation(self) -> None:
        post_id = "20260531-001"
        now = db.now_ts()
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-31T12:00:00+08:00", "今天想练歌", now, now),
        )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (post_id, "默认", "我陪你练。", now),
        )

        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value={"reply": "继续说。"}):
            with self._client() as client:
                response = client.post(
                    f"/comments/posts/{post_id}/souls/{quote('默认')}/messages",
                    json={"content": "卡在副歌了"},
                )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertTrue(data["result"]["ok"])
        self.assertEqual(post_id, data["conversation"]["post_id"])
        self.assertEqual(["assistant", "user", "assistant"], [message["role"] for message in data["messages"]])
        self.assertEqual([0, 1, 2], [message["seq"] for message in data["messages"]])

    def test_comment_message_sse_format(self) -> None:
        from api.routes.comments import _format_message_sse

        payload = _format_message_sse(
            {
                "id": 1,
                "post_id": "p-1",
                "soul_name": "默认",
                "role": "assistant",
                "content": "继续说。",
                "seq": 2,
                "created_at": 1.0,
            }
        )

        self.assertIn("id: 1\n", payload)
        self.assertIn("event: comment_message\n", payload)
        self.assertIn('"content": "继续说。"', payload)


if __name__ == "__main__":
    unittest.main()
