from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core import db, goal_schedule_service, goal_service
from core.schedule_service import ScheduleService


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
        self.assertIsNotNone(
            db.query_one(
                "SELECT 1 FROM goal_schedule_assessments WHERE event_id = ?",
                ("event-1",),
            )
        )

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

    def test_weekly_progress_by_goals_matches_the_single_goal_path(self) -> None:
        """成批算出来的节奏必须和逐个算的一模一样，页面才敢一次铺完。"""
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 15, 12, 0, tzinfo=zone)
        with_target = goal_service.create_goal("每周健身", None, "short")
        goal_schedule_service.update_expectation(
            with_target["id"],
            {"period": "week", "target": 3, "label": "每周健身 3 次"},
        )
        without_target = goal_service.create_goal("读完那本书", None, "long")
        for event_id, local_start in (
            ("monday", datetime(2026, 7, 13, 9, 0, tzinfo=zone)),
            ("wednesday", datetime(2026, 7, 15, 9, 0, tzinfo=zone)),
            ("next-monday", datetime(2026, 7, 20, 9, 0, tzinfo=zone)),
        ):
            self._insert_event(event_id, event_id, local_start.timestamp(), local_start.timestamp() + 3600)
            goal_schedule_service.link(with_target["id"], event_id)

        batch = goal_schedule_service.weekly_progress_by_goals(
            [with_target["id"], without_target["id"], "missing-goal"],
            now=now,
        )

        self.assertEqual(
            goal_schedule_service.weekly_progress(with_target["id"], now=now),
            batch[with_target["id"]],
        )
        self.assertEqual(
            goal_schedule_service.weekly_progress(without_target["id"], now=now),
            batch[without_target["id"]],
        )
        self.assertEqual(2, batch[with_target["id"]]["current"])
        self.assertIsNone(batch[without_target["id"]]["target"])
        # 不存在的目标只是不出现，不该让整页的节奏一起垮掉。
        self.assertNotIn("missing-goal", batch)

    def test_weekly_progress_counts_a_linked_local_event(self) -> None:
        class DisconnectedAuth:
            def client_id(self):
                return None

            def get_access_token(self):
                return None

        zone = ZoneInfo("Asia/Shanghai")
        goal = goal_service.create_goal("本地周目标", None, "short")
        service = ScheduleService(auth=DisconnectedAuth(), clock=lambda: 1.0)
        service.create_local_account()
        created = service.create_event(
            subject="本地执行",
            event_date=date(2026, 7, 16),
            goal_id=goal["id"],
        )

        progress = goal_schedule_service.weekly_progress(
            goal["id"],
            now=datetime(2026, 7, 15, 12, 0, tzinfo=zone),
        )

        self.assertEqual("local", created["account_id"])
        self.assertEqual(1, progress["current"])

    def test_expired_event_links_and_writes_scheduled_not_progress(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        self._insert_event("exam-check", "驾考体检", 10.0, 20.0)

        result = goal_schedule_service.run_automation(
            None,
            None,
            now=30.0,
            matcher=lambda events, goals: [
                {
                    "event_id": "exam-check",
                    "matches": [{"goal_id": goal["id"], "confidence": 0.95}],
                }
            ],
        )

        self.assertEqual("ok", result["matching_status"])
        self.assertEqual(
            [{"goal_id": goal["id"], "goal_title": "考驾照"}],
            goal_schedule_service.links_for_events(["exam-check"])["exam-check"],
        )
        activities = db.query_all(
            """
            SELECT kind, source, evidence_ref, evidence_span, confidence
            FROM goal_activities
            WHERE goal_id = ?
            """,
            (goal["id"],),
        )
        self.assertEqual(1, len(activities))
        self.assertEqual("scheduled", activities[0]["kind"])
        self.assertEqual("schedule", activities[0]["source"])
        self.assertEqual("schedule:exam-check", activities[0]["evidence_ref"])
        self.assertIsNone(activities[0]["evidence_span"])
        self.assertIsNone(activities[0]["confidence"])
        self.assertIsNone(goal_service.get_goal(goal["id"])["last_progress_at"])

    def test_future_event_only_links_without_ledger_activity(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        self._insert_event("future-exam", "科目一", 100.0, 200.0)

        goal_schedule_service.run_automation(
            None,
            None,
            now=200.0,
            matcher=lambda events, goals: [
                {
                    "event_id": "future-exam",
                    "matches": [{"goal_id": goal["id"], "confidence": 0.9}],
                }
            ],
        )

        self.assertEqual(
            1,
            len(goal_schedule_service.links_for_events(["future-exam"])["future-exam"]),
        )
        self.assertIsNone(
            db.query_one(
                "SELECT 1 FROM goal_activities WHERE evidence_ref = ?",
                ("schedule:future-exam",),
            )
        )

    def test_negative_assessment_does_not_call_matcher_again(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        # Put the goal on the same synthetic clock the scans below use, so
        # "adopted after the last verdict" reads false instead of trivially true.
        db.execute("UPDATE goals SET created_at = ? WHERE id = ?", (5.0, goal["id"]))
        self._insert_event("eye-exam", "验光", 10.0, 20.0)
        calls: list[list[str]] = []

        def matcher(events, goals):
            calls.append([str(event["id"]) for event in events])
            return [{"event_id": "eye-exam", "matches": []}]

        goal_schedule_service.run_automation(
            None, None, now=30.0, matcher=matcher
        )
        goal_schedule_service.run_automation(
            None, None, now=40.0, matcher=matcher
        )

        self.assertEqual([["eye-exam"]], calls)
        self.assertIsNotNone(
            db.query_one(
                "SELECT 1 FROM goal_schedule_assessments WHERE event_id = ?",
                ("eye-exam",),
            )
        )
        self.assertEqual([], goal_schedule_service.links_for_events(["eye-exam"])["eye-exam"])

    def test_adopting_a_goal_reopens_previously_unmatched_events(self) -> None:
        """A verdict is not a permanent tombstone.

        Adopting goals is the product's core loop, so a newly adopted goal has to
        be able to pick up schedule entries that were judged before it existed —
        otherwise its weekly progress is stuck at zero forever, which is the same
        lie this feature exists to fix.
        """
        first = goal_service.create_goal("考驾照", None, "short")
        db.execute("UPDATE goals SET created_at = ? WHERE id = ?", (5.0, first["id"]))
        self._insert_event("gym-1", "健身房", 10.0, 20.0)
        calls: list[tuple[list[str], int]] = []

        def matcher(events, goals):
            calls.append(([str(event["id"]) for event in events], len(goals)))
            return [{"event_id": "gym-1", "matches": []}]

        goal_schedule_service.run_automation(None, None, now=30.0, matcher=matcher)
        goal_schedule_service.run_automation(None, None, now=40.0, matcher=matcher)
        self.assertEqual(1, len(calls))  # no new goal, so no re-judging

        second = goal_service.create_goal("健身", None, "short")
        db.execute("UPDATE goals SET created_at = ? WHERE id = ?", (50.0, second["id"]))
        goal_schedule_service.run_automation(None, None, now=60.0, matcher=matcher)
        self.assertEqual(2, len(calls))
        self.assertEqual(["gym-1"], calls[1][0])
        self.assertEqual(2, calls[1][1])  # both goals offered to the matcher

        # The refreshed timestamp must stop it there: without the upsert the row
        # would stay older than the new goal and every later scan would re-judge.
        goal_schedule_service.run_automation(None, None, now=70.0, matcher=matcher)
        self.assertEqual(2, len(calls))

    def test_cancelled_event_is_not_assessed_linked_or_recorded(self) -> None:
        goal_service.create_goal("考驾照", None, "short")
        self._insert_event(
            "cancelled-exam",
            "科目一",
            10.0,
            20.0,
            is_cancelled=True,
        )
        calls = 0

        def matcher(events, goals):
            nonlocal calls
            calls += 1
            return []

        goal_schedule_service.run_automation(
            None, None, now=30.0, matcher=matcher
        )

        self.assertEqual(0, calls)
        self.assertIsNone(
            db.query_one(
                "SELECT 1 FROM goal_schedule_assessments WHERE event_id = ?",
                ("cancelled-exam",),
            )
        )
        self.assertEqual(
            [],
            goal_schedule_service.links_for_events(["cancelled-exam"])[
                "cancelled-exam"
            ],
        )
        self.assertIsNone(
            db.query_one(
                "SELECT 1 FROM goal_activities WHERE evidence_ref = ?",
                ("schedule:cancelled-exam",),
            )
        )

    def test_repeated_series_instances_are_batched_and_recorded_independently(self) -> None:
        goal = goal_service.create_goal("每周健身", None, "short")
        self._insert_event(
            "series-1",
            "练背",
            10.0,
            20.0,
            series_master_id="series-master",
        )
        self._insert_event(
            "series-2",
            "练背",
            30.0,
            40.0,
            series_master_id="series-master",
        )
        calls: list[list[str]] = []

        def matcher(events, goals):
            calls.append([str(event["id"]) for event in events])
            return [
                {
                    "event_id": str(event["id"]),
                    "matches": [{"goal_id": goal["id"], "confidence": 0.9}],
                }
                for event in events
            ]

        goal_schedule_service.run_automation(
            None, None, now=50.0, matcher=matcher
        )

        self.assertEqual([["series-1", "series-2"]], calls)
        rows = db.query_all(
            """
            SELECT evidence_ref, kind
            FROM goal_activities
            WHERE goal_id = ?
            ORDER BY evidence_ref
            """,
            (goal["id"],),
        )
        self.assertEqual(
            [
                ("schedule:series-1", "scheduled"),
                ("schedule:series-2", "scheduled"),
            ],
            [(row["evidence_ref"], row["kind"]) for row in rows],
        )

    def test_repeated_scan_keeps_scheduled_activity_idempotent(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        self._insert_event("exam", "科目一", 10.0, 20.0)
        calls = 0

        def matcher(events, goals):
            nonlocal calls
            calls += 1
            return [
                {
                    "event_id": "exam",
                    "matches": [{"goal_id": goal["id"], "confidence": 0.9}],
                }
            ]

        goal_schedule_service.run_automation(
            None, None, now=30.0, matcher=matcher
        )
        goal_schedule_service.run_automation(
            None, None, now=40.0, matcher=matcher
        )

        self.assertEqual(1, calls)
        row = db.query_one(
            """
            SELECT COUNT(*) AS activity_count
            FROM goal_activities
            WHERE goal_id = ? AND evidence_ref = ?
            """,
            (goal["id"], "schedule:exam"),
        )
        self.assertEqual(1, int(row["activity_count"]))

    def test_confidence_below_threshold_is_persisted_without_link(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        self._insert_event("weak-match", "交通法规讲座", 100.0, 200.0)

        goal_schedule_service.run_automation(
            None,
            None,
            now=50.0,
            matcher=lambda events, goals: [
                {
                    "event_id": "weak-match",
                    "matches": [{"goal_id": goal["id"], "confidence": 0.69}],
                }
            ],
        )

        self.assertIsNotNone(
            db.query_one(
                "SELECT 1 FROM goal_schedule_assessments WHERE event_id = ?",
                ("weak-match",),
            )
        )
        self.assertEqual(
            [],
            goal_schedule_service.links_for_events(["weak-match"])["weak-match"],
        )

    def test_automation_has_an_independent_write_switch(self) -> None:
        goal_service.create_goal("考驾照", None, "short")
        self._insert_event("exam", "科目一", 10.0, 20.0)
        with patch.dict(
            os.environ,
            {goal_schedule_service.GOAL_SCHEDULE_AUTOMATION_ENABLED_ENV: "0"},
        ):
            result = goal_schedule_service.run_automation(
                None,
                None,
                now=30.0,
                matcher=lambda events, goals: self.fail("matcher should stay off"),
            )

        self.assertFalse(result["enabled"])
        self.assertIsNone(
            db.query_one("SELECT 1 FROM goal_schedule_assessments")
        )
        self.assertIsNone(db.query_one("SELECT 1 FROM goal_activities"))

    def test_dry_run_previews_links_and_scheduled_rows_without_writes(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        self._insert_event("exam", "科目一", 10.0, 20.0)

        result = goal_schedule_service.run_automation(
            None,
            None,
            now=30.0,
            dry_run=True,
            matcher=lambda events, goals: [
                {
                    "event_id": "exam",
                    "matches": [{"goal_id": goal["id"], "confidence": 0.9}],
                }
            ],
        )

        self.assertEqual(["exam"], result["assessed_event_ids"])
        self.assertEqual(["exam"], [item["event_id"] for item in result["links"]])
        self.assertEqual(
            ["exam"], [item["event_id"] for item in result["scheduled"]]
        )
        self.assertIsNone(db.query_one("SELECT 1 FROM goal_schedule_assessments"))
        self.assertIsNone(db.query_one("SELECT 1 FROM goal_schedule_links"))
        self.assertIsNone(db.query_one("SELECT 1 FROM goal_activities"))

    def test_best_effort_swallows_failure_and_logs_warning(self) -> None:
        with (
            patch(
                "core.goal_schedule_service.run_automation",
                side_effect=RuntimeError("broken"),
            ),
            patch("core.goal_schedule_service.logging_service.log_event") as log_event,
        ):
            result = goal_schedule_service.run_automation_best_effort(None, None)

        self.assertIsNone(result)
        log_event.assert_called_once_with(
            "goal_schedule_automation_failed",
            level="WARNING",
            error_type="RuntimeError",
            error="broken",
        )

    def _insert_event(
        self,
        event_id: str,
        subject: str,
        start_ts: float,
        end_ts: float,
        *,
        is_cancelled: bool = False,
        series_master_id: str | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO schedule_events(
                id, subject, start_ts, end_ts, start_local, end_local,
                series_master_id, is_cancelled, synced_at
            ) VALUES (
                ?, ?, ?, ?, '1970-01-01T08:01:40', '1970-01-01T08:03:20',
                ?, ?, 1
            )
            """,
            (
                event_id,
                subject,
                start_ts,
                end_ts,
                series_master_id,
                int(is_cancelled),
            ),
        )


if __name__ == "__main__":
    unittest.main()
