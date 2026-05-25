from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import memory
from core import db, profile_service


USER_MD = """---
schema: tracelog/user.md@v1
sensitivity:
  基本信息: high
  技能与专长: normal
  性格与情绪倾向: normal
---

# 用户档案

## 基本信息
- （暂无） <!-- id: bf-empty -->

## 技能与专长
- Python 后端 <!-- id: sk-py -->

## 性格与情绪倾向
- 容易焦虑 <!-- id: tr-anxious -->
"""


class ProfileServiceTest(unittest.TestCase):
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
        (self.workspace / "user.md").write_text(USER_MD, encoding="utf-8")
        self._insert_post("20260525-001")
        self._insert_post("20260525-002")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        memory.WORKSPACE_DIR = self.old_memory_workspace
        memory.USER_MD_PATH = self.old_user_md_path
        self.tmp.cleanup()

    def test_normal_add_writes_user_md_and_revision(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "技能与专长",
                "ops": [{"op": "add", "value": "熟悉 ChromaDB"}],
                "evidence": ["20260525-001"],
                "confidence": 0.8,
            }
        )

        content = memory.read_profile()
        row = db.query_one("SELECT patch, source FROM user_md_revisions ORDER BY id DESC LIMIT 1")

        self.assertEqual("applied", result["status"])
        self.assertIn("熟悉 ChromaDB <!-- id: sk-", content)
        self.assertIsNotNone(row)
        self.assertIn("熟悉 ChromaDB", row["patch"])
        self.assertEqual("reflector", row["source"])

    def test_high_add_goes_to_pending_without_changing_user_md(self) -> None:
        before = memory.read_profile()

        result = profile_service.apply_patch(
            {
                "section": "基本信息",
                "ops": [{"op": "add", "value": "学校：南京大学"}],
                "evidence": ["20260525-001", "20260525-002"],
                "confidence": 0.85,
            }
        )

        pending = profile_service.list_pending_changes()

        self.assertEqual("pending", result["status"])
        self.assertEqual(before, memory.read_profile())
        self.assertEqual(1, len(pending))
        self.assertEqual("基本信息", pending[0]["section"])
        self.assertEqual("学校：南京大学", pending[0]["patch"]["ops"][0]["value"])

    def test_invalid_evidence_is_skipped(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "技能与专长",
                "ops": [{"op": "add", "value": "熟悉 FTS5"}],
                "evidence": ["missing-post"],
                "confidence": 0.9,
            }
        )

        self.assertEqual({"status": "skipped", "reason": "invalid_evidence"}, result)
        self.assertNotIn("熟悉 FTS5", memory.read_profile())

    def test_low_confidence_is_skipped(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "技能与专长",
                "ops": [{"op": "add", "value": "熟悉 FastAPI"}],
                "evidence": ["20260525-001"],
                "confidence": 0.3,
            }
        )

        self.assertEqual("skipped", result["status"])
        self.assertEqual("low_confidence", result["reason"])

    def test_update_remove_missing_anchor_is_skipped(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "技能与专长",
                "ops": [{"op": "update", "anchor": "missing", "value": "Python"}],
                "evidence": ["20260525-001"],
                "confidence": 0.9,
            }
        )

        self.assertEqual({"status": "skipped", "reason": "invalid_anchor"}, result)

    def test_update_and_remove_existing_anchor(self) -> None:
        update = profile_service.apply_patch(
            {
                "section": "技能与专长",
                "ops": [{"op": "update", "anchor": "sk-py", "value": "Python 后端与 SQLite"}],
                "evidence": ["20260525-001"],
                "confidence": 0.9,
            }
        )
        remove = profile_service.apply_patch(
            {
                "section": "性格与情绪倾向",
                "ops": [{"op": "remove", "anchor": "tr-anxious"}],
                "evidence": ["20260525-002"],
                "confidence": 0.9,
            }
        )

        content = memory.read_profile()
        rows = db.query_all("SELECT id FROM user_md_revisions")

        self.assertEqual("applied", update["status"])
        self.assertEqual("applied", remove["status"])
        self.assertIn("Python 后端与 SQLite <!-- id: sk-py -->", content)
        self.assertNotIn("容易焦虑", content)
        self.assertEqual(2, len(rows))

    def _insert_post(self, post_id: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-25T10:00:00+08:00", "测试记录", 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
