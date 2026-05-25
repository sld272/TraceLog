from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import context_builder, db, profile_service, soul_memory_service, soul_service, todo_service, tool_config_service


class FakeClient:
    def __init__(self, payload: dict | None = None, content: str | None = None) -> None:
        self.payload = payload or {"todos_to_upsert": [], "todos_to_delete": []}
        self.content = content
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        self.calls += 1
        content = self.content if self.content is not None else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TodoServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_user_md_path = profile_service.USER_MD_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"

        db.init_db()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n", encoding="utf-8")
        soul_service.sync_souls()
        self._insert_post("20260525-001", "明天下午三点前交项目 PPT。")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        self.tmp.cleanup()

    def test_todo_tool_extracts_from_public_post_and_records_source_post(self) -> None:
        client = FakeClient(
            {
                "todos_to_upsert": [
                    {
                        "id": None,
                        "task": "交项目 PPT",
                        "date": "2026-05-26",
                        "start_time": "15:00",
                        "end_time": None,
                        "status": "未完成",
                    }
                ],
                "todos_to_delete": [],
            }
        )

        result = todo_service.run_for_post("20260525-001", client, "fake-model")
        row = db.query_one(
            "SELECT task, date, start_time, source_post FROM todos WHERE task = ?",
            ("交项目 PPT",),
        )

        self.assertTrue(result.applied)
        self.assertEqual(1, result.upserted)
        self.assertEqual("20260525-001", row["source_post"])
        self.assertEqual(("2026-05-26", "15:00"), (row["date"], row["start_time"]))

    def test_todo_tool_disabled_skips_call_and_context(self) -> None:
        tool_config_service.set_tool_enabled("todo", False)
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "已有任务", "未完成", 1.0, 1.0),
        )
        client = FakeClient()

        result = todo_service.run_for_post("20260525-001", client, "fake-model")
        context = context_builder.build_context()

        self.assertTrue(result.skipped)
        self.assertEqual(0, client.calls)
        self.assertNotIn("已有任务", context.shared_context)
        self.assertNotIn("# 待办事项", context.shared_context)

    def test_apply_post_todos_updates_existing_and_deletes_existing_only(self) -> None:
        db.execute(
            """
            INSERT INTO todos(id, task, date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("todo-1", "交项目 PPT", "2026-05-26", "未完成", 1.0, 1.0),
        )
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-2", "取消的任务", "未完成", 1.0, 1.0),
        )

        upserted, deleted = todo_service.apply_post_todos(
            "20260525-001",
            [
                {
                    "id": "todo-1",
                    "task": "交最终版项目 PPT",
                    "date": "2026-05-26",
                    "start_time": None,
                    "end_time": None,
                    "status": "已完成",
                }
            ],
            [{"id": "todo-2"}, {"id": "missing"}],
        )
        updated = db.query_one("SELECT task, status, source_post, completed_at FROM todos WHERE id = ?", ("todo-1",))
        deleted_row = db.query_one("SELECT 1 FROM todos WHERE id = ?", ("todo-2",))

        self.assertEqual((1, 1), (upserted, deleted))
        self.assertEqual("交最终版项目 PPT", updated["task"])
        self.assertEqual("已完成", updated["status"])
        self.assertEqual("20260525-001", updated["source_post"])
        self.assertIsNotNone(updated["completed_at"])
        self.assertIsNone(deleted_row)

    def _insert_post(self, post_id: str, content: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-25T10:00:00+08:00", content, 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
