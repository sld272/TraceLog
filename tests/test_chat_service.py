from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import chat_service, db, profile_service, retrieval, soul_memory_service, soul_service, tool_config_service


class FakeClient:
    def __init__(self, payload: dict | None = None, content: str | None = None) -> None:
        self.payload = payload or {"reply": "收到，我陪你捋一下。", "todos_to_upsert": [], "todos_to_delete": []}
        self.content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        content = self.content if self.content is not None else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class ChatServiceTest(unittest.TestCase):
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

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        retrieval.hybrid_search = lambda query, k=3: []

        db.init_db()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与现状\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        retrieval.hybrid_search = self.old_hybrid_search
        self.tmp.cleanup()

    def test_get_or_create_thread_creates_and_reuses_enabled_soul_thread(self) -> None:
        first = chat_service.get_or_create_thread("默认")
        second = chat_service.get_or_create_thread("默认")

        self.assertEqual(first.id, second.id)
        self.assertEqual("默认", first.soul_name)

    def test_disabled_soul_cannot_start_or_continue_chat(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        soul_service.disable_soul("默认")

        with self.assertRaises(ValueError):
            chat_service.get_or_create_thread("默认")
        with self.assertRaises(ValueError):
            chat_service.append_user_message(thread.id, "你好")

    def test_append_user_message_updates_thread_activity(self) -> None:
        thread = chat_service.get_or_create_thread("默认")

        message = chat_service.append_user_message(thread.id, "今天有点累")
        refreshed = chat_service.get_thread(thread.id)

        self.assertEqual("user", message.role)
        self.assertEqual("今天有点累", message.content)
        self.assertIsNotNone(refreshed.last_message_at)

    def test_list_chat_threads_orders_by_recent_activity(self) -> None:
        first = chat_service.get_or_create_thread("默认")
        soul_service.create_soul("测试好友", description="测试描述")
        second = chat_service.get_or_create_thread("测试好友")
        chat_service.append_user_message(first.id, "第一条")
        chat_service.append_user_message(second.id, "第二条")

        threads = chat_service.list_chat_threads()

        self.assertEqual(["测试好友", "默认"], [thread.soul_name for thread in threads])

    def test_build_chat_context_contains_profile_messages_todos_posts_and_comments(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        chat_service.append_user_message(thread.id, "聊聊考试")
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("20260525-001", "2026-05-25T00:00:00+08:00", "考试压力很大", 1.0, 1.0),
        )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("20260525-001", "默认", "你之前也提到过考试压力。", 1.0),
        )
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "复习数学", "未完成", 1.0, 1.0),
        )
        retrieval.hybrid_search = lambda query, k=3: ["20260525-001"]

        context = chat_service.build_chat_context(thread.id, "考试怎么办")

        self.assertIn("你是 TraceLog 默认的 AI 好友", context.soul.persona)
        self.assertIn("# 默认的相处记忆", context.soul.soul_memory)
        self.assertIn("测试用户", context.context)
        self.assertIn("考试压力很大", context.context)
        self.assertIn("你之前也提到过考试压力", context.context)
        self.assertIn("复习数学", context.context)
        self.assertIn("聊聊考试", context.context)

    def test_chat_reply_success_writes_assistant_message(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        client = FakeClient({"reply": "先睡一下也行。", "todos_to_upsert": [], "todos_to_delete": []})

        result = chat_service.call_chat_reply(thread.id, "我好累", client, "fake-model")
        messages = chat_service.list_thread_messages(thread.id)

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.assistant_message_id)
        self.assertEqual(["user", "assistant"], [message.role for message in messages])
        self.assertEqual("先睡一下也行。", messages[-1].content)

    def test_chat_reply_failure_preserves_user_message_only(self) -> None:
        thread = chat_service.get_or_create_thread("默认")

        with patch("core.chat_service.reply_router.call_soul_chat_reply", return_value=None):
            result = chat_service.call_chat_reply(thread.id, "我好累", FakeClient(), "fake-model")
        messages = chat_service.list_thread_messages(thread.id)

        self.assertFalse(result.ok)
        self.assertIsNone(result.assistant_message_id)
        self.assertEqual(["user"], [message.role for message in messages])

    def test_private_chat_does_not_write_posts(self) -> None:
        thread = chat_service.get_or_create_thread("默认")

        chat_service.append_user_message(thread.id, "这是一条私聊")

        row = db.query_one("SELECT COUNT(*) AS count FROM posts")
        self.assertEqual(0, row["count"])

    def test_private_chat_reply_does_not_write_reflection_or_profile_revisions(self) -> None:
        thread = chat_service.get_or_create_thread("默认")

        chat_service.call_chat_reply(thread.id, "这是一条私聊回复", FakeClient(), "fake-model")

        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM entities")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM emotions")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM events")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM relations")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM user_md_revisions")["count"])
        rows = db.query_all("SELECT source FROM soul_memory_revisions WHERE source != 'system'")
        self.assertEqual([], rows)

    def test_private_chat_reply_ignores_todo_fields(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        client = FakeClient(
            {
                "reply": "我记下来了。",
                "todos_to_upsert": [
                    {
                        "id": None,
                        "task": "明天交作业",
                        "date": "2026-05-26",
                        "start_time": None,
                        "end_time": None,
                        "status": "未完成",
                    }
                ],
                "todos_to_delete": [],
            }
        )

        result = chat_service.call_chat_reply(thread.id, "提醒我明天交作业", client, "fake-model")
        row = db.query_one("SELECT COUNT(*) AS count FROM todos")

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.assistant_message_id)
        self.assertEqual(0, row["count"])

    def test_build_chat_context_omits_todos_when_tool_disabled(self) -> None:
        tool_config_service.set_tool_enabled("todo", False)
        thread = chat_service.get_or_create_thread("默认")
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "复习数学", "未完成", 1.0, 1.0),
        )

        context = chat_service.build_chat_context(thread.id, "考试怎么办")

        self.assertNotIn("复习数学", context.context)
        self.assertNotIn("# 待办事项", context.context)


if __name__ == "__main__":
    unittest.main()
