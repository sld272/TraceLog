from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import comment_service, db, memory, soul_memory_service, soul_service, tool_config_service


class FakeClient:
    def __init__(self, payload: dict | None = None, content: str | None = None) -> None:
        self.payload = payload or {"reply": "我看到了，继续说。", "todos_to_upsert": [], "todos_to_delete": []}
        self.content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        content = self.content if self.content is not None else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class CommentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_memory_workspace = memory.WORKSPACE_DIR
        self.old_user_md_path = memory.USER_MD_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        memory.WORKSPACE_DIR = str(self.workspace)
        memory.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"

        db.init_db()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与现状\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()
        self._insert_post_and_comment("20260525-001", "默认", "我陪你继续拆。")
        self._insert_post_and_comment("20260525-001", "毒舌好友", "别装了，继续讲重点。")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        memory.WORKSPACE_DIR = self.old_memory_workspace
        memory.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        self.tmp.cleanup()

    def test_get_or_create_thread_is_per_post_and_soul(self) -> None:
        first = comment_service.get_or_create_thread("20260525-001", "默认")
        second = comment_service.get_or_create_thread("20260525-001", "默认")
        other = comment_service.get_or_create_thread("20260525-001", "毒舌好友")

        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.id, other.id)
        self.assertEqual("默认", first.soul_name)

    def test_comment_reply_only_writes_selected_soul_thread(self) -> None:
        thread = comment_service.get_or_create_thread("20260525-001", "默认")
        client = FakeClient({"reply": "好，我只在这里接住这句。", "todos_to_upsert": [], "todos_to_delete": []})

        result = comment_service.call_comment_reply(thread.id, "只回复默认", client, "fake-model")
        default_messages = comment_service.list_thread_messages(thread.id)
        other_thread = comment_service.get_or_create_thread("20260525-001", "毒舌好友")
        other_messages = comment_service.list_thread_messages(other_thread.id)

        self.assertTrue(result.ok)
        self.assertEqual("默认", result.soul_name)
        self.assertEqual(["user", "assistant"], [message.role for message in default_messages])
        self.assertEqual([], other_messages)

    def test_comment_reply_failure_preserves_user_message_only(self) -> None:
        thread = comment_service.get_or_create_thread("20260525-001", "默认")

        with patch("core.comment_service.router.call_soul_comment_reply", return_value=None):
            result = comment_service.call_comment_reply(thread.id, "这句先记下", FakeClient(), "fake-model")

        messages = comment_service.list_thread_messages(thread.id)
        self.assertFalse(result.ok)
        self.assertIsNone(result.assistant_message_id)
        self.assertEqual(["user"], [message.role for message in messages])

    def test_comment_reply_ignores_todo_fields(self) -> None:
        thread = comment_service.get_or_create_thread("20260525-001", "默认")
        client = FakeClient(
            {
                "reply": "我记下来了。",
                "todos_to_upsert": [
                    {
                        "id": None,
                        "task": "今晚整理歌单",
                        "date": "2026-05-25",
                        "start_time": None,
                        "end_time": None,
                        "status": "未完成",
                    }
                ],
                "todos_to_delete": [],
            }
        )

        result = comment_service.call_comment_reply(thread.id, "提醒我今晚整理歌单", client, "fake-model")
        row = db.query_one("SELECT COUNT(*) AS count FROM todos")

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.assistant_message_id)
        self.assertEqual(0, row["count"])

    def test_comment_context_omits_todos_when_tool_disabled(self) -> None:
        tool_config_service.set_tool_enabled("todo", False)
        thread = comment_service.get_or_create_thread("20260525-001", "默认")
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "整理歌单", "未完成", 1.0, 1.0),
        )

        context = comment_service.build_comment_context(thread.id, "继续聊")

        self.assertNotIn("整理歌单", context.context)
        self.assertNotIn("# 待办事项", context.context)

    def test_comment_reply_does_not_write_light_reflection_tables_or_revisions(self) -> None:
        thread = comment_service.get_or_create_thread("20260525-001", "默认")

        comment_service.call_comment_reply(thread.id, "这是一条评论线程回复", FakeClient(), "fake-model")

        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM entities")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM emotions")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM events")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM relations")["count"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM user_md_revisions")["count"])
        rows = db.query_all("SELECT source FROM soul_memory_revisions WHERE source != 'system'")
        self.assertEqual([], rows)

    def _insert_post_and_comment(self, post_id: str, soul_name: str, comment: str) -> None:
        if db.query_one("SELECT 1 FROM posts WHERE id = ?", (post_id,)) is None:
            db.execute(
                """
                INSERT INTO posts(id, ts, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (post_id, "2026-05-25T10:00:00+08:00", "今天想认真练歌。", 1.0, 1.0),
            )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (post_id, soul_name, comment, 2.0),
        )


if __name__ == "__main__":
    unittest.main()
