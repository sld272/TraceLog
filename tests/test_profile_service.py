from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, profile_service
from tests.helpers import require_not_none


USER_MD = """---
schema: tracelog/user.md@v1
sensitivity:
  基本信息: high
  身份与角色: high
  当前状态与关注: low
  技能与专长: normal
  性格与倾向: normal
---

# 用户档案

## 基本信息
- （暂无） <!-- id: bf-empty -->

## 身份与角色
- 高一学生 <!-- id: role-student -->

## 当前状态与关注
- 下周三数学期末考，正在复习 <!-- id: current-exam -->

## 技能与专长
- Python 后端 <!-- id: sk-py -->

## 性格与倾向
- 容易焦虑 <!-- id: trait-anxious -->
"""


class ProfileServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_user_md_path = profile_service.USER_MD_PATH

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")

        db.init_db()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text(USER_MD, encoding="utf-8")
        self._insert_post("20260525-001")
        self._insert_post("20260525-002")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
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

        content = profile_service.read_profile()
        row = db.query_one("SELECT patch, source FROM user_md_revisions ORDER BY id DESC LIMIT 1")

        self.assertEqual("applied", result["status"])
        self.assertIn("熟悉 ChromaDB <!-- id: skill-", content)
        row = require_not_none(row)
        self.assertIn("熟悉 ChromaDB", row["patch"])
        self.assertEqual("reflector", row["source"])

    def test_user_overwrite_writes_user_source_revision(self) -> None:
        updated = USER_MD + "\n用户手动覆盖\n"

        profile_service.write_profile(
            updated,
            source="user",
            patch={"op": "overwrite_user_memory"},
        )
        row = db.query_one("SELECT snapshot, patch, source FROM user_md_revisions ORDER BY id DESC LIMIT 1")

        row = require_not_none(row)
        self.assertEqual(updated, profile_service.read_profile())
        self.assertEqual(updated, row["snapshot"])
        self.assertIn("overwrite_user_memory", row["patch"])
        self.assertEqual("user", row["source"])

    def test_default_user_md_has_empty_sections_without_placeholders(self) -> None:
        self.assertNotIn("暂无", profile_service.DEFAULT_USER_MD)
        self.assertIn("schema: tracelog/user.md@v1", profile_service.DEFAULT_USER_MD)
        self.assertIn("## 基本信息\n\n## 身份与角色", profile_service.DEFAULT_USER_MD)
        self.assertIn("## 当前状态与关注", profile_service.DEFAULT_USER_MD)
        self.assertNotIn("## 关键身份", profile_service.DEFAULT_USER_MD)
        self.assertNotIn("## 身份与现状", profile_service.DEFAULT_USER_MD)
        self.assertNotIn("## 长期目标与当前痛点", profile_service.DEFAULT_USER_MD)
        self.assertNotIn("## 近期主题与走向", profile_service.DEFAULT_USER_MD)

    def test_high_add_with_one_evidence_writes_user_md_and_leaves_legacy_placeholder(self) -> None:
        result = profile_service.apply_patch(
            {
                "section": "基本信息",
                "ops": [{"op": "add", "value": "学校：南京大学"}],
                "evidence": ["20260525-001"],
                "confidence": 0.85,
            }
        )

        content = profile_service.read_profile()
        row = db.query_one("SELECT patch, source FROM user_md_revisions ORDER BY id DESC LIMIT 1")

        self.assertEqual("applied", result["status"])
        self.assertIn("学校：南京大学 <!-- id: bf-", content)
        self.assertIn("（暂无） <!-- id: bf-empty -->", content)
        row = require_not_none(row)
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
        self.assertNotIn("姓名：张三 <!-- id: bf-", profile_service.read_profile())

    def test_high_update_and_remove_thresholds(self) -> None:
        low_update = profile_service.apply_patch(
            {
                "section": "身份与角色",
                "ops": [{"op": "update", "anchor": "role-student", "value": "高中一年级学生"}],
                "evidence": ["20260525-001"],
                "confidence": 0.87,
            }
        )
        update = profile_service.apply_patch(
            {
                "section": "身份与角色",
                "ops": [{"op": "update", "anchor": "role-student", "value": "高中一年级学生"}],
                "evidence": ["20260525-001"],
                "confidence": 0.88,
            }
        )
        low_remove = profile_service.apply_patch(
            {
                "section": "身份与角色",
                "ops": [{"op": "remove", "anchor": "role-student"}],
                "evidence": ["20260525-001"],
                "confidence": 0.94,
            }
        )
        remove = profile_service.apply_patch(
            {
                "section": "身份与角色",
                "ops": [{"op": "remove", "anchor": "role-student"}],
                "evidence": ["20260525-001"],
                "confidence": 0.95,
            }
        )

        content = profile_service.read_profile()

        self.assertEqual("skipped", low_update["status"])
        self.assertEqual("low_confidence", low_update["reason"])
        self.assertEqual("applied", update["status"])
        self.assertEqual("skipped", low_remove["status"])
        self.assertEqual("low_confidence", low_remove["reason"])
        self.assertEqual("applied", remove["status"])
        self.assertNotIn("高中一年级学生", content)

    def test_low_current_status_thresholds_are_fast_moving(self) -> None:
        add = profile_service.apply_patch(
            {
                "section": "当前状态与关注",
                "ops": [{"op": "add", "value": "最近在准备数学期末考"}],
                "evidence": ["20260525-001"],
                "confidence": 0.50,
            }
        )
        update = profile_service.apply_patch(
            {
                "section": "当前状态与关注",
                "ops": [{"op": "update", "anchor": "current-exam", "value": "数学期末考已进入最后复习阶段"}],
                "evidence": ["20260525-001"],
                "confidence": 0.50,
            }
        )
        low_remove = profile_service.apply_patch(
            {
                "section": "当前状态与关注",
                "ops": [{"op": "remove", "anchor": "current-exam"}],
                "evidence": ["20260525-001"],
                "confidence": 0.59,
            }
        )
        remove = profile_service.apply_patch(
            {
                "section": "当前状态与关注",
                "ops": [{"op": "remove", "anchor": "current-exam"}],
                "evidence": ["20260525-001"],
                "confidence": 0.60,
            }
        )

        content = profile_service.read_profile()

        self.assertEqual("applied", add["status"])
        self.assertEqual("applied", update["status"])
        self.assertEqual("skipped", low_remove["status"])
        self.assertEqual("low_confidence", low_remove["reason"])
        self.assertEqual("applied", remove["status"])
        self.assertIn("最近在准备数学期末考 <!-- id: current-", content)
        self.assertNotIn("数学期末考已进入最后复习阶段", content)

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
        self.assertNotIn("熟悉 FTS5", profile_service.read_profile())

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
                "section": "性格与倾向",
                "ops": [{"op": "remove", "anchor": "trait-anxious"}],
                "evidence": ["20260525-002"],
                "confidence": 0.9,
            }
        )

        content = profile_service.read_profile()
        rows = db.query_all("SELECT id FROM user_md_revisions")

        self.assertEqual("applied", update["status"])
        self.assertEqual("applied", remove["status"])
        self.assertIn("Python 后端与 SQLite <!-- id: sk-py -->", content)
        self.assertNotIn("容易焦虑", content)
        self.assertEqual(2, len(rows))

    def test_patch_gate_reuses_section_bounds_lookup(self) -> None:
        original = profile_service._find_section_bounds
        with patch("core.profile_service._find_section_bounds", wraps=original) as find_section_bounds:
            result = profile_service.apply_patch(
                {
                    "section": "技能与专长",
                    "ops": [{"op": "update", "anchor": "sk-py", "value": "Python 后端与 SQLite"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.65,
                }
            )

        self.assertEqual("applied", result["status"])
        self.assertEqual(2, find_section_bounds.call_count)

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
