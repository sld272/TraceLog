from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, memory_events_service as mes, memory_read, memory_unit_service as mus


class ReadModeFlagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        with db.transaction() as conn:
            ev = mes.record_post_mutation(conn, post_id="p1", op="create", content="e", occurred_at=1.0).id
        mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content="喜欢安静的咖啡馆", importance=0.5, evidence_event_ids=[ev],
        )

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_legacy_mode_injects_nothing(self) -> None:
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "legacy"}):
            self.assertFalse(memory_read.memory_reading_enabled())
            self.assertEqual(memory_read.memory_section_for("public_post", "gotoh", "咖啡馆"), "")

    def test_default_is_legacy(self) -> None:
        env = dict(os.environ)
        env.pop(memory_read.READ_MODE_ENV, None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(memory_read.read_mode(), "legacy")

    def test_units_mode_injects_memory(self) -> None:
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "units"}):
            self.assertTrue(memory_read.memory_reading_enabled())
            section = memory_read.memory_section_for("public_post", "gotoh", "咖啡馆")
        self.assertIn("咖啡馆", section)
        self.assertIn("[相关记忆]", section)

    def test_invalid_mode_falls_back_to_legacy(self) -> None:
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "bogus"}):
            self.assertEqual(memory_read.read_mode(), "legacy")
            self.assertFalse(memory_read.memory_reading_enabled())

    def test_v2_portrait_is_table_only_no_workspace_file(self) -> None:
        from core import memory_view_service as mvs

        ev = self._ev_for_core()
        uid = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="identity", content="南大法语生，自学计算机", confidence=0.9, tier="core",
            importance=0.9, evidence_event_ids=[ev],
        )
        view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        # portrait persisted in the table...
        self.assertIsNotNone(mvs.get_view("global", "public", mvs.VIEW_USER_MD))
        self.assertIn(uid, view.unit_ids)
        # ...but NOT written as a workspace file (user.md is non-editable in v2)
        self.assertFalse((self.workspace / "user.md").exists())

    def _ev_for_core(self) -> int:
        with db.transaction() as conn:
            return mes.record_post_mutation(conn, post_id="pc", op="create", content="e", occurred_at=1.0).id

    def test_v2_mode_suppresses_legacy_profile_injection(self) -> None:
        from core import context_builder, profile_service

        old_path = profile_service.USER_MD_PATH
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            Path(profile_service.USER_MD_PATH).write_text("# 用户档案\n我是旧档案", encoding="utf-8")
            with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "units"}):
                v2 = context_builder.build_context(query=None)
            self.assertNotIn("用户档案", v2.shared_context)  # v2 portrait owns this channel
            with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "legacy"}):
                legacy = context_builder.build_context(query=None)
            self.assertIn("用户档案", legacy.shared_context)  # unchanged in legacy
        finally:
            profile_service.USER_MD_PATH = old_path


if __name__ == "__main__":
    unittest.main()
