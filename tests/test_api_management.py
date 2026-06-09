from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from PIL import Image

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
                configured=True,
                client=object(),
                model="test-model",
                vectorstore_initialized=False,
                worker=SimpleNamespace(),
            )
            return deps._runtime

        async def fake_shutdown_runtime():
            deps._runtime = None  # type: ignore[attr-defined]

        async def fake_reload_runtime():
            deps._runtime = SimpleNamespace(  # type: ignore[attr-defined]
                config={},
                configured=True,
                client=object(),
                model="updated-model",
                vectorstore_initialized=False,
                worker=SimpleNamespace(),
            )
            return deps._runtime

        self.init_patch = patch("api.deps.init_runtime", fake_init_runtime)
        self.shutdown_patch = patch("api.deps.shutdown_runtime", fake_shutdown_runtime)
        self.reload_patch = patch("api.deps.reload_runtime", fake_reload_runtime)
        self.config_patch = patch("api.routes.settings.CONFIG_FILE", str(self.config_path))
        self.init_patch.start()
        self.shutdown_patch.start()
        self.reload_patch.start()
        self.config_patch.start()
        self.addCleanup(self.init_patch.stop)
        self.addCleanup(self.shutdown_patch.stop)
        self.addCleanup(self.reload_patch.stop)
        self.addCleanup(self.config_patch.stop)
        return TestClient(create_app())

    def test_profile_routes_read_update_and_list_revisions(self) -> None:
        new_profile = "# 用户档案\n\n## 身份与角色\nAPI 测试用户\n"

        with self._client() as client:
            get_response = client.get("/profile")
            put_response = client.put("/profile", json={"content": new_profile})
            revisions_response = client.get("/profile/revisions")
            revision_id = revisions_response.json()[0]["id"]
            revision_response = client.get(f"/profile/revisions/{revision_id}")

        self.assertEqual(200, get_response.status_code)
        self.assertIn("测试用户", get_response.json()["content"])
        self.assertEqual(200, put_response.status_code)
        self.assertIn("API 测试用户", put_response.json()["content"])
        self.assertEqual(200, revisions_response.status_code)
        self.assertGreaterEqual(len(revisions_response.json()), 1)
        self.assertEqual(200, revision_response.status_code)
        self.assertEqual(new_profile, revision_response.json()["snapshot"])

    def test_soul_routes_create_patch_and_edit_memory(self) -> None:
        name = quote("测试好友")
        memory = "# 测试好友的相处记忆\n\n## 对用户的理解\nAPI 里写入的记忆\n"

        with self._client() as client:
            create_response = client.post("/souls", json={"name": "测试好友", "description": "测试描述"})
            patch_response = client.patch(f"/souls/{name}", json={"enabled": False})
            get_memory_response = client.get(f"/souls/{name}/memory")
            memory_response = client.put(f"/souls/{name}/memory", json={"content": memory})
            revisions_response = client.get(f"/souls/{name}/memory/revisions")
            revision_id = revisions_response.json()[0]["id"]
            revision_response = client.get(f"/souls/{name}/memory/revisions/{revision_id}")
            list_response = client.get("/souls")

        self.assertEqual(200, create_response.status_code)
        self.assertEqual("测试好友", create_response.json()["name"])
        self.assertEqual(200, patch_response.status_code)
        self.assertFalse(patch_response.json()["enabled"])
        self.assertEqual(200, get_memory_response.status_code)
        self.assertIn("# 测试好友的相处记忆", get_memory_response.json()["content"])
        self.assertEqual(200, memory_response.status_code)
        self.assertIn("API 里写入的记忆", memory_response.json()["content"])
        self.assertEqual(200, revisions_response.status_code)
        self.assertGreaterEqual(len(revisions_response.json()), 1)
        self.assertEqual(200, revision_response.status_code)
        self.assertEqual(memory, revision_response.json()["snapshot"])
        self.assertIn("测试好友", [item["name"] for item in list_response.json()])

    def test_upload_attachment_and_create_image_only_post(self) -> None:
        with self._client() as client:
            upload_response = client.post(
                "/attachments/upload",
                files={"file": ("photo.png", _image_bytes(), "image/png")},
            )
            attachment_id = upload_response.json()["id"]
            create_response = client.post("/posts", json={"content": "", "attachment_ids": [attachment_id]})
            list_response = client.get("/posts")

        self.assertEqual(200, upload_response.status_code)
        self.assertEqual("image/png", upload_response.json()["mime_type"])
        self.assertEqual(200, create_response.status_code)
        post = list_response.json()[0]
        self.assertEqual("", post["content"])
        self.assertEqual([attachment_id], [attachment["id"] for attachment in post["attachments"]])

    def test_upload_attachment_accepts_uppercase_jpg_with_generic_mime_type(self) -> None:
        with self._client() as client:
            upload_response = client.post(
                "/attachments/upload",
                files={"file": ("PHOTO.JPG", _image_bytes("JPEG"), "application/octet-stream")},
            )

        self.assertEqual(200, upload_response.status_code, upload_response.text)
        self.assertEqual("image/jpeg", upload_response.json()["mime_type"])
        self.assertEqual("PHOTO.JPG", upload_response.json()["original_filename"])
        self.assertTrue(upload_response.json()["file_path"].endswith(".jpg"))

    def test_upload_attachment_accepts_first_file_even_when_field_name_varies(self) -> None:
        with self._client() as client:
            upload_response = client.post(
                "/attachments/upload",
                files={"upload": ("PHOTO.JPG", _image_bytes("JPEG"), "application/octet-stream")},
            )

        self.assertEqual(200, upload_response.status_code, upload_response.text)
        self.assertEqual("image/jpeg", upload_response.json()["mime_type"])
        self.assertEqual("PHOTO.JPG", upload_response.json()["original_filename"])

    def test_upload_attachment_returns_readable_error_when_file_is_missing(self) -> None:
        with self._client() as client:
            upload_response = client.post("/attachments/upload", data={"file": "PHOTO.JPG"})

        self.assertEqual(400, upload_response.status_code)
        self.assertEqual("没有找到上传图片文件", upload_response.json()["detail"])

    def test_upload_attachment_compresses_large_image(self) -> None:
        with self._client() as client:
            upload_response = client.post(
                "/attachments/upload",
                files={"file": ("large.jpg", _noisy_image_bytes("JPEG", (3000, 2200)), "image/jpeg")},
            )

        self.assertEqual(200, upload_response.status_code)
        self.assertEqual("image/jpeg", upload_response.json()["mime_type"])
        self.assertLessEqual(upload_response.json()["file_size"], 5 * 1024 * 1024)

    def test_generate_soul_route_returns_markdown(self) -> None:
        generated = {
            "soul": "---\nname: 测试好友\nversion: 1\ndescription: 测试\n---\n\n# 测试好友\n\n## 人格定位\n测试",
        }

        with patch("core.llm.soul_router.generate_soul", return_value=generated):
            with self._client() as client:
                response = client.post(
                    "/souls/generate-soul",
                    json={"name": "测试好友", "inspiration": "温柔但不纵容"},
                )

        self.assertEqual(200, response.status_code)
        self.assertIn("## 人格定位", response.json()["soul"])

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
                    "logging": {"enabled": True, "level": "DEBUG", "history_retention": 7},
                    "vision": {
                        "enabled": True,
                        "model": "vision-model",
                        "api_key": "",
                        "base_url": "",
                    },
                    "web_search": {
                        "enabled": True,
                        "provider": "tavily",
                        "tavily_api_key": "tavily-secret",
                        "max_results": 6,
                        "timeout_s": 9,
                        "cache_ttl_s": 600,
                    },
                },
            )
            workspace_response = client.get("/settings/workspace")

        self.assertEqual(200, get_response.status_code)
        self.assertTrue(get_response.json()["has_api_key"])
        self.assertNotIn("sk-test-secret", json.dumps(get_response.json()))

        self.assertEqual(200, put_response.status_code)
        updated = put_response.json()
        self.assertEqual("updated-model", updated["model"])
        self.assertFalse(updated["restart_required"])
        self.assertTrue(updated["runtime_reloaded"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("sk-test-secret-123456", saved["api_key"])
        self.assertEqual("https://updated.invalid/v1", saved["base_url"])
        self.assertNotIn("job_worker_concurrency", saved)
        self.assertEqual({"enabled": True, "model": "vision-model", "api_key": None, "base_url": None}, saved["vision"])
        self.assertEqual(
            {
                "enabled": True,
                "provider": "tavily",
                "tavily_api_key": "tavily-secret",
                "max_results": 6,
                "timeout_s": 9,
                "cache_ttl_s": 600,
            },
            saved["web_search"],
        )
        self.assertNotIn("tavily-secret", json.dumps(updated))

        self.assertEqual(200, workspace_response.status_code)
        status = workspace_response.json()
        self.assertTrue(status["db_exists"])
        self.assertGreaterEqual(status["counts"]["souls"], 1)
        self.assertIn("vision_cache", status["counts"])
        self.assertIn("web_search", status)
        self.assertIn("vector_index", status)
        self.assertIn("source_revision", status["vector_index"])

    def test_todo_routes_list_and_patch_status(self) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("post-1", "2026-06-04T00:00:00+00:00", "来源记录", 1.0, 1.0),
        )
        db.execute(
            """
            INSERT INTO todos(id, task, status, source_post, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("todo-1", "复习数学", "未完成", "post-1", 1.0, 1.0),
        )

        with self._client() as client:
            list_response = client.get("/todos")
            patch_response = client.patch("/todos/todo-1", json={"status": "已完成"})
            create_response = client.post(
                "/todos",
                json={
                    "task": "手动整理错题",
                    "date": "2026-06-05",
                    "start_time": "20:00",
                    "end_time": "",
                },
            )
            delete_response = client.delete("/todos/todo-1")
            list_after_delete_response = client.get("/todos")

        self.assertEqual(200, list_response.status_code)
        self.assertEqual("复习数学", list_response.json()[0]["task"])
        self.assertEqual("post-1", list_response.json()[0]["source_post"])
        self.assertEqual(200, patch_response.status_code)
        self.assertEqual("已完成", patch_response.json()["status"])
        self.assertIsNotNone(patch_response.json()["completed_at"])

        self.assertEqual(200, create_response.status_code)
        created = create_response.json()
        self.assertTrue(created["id"].startswith("manual-"))
        self.assertEqual("手动整理错题", created["task"])
        self.assertEqual("2026-06-05", created["date"])
        self.assertEqual("20:00", created["start_time"])
        self.assertIsNone(created["end_time"])
        self.assertEqual("未完成", created["status"])
        self.assertIsNone(created["source_post"])

        self.assertEqual(200, delete_response.status_code)
        self.assertEqual({"ok": True}, delete_response.json())
        self.assertEqual([created["id"]], [todo["id"] for todo in list_after_delete_response.json()])

    def test_todo_routes_validate_create_update_delete(self) -> None:
        with self._client() as client:
            empty_create = client.post("/todos", json={"task": " "})
            invalid_status = client.post("/todos", json={"task": "有效任务", "status": "doing"})
            missing_patch = client.patch("/todos/missing", json={"status": "已完成"})
            missing_delete = client.delete("/todos/missing")

        self.assertEqual(422, empty_create.status_code)
        self.assertEqual(422, invalid_status.status_code)
        self.assertEqual(404, missing_patch.status_code)
        self.assertEqual(404, missing_delete.status_code)

    def test_delete_post_route_hard_deletes_post_comments_and_cancels_pending_jobs(self) -> None:
        post_id = "post-delete-1"
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-06-05T12:00:00+08:00", "要删除的 post", 1.0, 1.0),
        )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
            VALUES (?, ?, 'assistant', ?, 0, ?)
            """,
            (post_id, "默认", "要一起删除的评论", 2.0),
        )
        job_id = job_service.enqueue(job_service.TYPE_GENERATE_POST_REPLIES, {"post_id": post_id})

        with self._client() as client:
            delete_response = client.delete(f"/posts/{post_id}")
            detail_response = client.get(f"/posts/{post_id}")

        self.assertEqual(200, delete_response.status_code, delete_response.text)
        self.assertEqual({"ok": True, "post_id": post_id, "deleted_comments": 1, "cancelled_jobs": 1}, delete_response.json())
        self.assertEqual(404, detail_response.status_code)
        self.assertIsNone(db.query_one("SELECT id FROM posts WHERE id = ?", (post_id,)))
        self.assertIsNone(db.query_one("SELECT id FROM comments WHERE post_id = ?", (post_id,)))
        job = job_service.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job_service.STATUS_CANCELLED, job["status"])

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

    def test_chat_edit_route_regenerates_assistant_reply(self) -> None:
        soul_name = quote("默认")

        with patch(
            "core.chat_service.reply_router.call_soul_chat_reply",
            side_effect=[{"reply": "旧回复"}, {"reply": "新回复"}],
        ):
            with self._client() as client:
                send_response = client.post(f"/chat/{soul_name}/messages", json={"content": "旧问题"})
                user_message_id = send_response.json()["messages"][0]["id"]
                edit_response = client.patch(f"/chat/messages/{user_message_id}", json={"content": "新问题"})

        self.assertEqual(200, edit_response.status_code, edit_response.text)
        data = edit_response.json()
        self.assertTrue(data["result"]["ok"])
        self.assertEqual("新回复", data["result"]["reply"])
        self.assertEqual(["user", "assistant"], [message["role"] for message in data["messages"]])
        self.assertEqual("新问题", data["messages"][0]["content"])
        self.assertEqual("新回复", data["messages"][1]["content"])

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


def _image_bytes(image_format: str = "PNG") -> bytes:
    image = Image.new("RGB", (10, 10), color=(50, 100, 150))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _noisy_image_bytes(image_format: str, size: tuple[int, int]) -> bytes:
    width, height = size
    image = Image.frombytes("RGB", size, os.urandom(width * height * 3))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
