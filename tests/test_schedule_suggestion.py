from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from core import db, goal_schedule_service, suggestion_service
from core.graph.client import GraphHTTPError
from core.schedule_service import NoWritableAccountError, ScheduleService


class DisconnectedAuth:
    def client_id(self):
        return None

    def get_access_token(self):
        return None


class FakeScheduleService:
    def __init__(
        self,
        outcomes=None,
        *,
        accounts=None,
        recovered=None,
        create_local_error: Exception | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.accounts = list(accounts or [])
        self.recovered = recovered
        self.create_local_error = create_local_error
        self.create_calls: list[dict] = []
        self.create_local_calls = 0
        self.sync_calls = 0

    def create_event(self, **kwargs):
        self.create_calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else {"id": "event-1"}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def create_local_account(self):
        self.create_local_calls += 1
        if self.create_local_error is not None:
            raise self.create_local_error
        self.accounts = [
            {
                "id": "local",
                "provider": "local",
                "display_name": "本地日历",
                "event_count": 0,
            }
        ]
        return self.accounts[0]

    def list_accounts(self):
        return self.accounts

    def sync(self):
        self.sync_calls += 1
        return {"ok": True}

    def list_events(self, start, end):
        del start, end
        return {"events": [self.recovered] if self.recovered is not None else []}


class ScheduleSuggestionServiceTest(unittest.TestCase):
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

    def _create_schedule(self, **overrides):
        payload = {
            "subject": " 打疫苗 ",
            "date": "2026-07-20",
            "start_time": "15:00",
            "end_time": None,
            "all_day": False,
            "goal_id": None,
            **overrides,
        }
        return suggestion_service.create_suggestion(
            "schedule", payload, "chat:12", 0.9
        )

    def test_schedule_normalize_and_list_pending_kind_filter(self) -> None:
        schedule = self._create_schedule()
        goal = suggestion_service.create_suggestion(
            "goal", {"title": "准备考试", "horizon": "long"}, "chat:13", 0.8
        )

        self.assertEqual(
            {
                "subject": "打疫苗",
                "date": "2026-07-20",
                "start_time": "15:00",
                "end_time": None,
                "all_day": False,
                "goal_id": None,
            },
            schedule["payload"],
        )
        self.assertEqual([schedule["id"]], [item["id"] for item in suggestion_service.list_pending("schedule")])
        self.assertEqual([goal["id"]], [item["id"] for item in suggestion_service.list_pending("goal")])
        self.assertEqual(2, len(suggestion_service.list_pending()))

    def test_schedule_normalize_rejects_invalid_date_time_and_subject(self) -> None:
        invalid_payloads = [
            {"subject": "", "date": "2026-07-20", "all_day": True},
            {"subject": "x", "date": "2026-02-30", "all_day": True},
            {
                "subject": "x",
                "date": "2026-07-20",
                "start_time": "15:60",
                "all_day": False,
            },
            {
                "subject": "x",
                "date": "2026-07-20",
                "start_time": "15:00",
                "end_time": "14:00",
                "all_day": False,
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                suggestion_service.create_suggestion("schedule", payload, "chat:1")

    def test_goal_normalized_key_remains_byte_compatible_for_unicode_inputs(self) -> None:
        self.assertEqual(
            "sha256:b0e842cba1f2d792bccd96594709a812eae533436895565e94ef1479d187a85a",
            suggestion_service.normalized_key_for(
                "goal",
                {"title": "  Ａ_考-研 café é  ", "horizon": "long"},
                "chat:12",
            ),
        )
        self.assertEqual(
            "sha256:c47e6b6bf2fa7773676e22d3b738b161fd7ff4a20b4ca50bedf77b57065478d4",
            suggestion_service.normalized_key_for(
                "goal",
                {"title": "Straße／研究", "horizon": "short"},
                "comment:99",
            ),
        )

    def test_schedule_normalized_key_uses_subject_date_start_and_source(self) -> None:
        payload = self._create_schedule()["payload"]
        same_source = suggestion_service.normalized_key_for("schedule", payload, "chat:999")
        original = suggestion_service.normalized_key_for("schedule", payload, "chat:12")
        changed = suggestion_service.normalized_key_for(
            "schedule", {**payload, "start_time": "16:00"}, "chat:12"
        )
        self.assertEqual(original, same_source)
        self.assertNotEqual(original, changed)

    def test_goal_only_schema_migrates_without_losing_existing_suggestions(self) -> None:
        with db.transaction() as conn:
            conn.execute("DROP TABLE suggestions")
            conn.execute(
                """
                CREATE TABLE suggestions (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind = 'goal'),
                    payload_json TEXT NOT NULL,
                    evidence_ref TEXT,
                    confidence REAL NOT NULL DEFAULT 0.6,
                    status TEXT NOT NULL DEFAULT 'pending',
                    normalized_key TEXT,
                    created_at REAL NOT NULL,
                    decided_at REAL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO suggestions(
                    id, kind, payload_json, confidence, status, normalized_key, created_at
                ) VALUES ('old-goal', 'goal', '{}', 0.8, 'pending', 'old-key', 1)
                """
            )

        db.init_db()

        table_sql = db.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'suggestions'"
        )["sql"]
        self.assertIn("schedule", table_sql)
        self.assertIsNotNone(db.query_one("SELECT 1 FROM suggestions WHERE id = 'old-goal'"))
        self.assertIsNotNone(self._create_schedule())

    def test_accept_schedule_keeps_external_write_outside_sqlite_transaction(self) -> None:
        suggestion = self._create_schedule()
        service = FakeScheduleService([{"id": "event-1"}])
        original_transaction = db.immediate_transaction
        state = {"active": False}

        @contextmanager
        def tracked_transaction():
            self.assertFalse(state["active"])
            state["active"] = True
            try:
                with original_transaction() as conn:
                    yield conn
            finally:
                state["active"] = False

        original_create = service.create_event

        def checked_create(**kwargs):
            self.assertFalse(state["active"])
            return original_create(**kwargs)

        service.create_event = checked_create  # type: ignore[method-assign]
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ), patch("core.suggestion_service.db.immediate_transaction", tracked_transaction):
            result = suggestion_service.accept(suggestion["id"])

        self.assertEqual("accepted", result["suggestion"]["status"])
        self.assertEqual(suggestion["id"], service.create_calls[0]["client_request_id"])
        self.assertIsNone(service.create_calls[0]["account_id"])

    def test_no_writable_account_remains_pending_until_explicit_local_fallback(self) -> None:
        suggestion = self._create_schedule()
        service = FakeScheduleService(
            [
                NoWritableAccountError("none"),
                NoWritableAccountError("none"),
                {"id": "local-event"},
            ]
        )
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ):
            with self.assertRaises(NoWritableAccountError):
                suggestion_service.accept(suggestion["id"])
            self.assertEqual("pending", suggestion_service.get_suggestion(suggestion["id"])["status"])
            result = suggestion_service.accept(suggestion["id"], fallback_local=True)

        self.assertEqual("accepted", result["suggestion"]["status"])
        self.assertEqual(1, service.create_local_calls)
        self.assertEqual("local", service.create_calls[-1]["account_id"])
        self.assertEqual(
            service.create_calls[0]["client_request_id"],
            service.create_calls[-1]["client_request_id"],
        )

    def test_local_fallback_tolerates_account_created_by_a_concurrent_request(self) -> None:
        suggestion = self._create_schedule()
        service = FakeScheduleService(
            [NoWritableAccountError("none"), {"id": "local-event"}],
            accounts=[{"id": "local"}],
            create_local_error=ValueError("本地日历已存在"),
        )
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ):
            result = suggestion_service.accept(suggestion["id"], fallback_local=True)
        self.assertEqual("accepted", result["suggestion"]["status"])
        self.assertEqual("local", service.create_calls[-1]["account_id"])

    def test_graph_409_is_treated_as_same_transaction_and_recovers_cache(self) -> None:
        suggestion = self._create_schedule()
        recovered = {
            "id": "graph-event",
            "subject": "打疫苗",
            "start_ts": datetime(
                2026, 7, 20, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp(),
        }
        service = FakeScheduleService([GraphHTTPError(409)], recovered=recovered)
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ):
            result = suggestion_service.accept(suggestion["id"])

        self.assertEqual(recovered, result["created"])
        self.assertEqual("accepted", result["suggestion"]["status"])
        self.assertEqual(1, service.sync_calls)
        self.assertEqual(suggestion["id"], service.create_calls[0]["client_request_id"])
        with self.assertRaisesRegex(ValueError, "已处理"):
            suggestion_service.accept(suggestion["id"])
        self.assertEqual(1, len(service.create_calls))

    def test_graph_409_cache_miss_still_marks_suggestion_accepted(self) -> None:
        suggestion = self._create_schedule()
        service = FakeScheduleService([GraphHTTPError(409)])
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ):
            result = suggestion_service.accept(suggestion["id"])
        self.assertIsNone(result["created"])
        self.assertEqual("accepted", result["suggestion"]["status"])

    def test_graph_409_recovery_sync_failure_does_not_recreate_or_block_accept(self) -> None:
        suggestion = self._create_schedule()
        service = FakeScheduleService([GraphHTTPError(409)])
        service.sync = Mock(side_effect=GraphHTTPError(503))  # type: ignore[method-assign]
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ):
            result = suggestion_service.accept(suggestion["id"])
        self.assertIsNone(result["created"])
        self.assertEqual("accepted", result["suggestion"]["status"])
        self.assertEqual(1, len(service.create_calls))

    def test_expiration_uses_asia_shanghai_day_boundary(self) -> None:
        suggestion = self._create_schedule(
            date="2026-07-18",
            start_time=None,
            end_time=None,
            all_day=True,
        )
        service_factory = Mock()
        with patch("core.suggestion_service.ScheduleService", service_factory), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 18, 16, 30, tzinfo=timezone.utc),
        ):
            with self.assertRaises(suggestion_service.SuggestionExpiredError):
                suggestion_service.accept(suggestion["id"])
        service_factory.assert_not_called()
        self.assertEqual("pending", suggestion_service.get_suggestion(suggestion["id"])["status"])

    def test_local_event_id_is_deterministic_for_client_request_id(self) -> None:
        service = ScheduleService(auth=DisconnectedAuth(), clock=lambda: 1.0)
        service.create_local_account()
        request_id = "s_retry_中文"

        first = service.create_event(
            subject="第一次",
            event_date=datetime(2026, 7, 20).date(),
            account_id="local",
            client_request_id=request_id,
        )
        second = service.create_event(
            subject="第一次",
            event_date=datetime(2026, 7, 20).date(),
            account_id="local",
            client_request_id=request_id,
        )

        expected = f"local_{hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:32]}"
        self.assertEqual(expected, first["id"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            1,
            db.query_one("SELECT COUNT(*) AS count FROM schedule_events")["count"],
        )

    def test_local_accept_retry_converges_and_missing_end_defaults_to_one_hour(self) -> None:
        suggestion = self._create_schedule()
        service = ScheduleService(auth=DisconnectedAuth(), clock=lambda: 1.0)
        service.create_local_account()
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ):
            first = suggestion_service.accept(suggestion["id"])
            db.execute(
                "UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE id = ?",
                (suggestion["id"],),
            )
            second = suggestion_service.accept(suggestion["id"])

        self.assertEqual(first["created"]["id"], second["created"]["id"])
        self.assertEqual("2026-07-20T16:00:00", second["created"]["end_local"])
        self.assertEqual(1, db.query_one("SELECT COUNT(*) AS count FROM schedule_events")["count"])

    def test_goal_id_is_deferred_to_accept_and_invalid_link_keeps_pending(self) -> None:
        suggestion = self._create_schedule(goal_id="missing-goal")
        service = ScheduleService(auth=DisconnectedAuth(), clock=lambda: 1.0)
        service.create_local_account()
        with patch("core.suggestion_service.ScheduleService", return_value=service), patch(
            "core.suggestion_service._now_local",
            return_value=datetime(2026, 7, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ):
            with self.assertRaises(goal_schedule_service.GoalNotFoundError):
                suggestion_service.accept(suggestion["id"])
        self.assertEqual("pending", suggestion_service.get_suggestion(suggestion["id"])["status"])
        self.assertEqual(0, db.query_one("SELECT COUNT(*) AS count FROM schedule_events")["count"])


if __name__ == "__main__":
    unittest.main()
