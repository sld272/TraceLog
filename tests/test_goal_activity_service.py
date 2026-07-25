from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, goal_activity_service, goal_service


class GoalActivityServiceTest(unittest.TestCase):
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

    def test_last_progress_only_tracks_progress_and_milestone(self) -> None:
        goal = goal_service.create_goal("跨专业考研", None, "long")

        for kind, created_at in (
            ("commitment", 10.0),
            ("blocked", 20.0),
            ("scheduled", 30.0),
        ):
            goal_activity_service.record(
                goal["id"],
                kind,
                "manual" if kind != "scheduled" else "schedule",
                f"{'post' if kind != 'scheduled' else 'schedule'}:{kind}",
                evidence_span=None if kind == "scheduled" else kind,
                created_at=created_at,
            )
        unchanged = goal_service.get_goal(goal["id"])
        self.assertIsNone(unchanged["last_progress_at"])
        self.assertFalse(unchanged["focus"])

        goal_activity_service.record(
            goal["id"],
            "progress",
            "manual",
            "post:progress",
            evidence_span="最近每天都在复习",
            created_at=40.0,
        )
        goal_activity_service.record(
            goal["id"],
            "milestone",
            "manual",
            "post:milestone",
            evidence_span="完成了第一轮复习",
            created_at=50.0,
        )
        goal_activity_service.record(
            goal["id"],
            "commitment",
            "manual",
            "post:later-commitment",
            evidence_span="下周开始第二轮",
            created_at=60.0,
        )

        updated = goal_service.get_goal(goal["id"])
        self.assertEqual(50.0, updated["last_progress_at"])
        self.assertFalse(updated["focus"])
        stats = goal_activity_service.stats_for_goal(goal["id"])
        self.assertEqual(
            {
                "commitment": 2,
                "progress": 1,
                "blocked": 1,
                "milestone": 1,
                "scheduled": 1,
            },
            stats["counts"],
        )
        self.assertEqual(40.0, stats["last_progress_at"])
        self.assertEqual(50.0, stats["last_milestone_at"])

    def test_duplicate_write_is_idempotent_and_rejected_activity_stays_rejected(self) -> None:
        goal = goal_service.create_goal("完成课程项目", None, "short", focus=False)
        first = goal_activity_service.record(
            goal["id"],
            "progress",
            "manual",
            "post:same",
            evidence_span="完成了主流程",
            created_at=10.0,
        )
        duplicate = goal_activity_service.record(
            goal["id"],
            "blocked",
            "manual",
            "post:same",
            evidence_span="这个字段不会覆盖原记录",
            created_at=20.0,
        )
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual("progress", duplicate["kind"])
        self.assertEqual(1, len(goal_activity_service.list_for_goal(goal["id"])))

        rejected = goal_activity_service.reject(first["id"])
        replay = goal_activity_service.record(
            goal["id"],
            "milestone",
            "manual",
            "post:same",
            evidence_span="这个字段也不会复活原记录",
            created_at=30.0,
        )
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(rejected["id"], replay["id"])
        self.assertEqual("rejected", replay["status"])
        self.assertEqual([], goal_activity_service.list_for_goal(goal["id"]))
        self.assertEqual(
            [rejected["id"]],
            [
                activity["id"]
                for activity in goal_activity_service.list_for_goal(
                    goal["id"], status="rejected"
                )
            ],
        )
        self.assertIsNone(goal_service.get_goal(goal["id"])["last_progress_at"])

    def test_delete_goal_removes_activities(self) -> None:
        goal = goal_service.create_goal("准备作品集", None, "short")
        goal_activity_service.record(
            goal["id"],
            "commitment",
            "manual",
            "post:portfolio",
            evidence_span="本周整理作品集",
        )

        self.assertTrue(goal_service.delete_goal(goal["id"]))
        self.assertEqual(
            0,
            db.query_one(
                "SELECT COUNT(*) AS count FROM goal_activities WHERE goal_id = ?",
                (goal["id"],),
            )["count"],
        )

    def test_auto_requires_evidence_span_and_schedule_allows_null(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "long")
        with self.assertRaisesRegex(ValueError, "evidence_span"):
            goal_activity_service.record(
                goal["id"],
                "progress",
                "auto",
                "post:auto-without-span",
                confidence=0.9,
            )

        scheduled = goal_activity_service.record(
            goal["id"],
            "scheduled",
            "schedule",
            "schedule:event-1",
        )
        self.assertIsNone(scheduled["evidence_span"])
        self.assertIsNone(scheduled["confidence"])


if __name__ == "__main__":
    unittest.main()
