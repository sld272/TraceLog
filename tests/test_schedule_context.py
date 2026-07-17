from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core import db, goal_service, schedule_context
from core.schedule_service import ScheduleService


class DisconnectedScheduleAuth:
    def client_id(self):
        return None

    def get_access_token(self):
        return None


class ScheduleContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        db.execute(
            """
            INSERT INTO calendar_accounts(id, provider, display_name, created_at)
            VALUES ('local', 'local', '本地日历', 1)
            """
        )
        self.service = ScheduleService(auth=DisconnectedScheduleAuth())
        self.zone = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 7, 17, 12, 0, tzinfo=self.zone)

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_recent_section_groups_relative_dates_and_reports_week_density(self) -> None:
        self._insert_event(
            "past",
            "复盘会",
            self.now - timedelta(days=1, hours=3),
            location="A 会议室",
            body_preview="不应进入上下文的会议纪要",
        )
        self._insert_event("today", "看牙", self.now + timedelta(hours=2))
        self._insert_event("tomorrow", "跑步", self.now + timedelta(days=1, hours=-2))
        self._insert_event("three-days", "交材料", self.now + timedelta(days=3, hours=-2))

        recent = schedule_context.build_recent_schedule_context(
            now=self.now,
            service=self.service,
        )

        self.assertIn("# 近期日程", recent.section)
        self.assertIn("本周共 3 项安排", recent.section)
        self.assertLess(recent.section.index("## 今天"), recent.section.index("## 未来"))
        self.assertLess(recent.section.index("## 未来"), recent.section.index("## 已结束"))
        self.assertIn("今天 14:00–15:00 看牙", recent.section)
        self.assertIn("明天（周六） 10:00–11:00 跑步", recent.section)
        self.assertIn("3 天后（周一） 10:00–11:00 交材料", recent.section)
        self.assertIn("昨天·已结束 09:00–10:00 复盘会；地点：A 会议室", recent.section)
        self.assertNotIn("会议纪要", recent.section)
        self.assertEqual(
            frozenset({"past", "today", "tomorrow", "three-days"}),
            recent.event_ids,
        )

    def test_recent_section_limits_twenty_and_declares_truncation(self) -> None:
        for index in range(22):
            self._insert_event(
                f"event-{index:02d}",
                f"安排 {index:02d}",
                self.now + timedelta(hours=1, minutes=index * 2),
            )

        recent = schedule_context.build_recent_schedule_context(
            now=self.now,
            service=self.service,
        )

        self.assertEqual(20, len([line for line in recent.section.splitlines() if line.startswith("- ")]))
        self.assertEqual(20, len(recent.event_ids))
        self.assertIn("（另有 2 条未列出）", recent.section)
        self.assertIn("本周共 22 项安排", recent.section)

    def test_recent_section_absent_without_any_calendar_account(self) -> None:
        db.execute("DELETE FROM calendar_accounts")

        recent = schedule_context.build_recent_schedule_context(now=self.now)

        self.assertEqual("", recent.section)
        self.assertEqual(frozenset(), recent.event_ids)

    def test_recent_section_is_present_when_window_is_empty(self) -> None:
        recent = schedule_context.build_recent_schedule_context(
            now=self.now,
            service=self.service,
        )

        self.assertIn("# 近期日程", recent.section)
        self.assertIn("本周共 0 项安排", recent.section)
        self.assertIn("（窗口内暂无安排）", recent.section)
        self.assertEqual(frozenset(), recent.event_ids)

    def test_mentioned_section_matches_subject_location_and_old_goal_without_duplicates(self) -> None:
        self._insert_event("recent", "马拉松训练", self.now + timedelta(days=1))
        self._insert_event(
            "distant-subject",
            "上海马拉松赛前会",
            self.now + timedelta(days=30),
            body_preview="敏感会议正文",
        )
        self._insert_event(
            "distant-location",
            "领取装备",
            self.now + timedelta(days=40),
            location="马拉松终点",
        )
        active = goal_service.create_goal("马拉松训练", None, "short")
        old = goal_service.create_goal("完成马拉松", None, "long")
        goal_service.set_status(old["id"], "abandoned")
        db.execute(
            "UPDATE goals SET updated_at = ? WHERE id = ?",
            (self.now.timestamp(), old["id"]),
        )

        section = schedule_context.build_mentioned_schedule_section(
            ["马拉松"],
            exclude_event_ids={"recent"},
            now=self.now,
        )

        self.assertIn("# 提及的日程", section)
        self.assertNotIn("马拉松训练", section)
        self.assertNotIn(active["id"], section)
        self.assertIn("上海马拉松赛前会", section)
        self.assertIn("领取装备；地点：马拉松终点", section)
        self.assertIn("曾有目标：完成马拉松（已放弃，2026-07）", section)
        self.assertNotIn("敏感会议正文", section)

    def test_mentioned_section_labels_past_and_future_distance(self) -> None:
        self._insert_event("past", "专题复盘", self.now - timedelta(days=5, hours=3))
        self._insert_event("future", "专题分享", self.now + timedelta(days=3, hours=2))

        section = schedule_context.build_mentioned_schedule_section(["专题"], now=self.now)

        self.assertIn("[已过去 5 天]", section)
        self.assertIn("[3 天后]", section)

    def test_like_keywords_treat_wildcards_as_literals(self) -> None:
        self._insert_event("literal", "100% 完成会", self.now + timedelta(days=10))
        self._insert_event("wildcard", "100x 完成会", self.now + timedelta(days=11))

        section = schedule_context.build_mentioned_schedule_section(["100%"], now=self.now)

        self.assertIn("100% 完成会", section)
        self.assertNotIn("100x 完成会", section)

    def _insert_event(
        self,
        event_id: str,
        subject: str,
        start: datetime,
        *,
        location: str | None = None,
        body_preview: str | None = None,
    ) -> None:
        end = start + timedelta(hours=1)
        db.execute(
            """
            INSERT INTO schedule_events(
                id, account_id, subject, body_preview, start_ts, end_ts,
                start_local, end_local, all_day, location, synced_at
            ) VALUES (?, 'local', ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                event_id,
                subject,
                body_preview,
                start.timestamp(),
                end.timestamp(),
                start.replace(tzinfo=None).isoformat(timespec="seconds"),
                end.replace(tzinfo=None).isoformat(timespec="seconds"),
                location,
                self.now.timestamp(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
