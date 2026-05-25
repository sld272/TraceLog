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
  关键身份: high
  技能与专长: normal
  性格与情绪倾向: normal
---

# 用户档案

## 基本信息
- （暂无） <!-- id: bf-empty -->

## 关键身份
- 高一学生 <!-- id: ki-student -->

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

    def test_default_user_md_has_empty_sections_without_placeholders(self) -> None:
        self.assertNotIn("暂无", memory.DEFAULT_USER_MD)
        self.assertIn("## 基本信息\n\n## 关键身份", memory.DEFAULT_USER_MD)

    def test_high_add_with_one_evidence_writes_user_md_and_leaves_legacy_placeholder(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "基本信息",
                "ops": [{"op": "add", "value": "学校：南京大学"}],
                "evidence": ["20260525-001"],
                "confidence": 0.85,
            }
        )

        content = memory.read_profile()
        row = db.query_one("SELECT patch, source FROM user_md_revisions ORDER BY id DESC LIMIT 1")

        self.assertEqual("applied", result["status"])
        self.assertIn("学校：南京大学 <!-- id: bf-", content)
        self.assertIn("（暂无） <!-- id: bf-empty -->", content)
        self.assertIsNotNone(row)
        self.assertIn("学校：南京大学", row["patch"])
        self.assertEqual("reflector", row["source"])

    def test_high_add_low_confidence_is_skipped(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "基本信息",
                "ops": [{"op": "add", "value": "姓名：张三"}],
                "evidence": ["20260525-001"],
                "confidence": 0.3,
            }
        )

        self.assertEqual("skipped", result["status"])
        self.assertEqual("low_confidence", result["reason"])
        self.assertNotIn("姓名：张三 <!-- id: bf-", memory.read_profile())

    def test_high_update_and_remove_thresholds(self) -> None:
        low_update = profile_service.apply_patch(
            {
                "section": "关键身份",
                "ops": [{"op": "update", "anchor": "ki-student", "value": "高中一年级学生"}],
                "evidence": ["20260525-001"],
                "confidence": 0.87,
            }
        )
        update = profile_service.apply_patch(
            {
                "section": "关键身份",
                "ops": [{"op": "update", "anchor": "ki-student", "value": "高中一年级学生"}],
                "evidence": ["20260525-001"],
                "confidence": 0.88,
            }
        )
        low_remove = profile_service.apply_patch(
            {
                "section": "关键身份",
                "ops": [{"op": "remove", "anchor": "ki-student"}],
                "evidence": ["20260525-001"],
                "confidence": 0.94,
            }
        )
        remove = profile_service.apply_patch(
            {
                "section": "关键身份",
                "ops": [{"op": "remove", "anchor": "ki-student"}],
                "evidence": ["20260525-001"],
                "confidence": 0.95,
            }
        )

        content = memory.read_profile()

        self.assertEqual("skipped", low_update["status"])
        self.assertEqual("low_confidence", low_update["reason"])
        self.assertEqual("applied", update["status"])
        self.assertEqual("skipped", low_remove["status"])
        self.assertEqual("low_confidence", low_remove["reason"])
        self.assertEqual("applied", remove["status"])
        self.assertNotIn("高中一年级学生", content)

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

    def test_placeholder_like_values_are_skipped(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "技能与专长",
                "ops": [{"op": "add", "value": "暂无"}],
                "evidence": ["20260525-001"],
                "confidence": 0.9,
            }
        )

        self.assertEqual({"status": "skipped", "reason": "invalid_value"}, result)

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
                "confidence": 0.65,
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
