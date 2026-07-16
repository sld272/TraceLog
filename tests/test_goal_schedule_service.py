from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core import db, goal_schedule_service, goal_service


class GoalScheduleServiceTest(unittest.TestCase):
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

    def test_link_crud_is_visible_from_goal_and_event_sides(self) -> None:
        goal = goal_service.create_goal("每周健身", None, "short")
        self._insert_event("event-1", "练背", 100.0, 200.0)

        created = goal_schedule_service.link(goal["id"], "event-1")

        self.assertEqual(goal["id"], created["goal_id"])
        self.assertEqual("event-1", created["event_id"])
        self.assertEqual(
            ["event-1"],
            [event["id"] for event in goal_schedule_service.links_for_goal(goal["id"])],
        )
        self.assertEqual(
            [{"goal_id": goal["id"], "goal_title": "每周健身"}],
            goal_schedule_service.links_for_events(["event-1"])["event-1"],
        )

        self.assertTrue(goal_schedule_service.unlink(goal["id"], "event-1"))
        self.assertEqual([], goal_schedule_service.links_for_goal(goal["id"]))
        self.assertFalse(goal_schedule_service.unlink(goal["id"], "event-1"))

    def test_weekly_progress_uses_shanghai_monday_boundaries(self) -> None:
        zone = ZoneInfo("Asia/Shanghai")
        goal = goal_service.create_goal("每周健身", None, "short")
        expectation = {
            "period": "week",
            "target": 3,
            "label": "每周健身 3 次",
        }
        goal_schedule_service.update_expectation(goal["id"], expectation)
        for event_id, local_start in (
            ("previous-sunday", datetime(2026, 7, 12, 23, 59, tzinfo=zone)),
            ("monday", datetime(2026, 7, 13, 0, 0, tzinfo=zone)),
            ("sunday", datetime(2026, 7, 19, 23, 59, tzinfo=zone)),
            ("next-monday", datetime(2026, 7, 20, 0, 0, tzinfo=zone)),
        ):
            self._insert_event(event_id, event_id, local_start.timestamp(), local_start.timestamp() + 3600)
            goal_schedule_service.link(goal["id"], event_id)

        progress = goal_schedule_service.weekly_progress(
            goal["id"],
            now=datetime(2026, 7, 15, 12, 0, tzinfo=zone),
        )

        self.assertEqual("2026-07-13", progress["week_start"])
        self.assertEqual("2026-07-19", progress["week_end"])
        self.assertEqual(2, progress["current"])
        self.assertEqual(3, progress["target"])
        self.assertEqual("2/3", progress["text"])
        self.assertEqual(expectation, progress["expectation"])

    def _insert_event(self, event_id: str, subject: str, start_ts: float, end_ts: float) -> None:
        db.execute(
            """
            INSERT INTO schedule_events(
                id, subject, start_ts, end_ts, start_local, end_local, synced_at
            ) VALUES (?, ?, ?, ?, '1970-01-01T08:01:40', '1970-01-01T08:03:20', 1)
            """,
            (event_id, subject, start_ts, end_ts),
        )


if __name__ == "__main__":
    unittest.main()
