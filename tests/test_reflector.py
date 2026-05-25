from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import memory
from core import db, reflector


class FakeClient:
    def __init__(self, content: str = "## 深反思\n\n你这段时间有明确的行动线索。") -> None:
        self.content = content
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class ReflectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_memory_workspace = memory.WORKSPACE_DIR
        self.old_user_md_path = memory.USER_MD_PATH

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        memory.WORKSPACE_DIR = str(self.workspace)
        memory.USER_MD_PATH = str(self.workspace / "user.md")

        db.init_db()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与现状\n测试用户\n", encoding="utf-8")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        memory.WORKSPACE_DIR = self.old_memory_workspace
        memory.USER_MD_PATH = self.old_user_md_path
        self.tmp.cleanup()

    def test_trigger_global_deep_reflection_writes_reflection(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。")
        client = FakeClient()

        result = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(["20260525-001"], result.related_post_ids)
        row = db.query_one("SELECT type, content, related_posts, metadata FROM reflections WHERE id = ?", (result.id,))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("global_deep", row["type"])
        self.assertIn("深反思", row["content"])
        self.assertIn("20260525-001", row["related_posts"])
        self.assertIn("cli_exit", row["metadata"])

    def test_trigger_global_deep_reflection_skips_when_no_new_posts(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。")
        client = FakeClient()

        first = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")
        second = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, client.calls)

    def _insert_post(self, post_id: str, ts: str, content: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, ts, content, 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
