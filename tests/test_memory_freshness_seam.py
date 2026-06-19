from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_read,
)


class FreshnessSeamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _post(self, content: str, ts: float) -> int:
        with db.transaction() as conn:
            return mes.record_post_mutation(conn, post_id=f"p{ts}", op="create", content=content, occurred_at=ts).id

    def test_seam_returns_recent_unconsumed_user_evidence(self) -> None:
        now = db.now_ts()
        self._post("我在准备考研", now - 100)
        items, truncated = memory_read.freshness_seam("public_post", None, now=now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content, "我在准备考研")
        self.assertFalse(truncated)

    def test_old_evidence_outside_window_is_dropped(self) -> None:
        now = db.now_ts()
        old_ts = now - (memory_read.FRESHNESS_WINDOW_DAYS + 1) * memory_read.DAY_SECONDS
        self._post("很久以前的事", old_ts)
        items, _ = memory_read.freshness_seam("public_post", None, now=now)
        self.assertEqual(items, [])

    def test_budget_truncates(self) -> None:
        now = db.now_ts()
        for i in range(memory_read.FRESHNESS_MAX_EVENTS + 3):
            self._post(f"动态{i}", now - i)
        items, truncated = memory_read.freshness_seam("public_post", None, now=now)
        self.assertLessEqual(len(items), memory_read.FRESHNESS_MAX_EVENTS)
        self.assertTrue(truncated)

    def test_other_souls_private_evidence_is_not_readable(self) -> None:
        now = db.now_ts()
        with db.transaction() as conn:
            mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create",
                                     content="只想对 luna 说的私房话", occurred_at=now - 10, role="user")
        # another soul replying must not see luna's private chat
        items_other, _ = memory_read.freshness_seam("chat", "毒舌好友", now=now)
        self.assertEqual(items_other, [])
        # luna itself can
        items_self, _ = memory_read.freshness_seam("chat", "luna", now=now)
        self.assertEqual(len(items_self), 1)

    def test_section_includes_seam_only_in_freshness_mode(self) -> None:
        now = db.now_ts()
        self._post("我刚开始学法语", now - 50)
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "units_and_freshness"}):
            text = memory_read.build_memory_section("public_post", None, "法语").text
        self.assertIn("最近动态", text)
        self.assertIn("我刚开始学法语", text)
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "units"}):
            text2 = memory_read.build_memory_section("public_post", None, "法语").text
        self.assertNotIn("最近动态", text2)


if __name__ == "__main__":
    unittest.main()
