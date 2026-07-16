from __future__ import annotations

import tempfile
import unittest
from datetime import date, time
from pathlib import Path

from core import db
from core import goal_schedule_service, goal_service
from core.graph.client import GraphHTTPError
from core.schedule_service import ScheduleNotConnectedError, ScheduleService


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
    def __init__(self, delta_results=None) -> None:
        self.delta_results = list(delta_results or [])
        self.delta_calls: list[dict] = []
        self.created_payloads: list[dict] = []

    def calendarview_delta(self, **kwargs):
        self.delta_calls.append(kwargs)
        result = self.delta_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def create_event(self, payload):
        self.created_payloads.append(payload)
        return graph_event("created-1", payload["subject"], 14)

    def update_event(self, event_id, payload):
        return graph_event(event_id, payload.get("subject", "updated"), 15)

    def delete_event(self, event_id):
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
        self.clock_values = iter([1000.0, 2000.0, 3000.0, 4000.0])

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

    def test_410_discards_expired_delta_and_replaces_full_cache(self) -> None:
        graph = FakeGraph(
            [
                GraphHTTPError(410),
                {"events": [graph_event("fresh", "全量恢复")], "delta_link": "new-delta"},
            ]
        )
        service = self._service(graph)
        db.execute(
            """
            INSERT INTO schedule_events(
                id, subject, start_ts, end_ts, start_local, end_local, synced_at
            ) VALUES ('stale', '旧缓存', 1, 2, '2026-01-01T00:00:00', '2026-01-01T01:00:00', 1)
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
        )
        listed = service.list_events(date(2026, 7, 16), date(2026, 7, 16))

        self.assertEqual("created-1", created["id"])
        self.assertEqual("评审 P3", graph.created_payloads[0]["subject"])
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


if __name__ == "__main__":
    unittest.main()
