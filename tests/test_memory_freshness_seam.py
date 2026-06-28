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

    def test_section_always_includes_freshness_seam(self) -> None:
        now = db.now_ts()
        self._post("我刚开始学法语", now - 50)
        text = memory_read.build_memory_section("public_post", None, "法语").text
        self.assertIn("尚未稳定沉淀的原始证据", text)
        self.assertIn("我刚开始学法语", text)

    def test_freshness_log_records_keyword_overlap_and_budget(self) -> None:
        now = db.now_ts()
        self._post("我在准备考研", now - 100)   # overlaps the query
        self._post("随便记点别的", now - 50)    # does not
        events = []

        def capture(event, level="INFO", **fields):
            events.append((event, fields))

        with patch.object(memory_read.logging_service, "is_enabled_for", return_value=True), \
             patch.object(memory_read.logging_service, "log_event", side_effect=capture):
            memory_read.freshness_seam(
                "public_post", None, now=now, query="考研", trace_context={"post_id": "pX"}
            )

        logged = [f for (e, f) in events if e == "memory_freshness"]
        self.assertEqual(1, len(logged))
        payload = logged[0]
        overlaps = {e["keyword_overlap"] for e in payload["events"]}
        self.assertIn(1, overlaps)  # "考研" matched one event's snapshot
        self.assertIn(0, overlaps)  # the other matched nothing — keyword count, not distance
        self.assertTrue(all("in_budget" in e for e in payload["events"]))
        self.assertEqual({"post_id": "pX"}, payload["trace"])

    def test_freshness_log_silent_when_logging_disabled(self) -> None:
        now = db.now_ts()
        self._post("我在准备考研", now - 100)
        with patch.object(memory_read.logging_service, "_enabled", False), \
             patch.object(memory_read.logging_service, "log_event") as mock_log:
            memory_read.freshness_seam("public_post", None, now=now, query="考研")
        mock_log.assert_not_called()

    def test_freshness_ranks_split_abbreviation_evidence_first(self) -> None:
        # query 南大法语生 — jieba splits 南大 into single chars; the recovery lets
        # the 南大 evidence outrank an unrelated but more recent post in ordering.
        now = db.now_ts()
        self._post("我在南大读书很开心", now - 100)   # older, mentions 南大
        self._post("今天随便写点别的", now - 10)       # newer, unrelated
        items, _ = memory_read.freshness_seam("public_post", None, now=now, query="南大法语生")
        self.assertEqual("我在南大读书很开心", items[0].content)


if __name__ == "__main__":
    unittest.main()
