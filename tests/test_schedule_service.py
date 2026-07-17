from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core import db
from core import goal_schedule_service, goal_service
from core.graph.client import GraphHTTPError
from core.schedule_service import (
    NoWritableAccountError,
    ScheduleNotConnectedError,
    ScheduleService,
)


def graph_event(event_id: str, subject: str, hour: int = 9) -> dict:
    return {
        "id": event_id,
        "subject": subject,
        "bodyPreview": f"{subject} preview",
        "start": {"dateTime": f"2026-07-16T{hour:02d}:00:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": f"2026-07-16T{hour + 1:02d}:00:00", "timeZone": "Asia/Shanghai"},
        "isAllDay": False,
        "location": {"displayName": "书房"},
        "webLink": f"https://outlook.office.com/{event_id}",
        "changeKey": f"ck-{event_id}-{subject}",
    }


class FakeAuth:
    def __init__(self, *, configured: bool = True, connected: bool = True) -> None:
        self.configured = configured
        self.connected = connected
        self.logged_out = False

    def client_id(self):
        return "client-id" if self.configured else None

    def get_access_token(self):
        return "access-token" if self.connected else None

    def account_info(self):
        return {"username": "person@example.com"} if self.connected else None

    def logout(self):
        self.logged_out = True
        self.connected = False


class FakeGraph:
    def __init__(self, delta_results=None, *, fail_create_at: int | None = None) -> None:
        self.delta_results = list(delta_results or [])
        self.fail_create_at = fail_create_at
        self.delta_calls: list[dict] = []
        self.created_payloads: list[dict] = []
        self.updated_payloads: list[tuple[str, dict]] = []
        self.deleted_ids: list[str] = []

    def calendarview_delta(self, **kwargs):
        self.delta_calls.append(kwargs)
        result = self.delta_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def create_event(self, payload):
        self.created_payloads.append(payload)
        create_number = len(self.created_payloads)
        if create_number == self.fail_create_at:
            raise GraphHTTPError(503)
        return {
            "id": f"created-{create_number}",
            "subject": payload["subject"],
            "start": payload["start"],
            "end": payload["end"],
            "isAllDay": payload["isAllDay"],
            "webLink": f"https://outlook.office.com/created-{create_number}",
        }

    def update_event(self, event_id, payload):
        self.updated_payloads.append((event_id, payload))
        return graph_event(event_id, payload.get("subject", "updated"), 15)

    def delete_event(self, event_id):
        self.deleted_ids.append(event_id)
        return None


class ScheduleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = Path(self.tmp.name) / "workspace"
        db.DB_PATH = db.WORKSPACE_DIR / "state.db"
        db.init_db()
        self.auth = FakeAuth()
        self.clock_values = iter(float(value) for value in range(1000, 100_000, 1000))

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _service(self, graph: FakeGraph, auth=None) -> ScheduleService:
        return ScheduleService(
            auth=auth or self.auth,
            graph_factory=lambda token_provider: graph,
            today=lambda: date(2026, 7, 16),
            clock=lambda: next(self.clock_values),
        )

    def _insert_outlook_event(
        self,
        event_id: str,
        subject: str,
        start: datetime,
    ) -> None:
        local_start = start.astimezone(ZoneInfo("Asia/Shanghai"))
        local_end = local_start + timedelta(hours=1)
        db.execute(
            """
            INSERT OR IGNORE INTO calendar_accounts(
                id, provider, display_name, created_at
            ) VALUES ('outlook', 'outlook', 'Outlook', 1)
            """
        )
        db.execute(
            """
            INSERT INTO schedule_events(
                id, account_id, subject, start_ts, end_ts, start_local, end_local,
                synced_at
            ) VALUES (?, 'outlook', ?, ?, ?, ?, ?, 1)
            """,
            (
                event_id,
                subject,
                local_start.timestamp(),
                local_end.timestamp(),
                local_start.replace(tzinfo=None).isoformat(timespec="seconds"),
                local_end.replace(tzinfo=None).isoformat(timespec="seconds"),
            ),
        )

    def test_initial_then_incremental_delta_updates_and_deletes_cache(self) -> None:
        graph = FakeGraph(
            [
                {
                    "events": [graph_event("e1", "初版"), graph_event("e2", "待删除", 11)],
                    "delta_link": "https://graph.microsoft.com/delta-1",
                },
                {
                    "events": [graph_event("e1", "新版"), {"id": "e2", "@removed": {"reason": "deleted"}}],
                    "delta_link": "https://graph.microsoft.com/delta-2",
                },
            ]
        )
        service = self._service(graph)

        first = service.sync()
        second = service.sync()

        self.assertTrue(first["ok"])
        self.assertEqual(2, first["upserted"])
        self.assertIn("start", graph.delta_calls[0])
        self.assertEqual({"delta_link": "https://graph.microsoft.com/delta-1"}, graph.delta_calls[1])
        self.assertEqual(1, second["upserted"])
        self.assertEqual(1, second["deleted"])
        rows = db.query_all("SELECT id, subject FROM schedule_events ORDER BY id")
        self.assertEqual([("e1", "新版")], [(row["id"], row["subject"]) for row in rows])
        self.assertEqual("https://graph.microsoft.com/delta-2", db.query_one("SELECT value FROM meta WHERE key = 'graph.delta_link'")["value"])
        self.assertEqual(
            [
                {
                    "id": "outlook",
                    "provider": "outlook",
                    "display_name": "person@example.com",
                    "event_count": 1,
                }
            ],
            service.status()["accounts"],
        )

    def test_410_discards_expired_delta_and_replaces_full_cache(self) -> None:
        graph = FakeGraph(
            [
                GraphHTTPError(410),
                {"events": [graph_event("fresh", "全量恢复")], "delta_link": "new-delta"},
            ]
        )
        service = self._service(graph)
        service.create_local_account()
        goal = goal_service.create_goal("保留本地日程", None, "short")
        local = service.create_event(
            subject="本地不参与重建",
            event_date=date(2026, 7, 16),
            account_id="local",
            goal_id=goal["id"],
        )
        db.execute(
            """
            INSERT INTO schedule_events(
                id, account_id, subject, start_ts, end_ts, start_local, end_local,
                synced_at
            ) VALUES ('stale', 'outlook', '旧缓存', 1, 2,
                      '2026-01-01T00:00:00', '2026-01-01T01:00:00', 1)
            """
        )
        for key, value in (
            ("graph.delta_link", "expired-delta"),
            ("graph.window_start", "2026-05-17"),
            ("graph.window_end", "2027-07-16"),
        ):
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

        result = service.sync()

        self.assertTrue(result["ok"])
        self.assertEqual({"delta_link": "expired-delta"}, graph.delta_calls[0])
        self.assertIn("start", graph.delta_calls[1])
        self.assertIsNone(db.query_one("SELECT 1 FROM schedule_events WHERE id = 'stale'"))
        self.assertIsNotNone(db.query_one("SELECT 1 FROM schedule_events WHERE id = 'fresh'"))
        self.assertEqual(
            [local["id"]],
            [event["id"] for event in goal_schedule_service.links_for_goal(goal["id"])],
        )
        self.assertEqual("new-delta", db.query_one("SELECT value FROM meta WHERE key = 'graph.delta_link'")["value"])

    def test_delta_removed_and_cancelled_events_remove_goal_links(self) -> None:
        cancelled = graph_event("cancelled", "会取消", 13)
        cancelled["isCancelled"] = True
        graph = FakeGraph(
            [
                {
                    "events": [graph_event("removed", "会删除", 11), graph_event("cancelled", "待确认", 13)],
                    "delta_link": "delta-1",
                },
                {
                    "events": [
                        {"id": "removed", "@removed": {"reason": "deleted"}},
                        cancelled,
                    ],
                    "delta_link": "delta-2",
                },
            ]
        )
        service = self._service(graph)
        service.sync()
        goal = goal_service.create_goal("准备会议", None, "short")
        goal_schedule_service.link(goal["id"], "removed")
        goal_schedule_service.link(goal["id"], "cancelled")

        service.sync()

        self.assertEqual([], goal_schedule_service.links_for_goal(goal["id"]))
        self.assertEqual(
            0,
            db.query_one("SELECT COUNT(*) AS count FROM goal_schedule_links")["count"],
        )

    def test_create_writes_graph_then_event_is_immediately_visible_in_cache(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        goal = goal_service.create_goal("完成 P4", None, "short")

        created = service.create_event(
            subject="评审 P3",
            event_date=date(2026, 7, 16),
            start_time=time(14, 0),
            end_time=time(15, 0),
            goal_id=goal["id"],
            client_request_id="request-123",
        )
        listed = service.list_events(date(2026, 7, 16), date(2026, 7, 16))

        self.assertEqual("created-1", created["id"])
        self.assertEqual("评审 P3", graph.created_payloads[0]["subject"])
        self.assertEqual("request-123", graph.created_payloads[0]["transactionId"])
        self.assertEqual(["created-1"], [event["id"] for event in listed["events"]])
        expected_links = [{"goal_id": goal["id"], "goal_title": "完成 P4"}]
        self.assertEqual(expected_links, created["goal_links"])
        self.assertEqual(expected_links, listed["events"][0]["goal_links"])

        updated = service.update_event("created-1", {"subject": "P3 已评审"})
        self.assertEqual("P3 已评审", updated["subject"])
        self.assertEqual(
            "P3 已评审",
            db.query_one("SELECT subject FROM schedule_events WHERE id = 'created-1'")["subject"],
        )

        service.delete_event("created-1")
        self.assertIsNone(db.query_one("SELECT 1 FROM schedule_events WHERE id = 'created-1'"))

    def test_unconnected_reads_and_sync_degrade_without_exception(self) -> None:
        graph = FakeGraph()
        auth = FakeAuth(configured=False, connected=False)
        service = self._service(graph, auth=auth)

        listed = service.list_events(date(2026, 7, 16), date(2026, 7, 16))
        synced = service.sync()

        self.assertEqual("not_connected", listed["status"])
        self.assertFalse(listed["configured"])
        self.assertEqual([], listed["events"])
        self.assertFalse(synced["ok"])
        self.assertEqual("not_connected", synced["status"])
        self.assertEqual([], graph.delta_calls)
        with self.assertRaises(ScheduleNotConnectedError):
            service.create_event(subject="x", event_date=date(2026, 7, 16))

    def test_local_account_lifecycle_is_explicit_and_listed_with_event_count(self) -> None:
        service = self._service(
            FakeGraph(), auth=FakeAuth(configured=False, connected=False)
        )

        created = service.create_local_account()

        self.assertEqual(
            {
                "id": "local",
                "provider": "local",
                "display_name": "本地日历",
                "event_count": 0,
            },
            created,
        )
        self.assertEqual([created], service.list_accounts())
        with self.assertRaises(ValueError):
            service.create_local_account()
        with self.assertRaises(ValueError):
            service.delete_local_account(delete_events=False)
        self.assertEqual(0, service.delete_local_account(delete_events=True))
        self.assertEqual([], service.list_accounts())

    def test_local_event_crud_uses_sqlite_and_remains_available_without_outlook(self) -> None:
        graph = FakeGraph()
        service = self._service(
            graph, auth=FakeAuth(configured=True, connected=False)
        )
        service.create_local_account()
        goal = goal_service.create_goal("完成本地日程", None, "short")

        created = service.create_event(
            subject="本地评审",
            event_date=date(2026, 7, 16),
            start_time=time(14, 0),
            end_time=time(15, 0),
            goal_id=goal["id"],
        )
        listed = service.list_events(date(2026, 7, 16), date(2026, 7, 16))

        self.assertTrue(created["id"].startswith("local_"))
        self.assertEqual("local", created["account_id"])
        self.assertEqual("local", created["provider"])
        self.assertEqual("2026-07-16T14:00:00", created["start_local"])
        self.assertIsNone(created["web_link"])
        self.assertIsNone(created["series_master_id"])
        self.assertIsNone(created["change_key"])
        self.assertFalse(listed["connected"])
        self.assertEqual("ok", listed["status"])
        self.assertEqual([created["id"]], [event["id"] for event in listed["events"]])
        self.assertEqual(1, listed["accounts"][0]["event_count"])
        self.assertEqual([], graph.created_payloads)

        updated = service.update_event(
            created["id"],
            {
                "subject": "本地评审完成",
                "start_time": time(15, 0),
                "end_time": time(16, 0),
            },
        )
        self.assertEqual("本地评审完成", updated["subject"])
        self.assertEqual("2026-07-16T15:00:00", updated["start_local"])
        self.assertEqual([], graph.updated_payloads)

        service.delete_event(created["id"])
        self.assertEqual([], graph.deleted_ids)
        self.assertEqual([], goal_schedule_service.links_for_goal(goal["id"]))
        self.assertEqual(
            [],
            service.list_events(date(2026, 7, 16), date(2026, 7, 16))["events"],
        )

    def test_explicit_and_default_account_routing_produce_a_sorted_mixed_list(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()

        local = service.create_event(
            subject="本地早会",
            event_date=date(2026, 7, 16),
            start_time=time(8, 0),
            end_time=time(9, 0),
            account_id="local",
        )
        outlook = service.create_event(
            subject="云端评审",
            event_date=date(2026, 7, 16),
        )

        listed = service.list_events(date(2026, 7, 16), date(2026, 7, 16))
        self.assertEqual([local["id"], outlook["id"]], [e["id"] for e in listed["events"]])
        self.assertEqual(["local", "outlook"], [e["provider"] for e in listed["events"]])
        self.assertEqual(["云端评审"], [p["subject"] for p in graph.created_payloads])

        self.auth.connected = False
        fallback = service.create_event(
            subject="断网本地写入",
            event_date=date(2026, 7, 16),
        )
        self.assertEqual("local", fallback["account_id"])
        service.delete_local_account(delete_events=True)
        with self.assertRaises(NoWritableAccountError):
            service.create_event(
                subject="没有可写账号",
                event_date=date(2026, 7, 16),
            )

    def test_delta_remote_removal_never_deletes_a_local_event_or_goal_link(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()
        goal = goal_service.create_goal("保护本地事件", None, "short")
        local = service.create_event(
            subject="只在本地",
            event_date=date(2026, 7, 16),
            goal_id=goal["id"],
            account_id="local",
        )
        graph.delta_results.append(
            {
                "events": [{"id": local["id"], "@removed": {"reason": "deleted"}}],
                "delta_link": "delta-2",
            }
        )
        for key, value in (
            ("graph.delta_link", "delta-1"),
            ("graph.window_start", "2026-05-17"),
            ("graph.window_end", "2027-07-16"),
        ):
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

        result = service.sync()

        self.assertEqual(0, result["deleted"])
        self.assertEqual([local["id"]], [e["id"] for e in goal_schedule_service.links_for_goal(goal["id"])])

    def test_deleting_local_account_cascades_events_and_goal_links(self) -> None:
        service = self._service(
            FakeGraph(), auth=FakeAuth(configured=False, connected=False)
        )
        service.create_local_account()
        goal = goal_service.create_goal("级联目标", None, "short")
        local = service.create_event(
            subject="会随账号删除",
            event_date=date(2026, 7, 16),
            account_id="local",
            goal_id=goal["id"],
        )

        deleted = service.delete_local_account(delete_events=True)

        self.assertEqual(1, deleted)
        self.assertEqual([], service.list_accounts())
        self.assertEqual([], goal_schedule_service.links_for_goal(goal["id"]))
        self.assertIsNone(
            db.query_one("SELECT 1 FROM schedule_events WHERE id = ?", (local["id"],))
        )

    def test_migration_preview_detects_normalized_subject_and_sixty_second_boundary(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()
        zone = ZoneInfo("Asia/Shanghai")
        at_boundary = service.create_event(
            subject="  Team Sync  ",
            event_date=date(2026, 7, 16),
            start_time=time(9, 0),
            end_time=time(10, 0),
            account_id="local",
        )
        inside = service.create_event(
            subject="Case Match",
            event_date=date(2026, 7, 16),
            start_time=time(11, 0),
            end_time=time(12, 0),
            account_id="local",
        )
        outside = service.create_event(
            subject="Outside",
            event_date=date(2026, 7, 16),
            start_time=time(13, 0),
            end_time=time(14, 0),
            account_id="local",
        )
        self._insert_outlook_event(
            "existing-boundary",
            "team sync",
            datetime(2026, 7, 16, 9, 1, tzinfo=zone),
        )
        self._insert_outlook_event(
            "existing-inside",
            "  CASE MATCH ",
            datetime(2026, 7, 16, 10, 59, 1, tzinfo=zone),
        )
        self._insert_outlook_event(
            "existing-outside",
            "outside",
            datetime(2026, 7, 16, 13, 1, 1, tzinfo=zone),
        )

        preview = service.migration_preview()

        self.assertEqual(3, preview["total"])
        self.assertEqual(1, preview["clean"])
        conflicts = {
            item["local"]["id"]: item["existing"]["id"]
            for item in preview["conflicts"]
        }
        self.assertEqual(
            {
                at_boundary["id"]: "existing-boundary",
                inside["id"]: "existing-inside",
            },
            conflicts,
        )
        self.assertNotIn(outside["id"], conflicts)
        self.assertEqual(
            {"id", "subject", "start_local", "end_local", "all_day"},
            set(preview["conflicts"][0]["local"]),
        )

    def test_migration_conflict_defaults_to_skip_and_repoints_goal_link(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()
        goal = goal_service.create_goal("默认跳过仍保留绑定", None, "short")
        local = service.create_event(
            subject="已有会议",
            event_date=date(2026, 7, 16),
            start_time=time(9, 0),
            end_time=time(10, 0),
            account_id="local",
            goal_id=goal["id"],
        )
        self._insert_outlook_event(
            "existing-event",
            " 已有会议 ",
            datetime(2026, 7, 16, 9, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        result = service.migrate_local_events({})

        self.assertEqual(
            {
                "status": "ok",
                "migrated": 0,
                "skipped": 1,
                "remaining": 0,
                "account_removed": True,
            },
            result,
        )
        self.assertEqual([], graph.created_payloads)
        self.assertIsNone(
            db.query_one("SELECT 1 FROM schedule_events WHERE id = ?", (local["id"],))
        )
        self.assertEqual(
            ["existing-event"],
            [event["id"] for event in goal_schedule_service.links_for_goal(goal["id"])],
        )
        self.assertIsNone(
            db.query_one("SELECT 1 FROM calendar_accounts WHERE id = 'local'")
        )
        self.assertEqual(
            "1",
            db.query_one(
                "SELECT value FROM meta WHERE key = 'schedule_local_migration_prompted'"
            )["value"],
        )

    def test_migration_create_decision_repoints_goal_link_to_new_event(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()
        goal = goal_service.create_goal("强制新建仍保留绑定", None, "short")
        local = service.create_event(
            subject="重复但仍创建",
            event_date=date(2026, 7, 16),
            account_id="local",
            goal_id=goal["id"],
        )
        self._insert_outlook_event(
            "existing-event",
            "重复但仍创建",
            datetime(2026, 7, 16, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        result = service.migrate_local_events({local["id"]: "create"})

        self.assertEqual(1, result["migrated"])
        self.assertEqual(0, result["skipped"])
        self.assertTrue(result["account_removed"])
        self.assertEqual(
            ["created-1"],
            [event["id"] for event in goal_schedule_service.links_for_goal(goal["id"])],
        )
        self.assertIsNotNone(
            db.query_one(
                "SELECT 1 FROM schedule_events WHERE id = 'created-1' AND account_id = 'outlook'"
            )
        )

    def test_migration_graph_failure_returns_partial_and_preserves_remaining_rows_and_links(self) -> None:
        graph = FakeGraph(fail_create_at=2)
        service = self._service(graph)
        service.create_local_account()
        goal = goal_service.create_goal("断点续传目标", None, "short")
        local_events = [
            service.create_event(
                subject=f"迁移第 {index} 条",
                event_date=date(2026, 7, 16),
                start_time=time(7 + index, 0),
                end_time=time(8 + index, 0),
                account_id="local",
                goal_id=goal["id"],
            )
            for index in range(1, 4)
        ]

        result = service.migrate_local_events({})

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["migrated"])
        self.assertEqual(0, result["skipped"])
        self.assertEqual(2, result["remaining"])
        self.assertFalse(result["account_removed"])
        self.assertTrue(result["error"])
        remaining_ids = {
            str(row["id"])
            for row in db.query_all(
                "SELECT id FROM schedule_events WHERE account_id = 'local'"
            )
        }
        self.assertEqual(
            {local_events[1]["id"], local_events[2]["id"]},
            remaining_ids,
        )
        linked_ids = {
            event["id"] for event in goal_schedule_service.links_for_goal(goal["id"])
        }
        self.assertEqual({"created-1", *remaining_ids}, linked_ids)
        self.assertIsNotNone(
            db.query_one("SELECT 1 FROM calendar_accounts WHERE id = 'local'")
        )
        self.assertIsNone(
            db.query_one(
                "SELECT 1 FROM meta WHERE key = 'schedule_local_migration_prompted'"
            )
        )

    def test_migration_retry_reuses_transaction_id_derived_from_local_event(self) -> None:
        graph = FakeGraph(fail_create_at=1)
        service = self._service(graph)
        service.create_local_account()
        local = service.create_event(
            subject="超时后重跑迁移",
            event_date=date(2026, 7, 16),
            account_id="local",
        )

        first = service.migrate_local_events({})
        second = service.migrate_local_events({})

        self.assertEqual("partial", first["status"])
        self.assertEqual("ok", second["status"])
        transaction_ids = [payload["transactionId"] for payload in graph.created_payloads]
        self.assertEqual(2, len(transaction_ids))
        self.assertEqual(transaction_ids[0], transaction_ids[1])
        uuid.UUID(transaction_ids[0])
        self.assertNotEqual(local["id"], transaction_ids[0])

    def test_migration_prompt_pending_checks_each_condition_and_dismisses(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()
        service.create_event(
            subject="等待迁移",
            event_date=date(2026, 7, 16),
            account_id="local",
        )

        self.assertTrue(service.status()["migration_prompt_pending"])

        self.auth.connected = False
        self.assertFalse(service.status()["migration_prompt_pending"])

        self.auth.connected = True
        service.delete_local_account(delete_events=True)
        self.assertFalse(service.status()["migration_prompt_pending"])

        service.create_local_account()
        self.assertFalse(service.status()["migration_prompt_pending"])

        service.create_event(
            subject="再次等待迁移",
            event_date=date(2026, 7, 16),
            account_id="local",
        )
        self.assertTrue(service.status()["migration_prompt_pending"])
        service.dismiss_migration_prompt()
        self.assertFalse(service.status()["migration_prompt_pending"])

    def test_migration_keeps_weekly_goal_progress_and_ignores_clean_item_decision(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()
        goal = goal_service.create_goal("迁移周进度", None, "short")
        local = service.create_event(
            subject="本周执行",
            event_date=date(2026, 7, 16),
            account_id="local",
            goal_id=goal["id"],
        )
        now = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        before = goal_schedule_service.weekly_progress(goal["id"], now=now)

        result = service.migrate_local_events({local["id"]: "skip"})
        after = goal_schedule_service.weekly_progress(goal["id"], now=now)

        self.assertEqual(1, before["current"])
        self.assertEqual(before["current"], after["current"])
        self.assertEqual(1, result["migrated"])
        self.assertEqual(0, result["skipped"])
        self.assertEqual(
            ["created-1"],
            [event["id"] for event in goal_schedule_service.links_for_goal(goal["id"])],
        )

    def test_outlook_logout_removes_only_outlook_events(self) -> None:
        graph = FakeGraph()
        service = self._service(graph)
        service.create_local_account()
        local = service.create_event(
            subject="退出后保留",
            event_date=date(2026, 7, 16),
            account_id="local",
        )
        outlook = service.create_event(
            subject="退出后清理",
            event_date=date(2026, 7, 16),
        )

        service.logout()

        self.assertTrue(self.auth.logged_out)
        self.assertIsNotNone(
            db.query_one("SELECT 1 FROM schedule_events WHERE id = ?", (local["id"],))
        )
        self.assertIsNone(
            db.query_one("SELECT 1 FROM schedule_events WHERE id = ?", (outlook["id"],))
        )


if __name__ == "__main__":
    unittest.main()
