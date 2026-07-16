from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import db, goal_service, suggestion_service
from core.llm import goal_router


class GoalServiceTest(unittest.TestCase):
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

    def test_goal_lifecycle_and_focus_gc(self) -> None:
        created = goal_service.create_goal("完成课程项目", "交付可演示版本", "short", focus=True)
        self.assertTrue(created["id"].startswith("g_"))
        self.assertEqual([created["id"]], [goal["id"] for goal in goal_service.list_active_short_term()])

        progressed = goal_service.mark_progress(created["id"], at=100.0)
        self.assertEqual(100.0, progressed["last_progress_at"])
        self.assertTrue(progressed["focus"])

        current = goal_service.list_current_focus(now=100.0 + 29 * goal_service.DAY_SECONDS)
        self.assertEqual([created["id"]], [goal["id"] for goal in current])
        expired = goal_service.list_current_focus(now=100.0 + 31 * goal_service.DAY_SECONDS)
        self.assertEqual([], expired)
        self.assertFalse(goal_service.get_goal(created["id"])["focus"])
        self.assertEqual("active", goal_service.get_goal(created["id"])["status"])

        goal_service.set_status(created["id"], "done")
        self.assertEqual("done", goal_service.get_goal(created["id"])["status"])

    def test_long_term_listing_and_formatting(self) -> None:
        goal = goal_service.create_goal("跨专业考研", None, "long")
        self.assertEqual([goal["id"]], [item["id"] for item in goal_service.list_active_long_term()])
        self.assertIn("长期", goal_service.format_goal_for_context(goal))


class SuggestionServiceTest(unittest.TestCase):
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

    def test_goal_accept_creates_active_goal(self) -> None:
        suggestion = suggestion_service.create_suggestion(
            "goal",
            {"title": "考研", "detail": "准备 2027 初试", "horizon": "long"},
            "chat:12",
            0.91,
        )
        result = suggestion_service.accept(suggestion["id"])
        self.assertEqual("accepted", result["suggestion"]["status"])
        self.assertEqual("suggested_accepted", result["created"]["source"])
        self.assertEqual("active", result["created"]["status"])

    def test_dismissed_key_is_a_permanent_tombstone(self) -> None:
        first = suggestion_service.create_suggestion(
            "goal",
            {"title": "  跨专业 考研 ", "horizon": "long"},
            "chat:12",
            0.8,
        )
        suggestion_service.dismiss(first["id"])
        second = suggestion_service.create_suggestion(
            "goal",
            {"title": "跨专业考研", "horizon": "long"},
            "chat:99",
            0.95,
        )
        self.assertIsNone(second)
        self.assertEqual([], suggestion_service.list_pending())

    def test_retry_reuses_pending_instead_of_duplicating(self) -> None:
        first = suggestion_service.create_suggestion(
            "goal", {"title": "完成毕业论文", "horizon": "short"}, "comment:3", 0.8
        )
        second = suggestion_service.create_suggestion(
            "goal", {"title": "完成毕业论文", "horizon": "short"}, "comment:9", 0.9
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(suggestion_service.list_pending("goal")))

    def test_legacy_pending_non_goal_suggestions_are_hidden(self) -> None:
        conn = db.connect()
        try:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                """
                INSERT INTO suggestions(
                    id, kind, payload_json, confidence, status,
                    normalized_key, created_at
                )
                VALUES ('legacy-1', 'todo', '{}', 0.8, 'pending', 'legacy-key', 1.0)
                """
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual([], suggestion_service.list_pending())
        self.assertIsNone(suggestion_service.get_suggestion("legacy-1"))
        with self.assertRaisesRegex(ValueError, "suggestion 不存在"):
            suggestion_service.accept("legacy-1")

    def test_only_goal_suggestion_kind_is_supported(self) -> None:
        with self.assertRaisesRegex(ValueError, "kind 只支持：goal"):
            suggestion_service.create_suggestion("todo", {}, None)


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False)))]
        )


class GoalRouterTest(unittest.TestCase):
    def test_router_filters_low_confidence_invalid_and_duplicate_candidates(self) -> None:
        client = FakeClient(
            {
                "goals": [
                    {"title": "考研", "detail": None, "horizon": "long", "confidence": 0.9},
                    {"title": "考研", "detail": "重复", "horizon": "long", "confidence": 0.8},
                    {"title": "也许学日语", "detail": None, "horizon": "long", "confidence": 0.4},
                    {"title": "坏数据", "horizon": "later", "confidence": 0.9},
                ]
            }
        )
        goals = goal_router.call_goal_router(client, "fake-model", user_input="我决定考研")
        self.assertEqual(
            [{"title": "考研", "detail": None, "horizon": "long", "confidence": 0.9}],
            goals,
        )


if __name__ == "__main__":
    unittest.main()
