from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, memory_review_service, profile_service, soul_memory_service
from tests.helpers import require_not_none


USER_MEMORY = """# 用户档案

## 身份与角色
- 测试用户
"""


def soul_memory(name: str, body: str = "- 初始记忆") -> str:
    return f"""---
schema: tracelog/soul_memory.md@v1
soul: {name}
---

# {name}的相处记忆

## 对用户的理解
{body}
"""


class MemoryReviewServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_user_md_path = profile_service.USER_MD_PATH
        self.old_soul_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"

        db.init_db()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text(USER_MEMORY, encoding="utf-8")
        self._insert_soul("默认")
        self._insert_soul("毒舌好友")
        soul_memory_service.write_soul_memory("默认", soul_memory("默认"), source="init")
        soul_memory_service.write_soul_memory("毒舌好友", soul_memory("毒舌好友"), source="init")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_soul_memories_dir
        self.tmp.cleanup()

    def test_save_user_memory_writes_file_and_user_revision(self) -> None:
        updated = "# 用户档案\n\n## 身份与角色\n- 用户手动修正\n"

        memory_review_service.save_user_memory(updated)
        row = require_not_none(db.query_one("SELECT source, patch FROM user_md_revisions ORDER BY id DESC LIMIT 1"))

        self.assertEqual(updated, memory_review_service.read_user_memory())
        self.assertEqual("user", row["source"])
        self.assertIn("overwrite_user_memory", row["patch"])

    def test_save_user_memory_bypasses_reflector_evidence_gate(self) -> None:
        updated = "# 用户档案\n\n## 基本信息\n- 姓名：用户自己写的\n"

        memory_review_service.save_user_memory(updated)

        self.assertIn("姓名：用户自己写的", profile_service.read_profile())
        self.assertEqual(1, require_not_none(db.query_one("SELECT COUNT(*) AS count FROM user_md_revisions"))["count"])

    def test_save_soul_memory_writes_file_and_user_revision(self) -> None:
        updated = soul_memory("默认", "- 用户手动修正 SOUL 记忆")

        memory_review_service.save_soul_memory("默认", updated)
        row = require_not_none(
            db.query_one("SELECT soul_name, source, patch FROM soul_memory_revisions ORDER BY id DESC LIMIT 1")
        )

        self.assertEqual(updated, memory_review_service.read_soul_memory("默认"))
        self.assertEqual("默认", row["soul_name"])
        self.assertEqual("user", row["source"])
        self.assertIn("overwrite_soul_memory", row["patch"])

    def test_invalid_user_memory_does_not_write_revision(self) -> None:
        before = require_not_none(db.query_one("SELECT COUNT(*) AS count FROM user_md_revisions"))["count"]

        with self.assertRaises(ValueError):
            memory_review_service.save_user_memory("# 错误标题\n")
        with self.assertRaises(ValueError):
            memory_review_service.save_user_memory("# 用户档案\n\x00")

        after = require_not_none(db.query_one("SELECT COUNT(*) AS count FROM user_md_revisions"))["count"]
        self.assertEqual(before, after)
        self.assertEqual(USER_MEMORY, profile_service.read_profile())

    def test_invalid_soul_memory_does_not_write_revision(self) -> None:
        before = require_not_none(db.query_one("SELECT COUNT(*) AS count FROM soul_memory_revisions"))["count"]
        original = soul_memory_service.read_soul_memory("默认")

        with self.assertRaises(ValueError):
            memory_review_service.save_soul_memory("默认", "# 默认的错误记忆\n")

        after = require_not_none(db.query_one("SELECT COUNT(*) AS count FROM soul_memory_revisions"))["count"]
        self.assertEqual(before, after)
        self.assertEqual(original, soul_memory_service.read_soul_memory("默认"))

    def test_list_user_revisions_is_summary_only_and_ordered(self) -> None:
        profile_service.write_profile(USER_MEMORY + "\nreflector\n", source="reflector")
        memory_review_service.save_user_memory(USER_MEMORY + "\nuser\n")

        revisions = memory_review_service.list_user_revisions()

        self.assertEqual(["user", "reflector"], [item["source"] for item in revisions])
        self.assertNotIn("snapshot", revisions[0])
        self.assertEqual("user", revisions[0]["target_type"])
        self.assertIsNone(revisions[0]["target_name"])
        self.assertEqual({"op": "overwrite_user_memory"}, revisions[0]["patch"])

    def test_get_user_revision_returns_snapshot_and_patch(self) -> None:
        updated = USER_MEMORY + "\n用户详情\n"
        memory_review_service.save_user_memory(updated)
        revision_id = memory_review_service.list_user_revisions()[0]["id"]

        revision = require_not_none(memory_review_service.get_user_revision(revision_id))

        self.assertEqual(updated, revision["snapshot"])
        self.assertEqual({"op": "overwrite_user_memory"}, revision["patch"])
        self.assertIsNone(memory_review_service.get_user_revision(9999))

    def test_list_soul_revisions_filters_by_soul_and_source(self) -> None:
        soul_memory_service.write_soul_memory("默认", soul_memory("默认", "- AI 修改"), source="soul_deep_reflector")
        memory_review_service.save_soul_memory("默认", soul_memory("默认", "- 用户修改"))
        memory_review_service.save_soul_memory("毒舌好友", soul_memory("毒舌好友", "- 另一个 SOUL 修改"))

        default_revisions = memory_review_service.list_soul_revisions(soul_name="默认")
        user_revisions = memory_review_service.list_soul_revisions(source="user")

        self.assertTrue(all(item["target_name"] == "默认" for item in default_revisions))
        self.assertEqual(["user", "soul_deep_reflector"], [item["source"] for item in default_revisions[:2]])
        self.assertEqual(["毒舌好友", "默认"], [item["target_name"] for item in user_revisions])
        self.assertNotIn("snapshot", default_revisions[0])

    def test_get_soul_revision_returns_snapshot_and_patch(self) -> None:
        updated = soul_memory("默认", "- 用户详情")
        memory_review_service.save_soul_memory("默认", updated)
        revision_id = memory_review_service.list_soul_revisions(soul_name="默认", source="user")[0]["id"]

        revision = require_not_none(memory_review_service.get_soul_revision(revision_id))

        self.assertEqual("soul", revision["target_type"])
        self.assertEqual("默认", revision["target_name"])
        self.assertEqual(updated, revision["snapshot"])
        self.assertEqual({"op": "overwrite_soul_memory"}, revision["patch"])
        self.assertIsNone(memory_review_service.get_soul_revision(9999))

    def _insert_soul(self, name: str) -> None:
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            (name, f"souls/{name}.md", 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
