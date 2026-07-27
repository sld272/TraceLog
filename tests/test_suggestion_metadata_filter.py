from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import db, suggestion_service


class SuggestionMetadataFilterTest(unittest.TestCase):
    """回复 metadata 里的建议快照必须按真实状态过滤。

    快照的 status 是写入那一刻冻结的，用户忽略/采纳后只改了 suggestions 表；
    不过滤的话刷新页面又会把已经决定过的建议原样显示出来。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = Path(self.tmp.name) / "workspace"
        db.DB_PATH = db.WORKSPACE_DIR / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _create(self, kind: str, payload: dict) -> dict:
        created = suggestion_service.create_suggestion(kind, payload, "comment:1")
        assert created is not None
        return created

    @staticmethod
    def _metadata(*suggestions: dict) -> str:
        return json.dumps(
            {"status": "ok", "suggestions": list(suggestions)},
            ensure_ascii=False,
        )

    @staticmethod
    def _ids(metadata: str | None) -> list[str]:
        assert metadata is not None
        return [item["id"] for item in json.loads(metadata)["suggestions"]]

    def test_pending_snapshot_is_kept(self) -> None:
        suggestion = self._create("goal", {"title": "每天跑步", "horizon": "short"})
        metadata = self._metadata(suggestion)

        self.assertEqual([suggestion["id"]], self._ids(suggestion_service.metadata_with_live_suggestions(metadata)))

    def test_dismissed_goal_suggestion_is_dropped(self) -> None:
        suggestion = self._create("goal", {"title": "每天跑步", "horizon": "short"})
        metadata = self._metadata(suggestion)
        suggestion_service.dismiss(suggestion["id"])

        self.assertEqual([], self._ids(suggestion_service.metadata_with_live_suggestions(metadata)))

    def test_dismissed_schedule_suggestion_is_dropped(self) -> None:
        suggestion = self._create(
            "schedule",
            {"subject": "跑步", "date": "2026-08-01", "start_time": "19:30", "end_time": "20:30"},
        )
        metadata = self._metadata(suggestion)
        suggestion_service.dismiss(suggestion["id"])

        self.assertEqual([], self._ids(suggestion_service.metadata_with_live_suggestions(metadata)))

    def test_only_the_decided_one_is_dropped(self) -> None:
        kept = self._create("goal", {"title": "每天跑步", "horizon": "short"})
        dropped = self._create("goal", {"title": "背单词", "horizon": "long"})
        metadata = self._metadata(kept, dropped)
        suggestion_service.dismiss(dropped["id"])

        self.assertEqual([kept["id"]], self._ids(suggestion_service.metadata_with_live_suggestions(metadata)))

    def test_deleted_row_is_dropped(self) -> None:
        """帖子被删时 pending 行会被直接删掉，快照不能再指向一条不存在的建议。"""
        suggestion = self._create("goal", {"title": "每天跑步", "horizon": "short"})
        metadata = self._metadata(suggestion)
        suggestion_service.delete_pending_for_evidence("comment:1")

        self.assertEqual([], self._ids(suggestion_service.metadata_with_live_suggestions(metadata)))

    def test_metadata_without_suggestions_is_returned_untouched(self) -> None:
        for metadata in (None, "", "not json", json.dumps({"status": "failed", "error": "boom"})):
            self.assertEqual(metadata, suggestion_service.metadata_with_live_suggestions(metadata))


if __name__ == "__main__":
    unittest.main()
