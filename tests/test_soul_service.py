from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db
from core import soul_memory_service
from core import soul_service


class SoulServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"

        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        self.tmp.cleanup()

    def test_sync_souls_creates_defaults_and_revisions(self) -> None:
        soul_service.sync_souls()

        records = soul_service.list_souls()
        self.assertEqual(["默认", "毒舌好友"], [record.name for record in records])
        self.assertTrue(all(record.enabled for record in records))
        self.assertTrue((self.workspace / "souls" / "默认.md").exists())
        self.assertTrue((self.workspace / "soul_memories" / "默认.md").exists())
        self.assertNotIn("（暂无）", soul_memory_service.read_soul_memory("默认"))

        rows = db.query_all(
            """
            SELECT soul_name, source
            FROM soul_memory_revisions
            ORDER BY soul_name
            """
        )
        self.assertEqual(
            [("毒舌好友", "system"), ("默认", "system")],
            [(row["soul_name"], row["source"]) for row in rows],
        )

    def test_sync_souls_upserts_new_file_and_creates_memory(self) -> None:
        soul_service.sync_souls()
        custom_path = self.workspace / "souls" / "测试好友.md"
        custom_path.write_text(
            "---\n"
            "name: 测试好友\n"
            "description: 测试描述\n"
            "---\n\n"
            "你是测试好友。\n",
            encoding="utf-8",
        )

        soul_service.sync_souls()

        record = soul_service.get_soul("测试好友")
        self.assertTrue(record.enabled)
        self.assertEqual("测试描述", record.description)
        self.assertTrue(record.memory_exists)

    def test_sync_souls_disables_missing_persona_file(self) -> None:
        soul_service.sync_souls()
        soul_service.create_soul("测试好友", description="测试描述")
        (self.workspace / "souls" / "测试好友.md").unlink()

        soul_service.sync_souls()

        self.assertFalse(soul_service.get_soul("测试好友").enabled)

    def test_enable_disable_and_list_enabled_souls(self) -> None:
        soul_service.sync_souls()

        soul_service.disable_soul("默认")
        enabled = soul_service.list_enabled_souls()
        self.assertEqual(["毒舌好友"], [soul.name for soul in enabled])

        soul_service.enable_soul("默认")
        enabled = soul_service.list_enabled_souls()
        self.assertEqual(["默认", "毒舌好友"], [soul.name for soul in enabled])

    def test_reorder_souls_moves_named_records_to_front(self) -> None:
        soul_service.sync_souls()
        soul_service.create_soul("测试好友", description="测试描述")

        records = soul_service.reorder_souls(["测试好友", "默认"])

        self.assertEqual(["测试好友", "默认", "毒舌好友"], [record.name for record in records])
        self.assertEqual([0, 1, 2], [record.sort_order for record in records])

    def test_create_soul_rejects_invalid_and_duplicate_names(self) -> None:
        soul_service.sync_souls()

        with self.assertRaises(ValueError):
            soul_service.create_soul("../坏名字")
        with self.assertRaises(ValueError):
            soul_service.create_soul("默认")

    def test_write_soul_memory_writes_file_and_revision(self) -> None:
        soul_service.sync_souls()

        soul_memory_service.write_soul_memory("默认", "# 新记忆\n", source="user")

        self.assertEqual("# 新记忆\n", soul_memory_service.read_soul_memory("默认"))
        row = db.query_one(
            """
            SELECT snapshot, source
            FROM soul_memory_revisions
            WHERE soul_name = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            ("默认",),
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("# 新记忆\n", row["snapshot"])
        self.assertEqual("user", row["source"])

    def test_write_soul_memory_rejects_missing_soul(self) -> None:
        db.init_db()

        with self.assertRaises(ValueError):
            soul_memory_service.write_soul_memory("不存在", "# 记忆\n")

    def test_apply_soul_memory_patch_add_update_remove_and_skip_placeholder(self) -> None:
        soul_service.sync_souls()
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("20260525-001", "2026-05-25T10:00:00+08:00", "我最近练歌很认真。", 1.0, 1.0),
        )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("20260525-001", "默认", "我看见你在认真练歌。", 2.0),
        )

        add_result = soul_memory_service.apply_patch(
            "默认",
            {
                "section": "对用户的理解",
                "ops": [{"op": "add", "value": "用户最近在认真练歌"}],
                "evidence": ["post:20260525-001"],
                "confidence": 0.8,
            },
        )
        content = soul_memory_service.read_soul_memory("默认")
        anchor = content.split("<!-- id: ", 1)[1].split(" -->", 1)[0]
        update_result = soul_memory_service.apply_patch(
            "默认",
            {
                "section": "对用户的理解",
                "ops": [{"op": "update", "anchor": anchor, "value": "用户最近在稳定练歌"}],
                "evidence": ["post:20260525-001"],
                "confidence": 0.8,
            },
        )
        skip_result = soul_memory_service.apply_patch(
            "默认",
            {
                "section": "对用户的理解",
                "ops": [{"op": "add", "value": "暂无"}],
                "evidence": ["post:20260525-001"],
                "confidence": 0.9,
            },
        )
        remove_result = soul_memory_service.apply_patch(
            "默认",
            {
                "section": "对用户的理解",
                "ops": [{"op": "remove", "anchor": anchor}],
                "evidence": ["post:20260525-001"],
                "confidence": 0.8,
            },
        )
        final_content = soul_memory_service.read_soul_memory("默认")

        self.assertEqual("applied", add_result["status"])
        self.assertEqual("applied", update_result["status"])
        self.assertEqual({"status": "skipped", "reason": "invalid_value"}, skip_result)
        self.assertEqual("applied", remove_result["status"])
        self.assertNotIn("用户最近在稳定练歌", final_content)

    def test_apply_soul_memory_patch_rejects_other_soul_evidence(self) -> None:
        soul_service.sync_souls()
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("20260525-001", "2026-05-25T10:00:00+08:00", "只和毒舌好友有关。", 1.0, 1.0),
        )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("20260525-001", "毒舌好友", "这条证据不属于默认。", 2.0),
        )

        result = soul_memory_service.apply_patch(
            "默认",
            {
                "section": "对用户的理解",
                "ops": [{"op": "add", "value": "用户只把这件事告诉毒舌好友"}],
                "evidence": ["post:20260525-001"],
                "confidence": 0.9,
            },
        )

        self.assertEqual({"status": "skipped", "reason": "invalid_evidence"}, result)


if __name__ == "__main__":
    unittest.main()
