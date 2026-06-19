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
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES ('p-1', '2026-06-19T10:00:00+08:00', '交作业', 1.0, 1.0)
            """
        )

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

    def test_todo_accept_preserves_post_source(self) -> None:
        suggestion = suggestion_service.create_suggestion(
            "todo",
            {"task": "交作业", "date": "2026-06-20"},
            "post:p-1",
            0.8,
        )
        result = suggestion_service.accept(suggestion["id"])
        self.assertEqual("p-1", result["created"]["source_post"])

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

    def test_todo_update_and_delete_suggestions_apply_only_on_accept(self) -> None:
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES ('todo-1', '旧任务', '未完成', 1.0, 1.0)
            """
        )
        update = suggestion_service.create_suggestion(
            "todo",
            {
                "action": "update",
                "todo_id": "todo-1",
                "task": "新任务",
                "date": None,
                "start_time": None,
                "end_time": None,
                "status": "已完成",
            },
            "chat:1",
            0.8,
        )
        self.assertEqual("旧任务", db.query_one("SELECT task FROM todos WHERE id = 'todo-1'")["task"])
        updated = suggestion_service.accept(update["id"])["created"]
        self.assertEqual("新任务", updated["task"])
        self.assertEqual("已完成", updated["status"])

        delete = suggestion_service.create_suggestion(
            "todo",
            {
                "action": "delete",
                "todo_id": "todo-1",
                "task": "新任务",
                "status": "已完成",
            },
            "comment:2",
            0.8,
        )
        deleted = suggestion_service.accept(delete["id"])["created"]
        self.assertTrue(deleted["deleted"])
        self.assertIsNone(db.query_one("SELECT id FROM todos WHERE id = 'todo-1'"))


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
