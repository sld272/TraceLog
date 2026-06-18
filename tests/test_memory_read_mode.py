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


if __name__ == "__main__":
    unittest.main()
