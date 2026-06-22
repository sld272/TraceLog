from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, soul_service


class SoulServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        soul_service.SOULS_DIR = self.workspace / "souls"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        soul_service.SOULS_DIR = self.old_souls_dir
        self.tmp.cleanup()

    def test_sync_souls_creates_defaults(self) -> None:
        soul_service.sync_souls()
        records = soul_service.list_souls()
        self.assertEqual(
            ["拾迹者", "温柔树洞", "毒舌好友"],
            [record.name for record in records],
        )
        self.assertTrue(all(record.enabled for record in records))
        self.assertTrue((self.workspace / "souls" / "拾迹者.md").exists())

    def test_sync_souls_upserts_new_file(self) -> None:
        soul_service.sync_souls()
        custom_path = self.workspace / "souls" / "测试好友.md"
        custom_path.write_text(
            "---\nname: 测试好友\ndescription: 测试描述\n---\n\n你是测试好友。\n",
            encoding="utf-8",
        )
        soul_service.sync_souls()
        record = soul_service.get_soul("测试好友")
        self.assertTrue(record.enabled)
        self.assertEqual("测试描述", record.description)

    def test_sync_souls_disables_missing_soul_file(self) -> None:
        soul_service.sync_souls()
        soul_service.create_soul("测试好友", description="测试描述")
        (self.workspace / "souls" / "测试好友.md").unlink()
        soul_service.sync_souls()
        self.assertFalse(soul_service.get_soul("测试好友").enabled)

    def test_enable_disable_and_list_enabled_souls(self) -> None:
        soul_service.sync_souls()
        soul_service.disable_soul("拾迹者")
        self.assertEqual(
            ["温柔树洞", "毒舌好友"],
            [soul.name for soul in soul_service.list_enabled_souls()],
        )
        soul_service.enable_soul("拾迹者")
        self.assertEqual(
            ["拾迹者", "温柔树洞", "毒舌好友"],
            [soul.name for soul in soul_service.list_enabled_souls()],
        )

    def test_reorder_souls_moves_named_records_to_front(self) -> None:
        soul_service.sync_souls()
        soul_service.create_soul("测试好友", description="测试描述")
        records = soul_service.reorder_souls(["测试好友", "拾迹者"])
        self.assertEqual(
            ["测试好友", "拾迹者", "温柔树洞", "毒舌好友"],
            [record.name for record in records],
        )

    def test_create_soul_rejects_invalid_and_duplicate_names(self) -> None:
        soul_service.sync_souls()
        with self.assertRaises(ValueError):
            soul_service.create_soul("../坏名字")
        with self.assertRaises(ValueError):
            soul_service.create_soul("拾迹者")


if __name__ == "__main__":
    unittest.main()
