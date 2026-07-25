from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from datetime import datetime
from zoneinfo import ZoneInfo

from core import db, goal_service, suggestion_pipeline
from core.llm import suggestion_router


class FakeSuggestionRouterClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(self.payload, ensure_ascii=False)
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


class SuggestionPipelineTest(unittest.TestCase):
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

    def test_disabled_when_env_explicitly_off(self) -> None:
        env = {suggestion_pipeline.GOAL_SUGGESTIONS_ENABLED_ENV: "0"}
        with patch.dict(os.environ, env):
            with patch("core.suggestion_pipeline.suggestion_router.call_suggestion_router") as router:
                self.assertEqual(
                    [],
                    suggestion_pipeline.collect_goal_suggestions(
                        user_input="我决定考研",
                        evidence_ref="chat:1",
                        client=object(),
                        model="m",
                    ),
                )
        router.assert_not_called()

    def test_enabled_persists_candidates_and_skips_existing_goal(self) -> None:
        env = {suggestion_pipeline.GOAL_SUGGESTIONS_ENABLED_ENV: "1"}
        candidate = {
            "title": "准备考研",
            "detail": None,
            "horizon": "long",
            "confidence": 0.9,
        }
        with patch.dict(os.environ, env), patch(
            "core.suggestion_pipeline.suggestion_router.call_suggestion_router",
            return_value={"goals": [candidate], "events": []},
        ):
            created = suggestion_pipeline.collect_goal_suggestions(
                user_input="我决定考研",
                evidence_ref="chat:1",
                client=object(),
                model="m",
            )
            self.assertEqual(1, len(created))
            goal_service.create_goal("另一个目标", None, "long")
            goal_service.create_goal("已经存在", None, "long")

        duplicate_candidate = {**candidate, "title": "已经存在"}
        with patch.dict(os.environ, env), patch(
            "core.suggestion_pipeline.suggestion_router.call_suggestion_router",
            return_value={"goals": [duplicate_candidate], "events": []},
        ):
            duplicate = suggestion_pipeline.collect_goal_suggestions(
                user_input="继续",
                evidence_ref="chat:2",
                client=object(),
                model="m",
            )
        self.assertEqual([], duplicate)

    def test_combined_router_parses_valid_events_and_rejects_invalid_or_duplicate(self) -> None:
        payload = {
            "goals": [],
            "events": [
                {
                    "subject": "打疫苗",
                    "date": "2026-07-18",
                    "start_time": "15:00",
                    "end_time": "16:00",
                    "all_day": False,
                    "confidence": 0.9,
                },
                {
                    "subject": "打疫苗",
                    "date": "2026-07-18",
                    "start_time": "15:00",
                    "end_time": "16:30",
                    "all_day": False,
                    "confidence": 0.8,
                },
                {
                    "subject": "坏日期",
                    "date": "2026-02-30",
                    "start_time": None,
                    "end_time": None,
                    "all_day": True,
                    "confidence": 0.9,
                },
                {
                    "subject": "倒置时间",
                    "date": "2026-07-18",
                    "start_time": "16:00",
                    "end_time": "15:00",
                    "all_day": False,
                    "confidence": 0.9,
                },
                {
                    "subject": "已经过去",
                    "date": "2026-07-16",
                    "start_time": None,
                    "end_time": None,
                    "all_day": True,
                    "confidence": 0.9,
                },
            ],
        }
        parsed = suggestion_router._parse_suggestion_router_content(
            json.dumps(payload, ensure_ascii=False),
            now=datetime(2026, 7, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            [
                {
                    "subject": "打疫苗",
                    "date": "2026-07-18",
                    "start_time": "15:00",
                    "end_time": "16:00",
                    "all_day": False,
                    "confidence": 0.9,
                }
            ],
            parsed["events"],
        )

    def test_activity_parser_requires_evidence_and_half_confidence(self) -> None:
        payload = {
            "goals": [],
            "events": [],
            "activities": [
                {
                    "goal_id": "g_valid",
                    "kind": "progress",
                    "evidence_span": "最近每天刷科目一的题",
                    "confidence": 0.5,
                },
                {
                    "goal_id": "g_missing_evidence",
                    "kind": "progress",
                    "confidence": 0.9,
                },
                {
                    "goal_id": "g_low_confidence",
                    "kind": "blocked",
                    "evidence_span": "卡在第三题",
                    "confidence": 0.49,
                },
                {
                    "goal_id": "g_scheduled",
                    "kind": "scheduled",
                    "evidence_span": "下周一要去体检",
                    "confidence": 0.9,
                },
            ],
        }

        parsed = suggestion_router._parse_suggestion_router_content(
            json.dumps(payload, ensure_ascii=False),
            user_input="最近每天刷科目一的题，不过卡在第三题，下周一要去体检",
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            [
                {
                    "goal_id": "g_valid",
                    "kind": "progress",
                    "evidence_span": "最近每天刷科目一的题",
                    "confidence": 0.5,
                }
            ],
            parsed["activities"],
        )

    def test_activity_parser_drops_quotes_absent_from_user_input(self) -> None:
        """A fabricated quote is worse than a missed activity: the timeline would
        show the user a sentence they never wrote, so the span must be verbatim."""
        payload = {
            "goals": [],
            "events": [],
            "activities": [
                {
                    "goal_id": "g_paraphrased",
                    "kind": "progress",
                    "evidence_span": "他每天都在认真复习科目一",
                    "confidence": 0.95,
                },
                {
                    "goal_id": "g_stitched",
                    "kind": "progress",
                    "evidence_span": "报名驾校每天刷题",
                    "confidence": 0.95,
                },
                {
                    "goal_id": "g_verbatim",
                    "kind": "progress",
                    "evidence_span": "最近每天刷科目一的 题",
                    "confidence": 0.6,
                },
            ],
        }

        parsed = suggestion_router._parse_suggestion_router_content(
            json.dumps(payload, ensure_ascii=False),
            user_input="这周二报名驾校了，最近每天刷科目一的题",
        )

        assert parsed is not None
        # Paraphrase and stitched-together fragments are dropped; only differing
        # whitespace survives, since reformatting is harmless.
        self.assertEqual(["g_verbatim"], [item["goal_id"] for item in parsed["activities"]])

    def test_activity_parser_drops_everything_without_user_input(self) -> None:
        payload = {
            "goals": [],
            "events": [],
            "activities": [
                {
                    "goal_id": "g_unverifiable",
                    "kind": "progress",
                    "evidence_span": "最近每天刷科目一的题",
                    "confidence": 0.99,
                }
            ],
        }

        parsed = suggestion_router._parse_suggestion_router_content(
            json.dumps(payload, ensure_ascii=False)
        )

        assert parsed is not None
        self.assertEqual([], parsed["activities"])

    def test_reply_pipeline_private_chat_never_persists_activity(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        activity = {
            "goal_id": goal["id"],
            "kind": "progress",
            "evidence_span": "最近每天刷科目一的题",
            "confidence": 0.9,
        }
        with patch(
            "core.suggestion_pipeline.suggestion_router.call_suggestion_router",
            return_value={"goals": [], "events": [], "activities": [activity]},
        ) as router:
            result = suggestion_pipeline.collect_reply_suggestions(
                user_input="最近每天刷科目一的题",
                evidence_ref="chat:42",
                client=object(),
                model="m",
            )

        self.assertEqual([], result)
        self.assertEqual(
            0,
            db.query_one("SELECT COUNT(*) AS count FROM goal_activities")["count"],
        )
        router.assert_called_once()

    def test_reply_pipeline_persists_public_activity_with_goal_context(self) -> None:
        goal = goal_service.create_goal("考驾照", None, "short")
        activity = {
            "goal_id": goal["id"],
            "kind": "progress",
            "evidence_span": "最近每天刷科目一的题",
            "confidence": 0.9,
        }
        with patch(
            "core.suggestion_pipeline.suggestion_router.call_suggestion_router",
            return_value={"goals": [], "events": [], "activities": [activity]},
        ) as router:
            result = suggestion_pipeline.collect_reply_suggestions(
                user_input="最近每天刷科目一的题",
                evidence_ref="post:p-activity",
                client=object(),
                model="m",
                context="公开 post",
            )

        self.assertEqual([], result)
        row = db.query_one("SELECT * FROM goal_activities")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(goal["id"], row["goal_id"])
        self.assertEqual("auto", row["source"])
        self.assertEqual("最近每天刷科目一的题", row["evidence_span"])
        self.assertIn(
            f"[{goal['id']}]",
            router.call_args.kwargs["context"],
        )

    def test_reply_pipeline_does_not_write_unknown_goal_id(self) -> None:
        activity = {
            "goal_id": "g_missing",
            "kind": "progress",
            "evidence_span": "最近每天刷科目一的题",
            "confidence": 0.9,
        }
        with patch(
            "core.suggestion_pipeline.suggestion_router.call_suggestion_router",
            return_value={"goals": [], "events": [], "activities": [activity]},
        ):
            suggestion_pipeline.collect_reply_suggestions(
                user_input="最近每天刷科目一的题",
                evidence_ref="post:p-missing-goal",
                client=object(),
                model="m",
            )

        self.assertEqual(
            0,
            db.query_one("SELECT COUNT(*) AS count FROM goal_activities")["count"],
        )

    def test_one_router_call_returns_goals_events_and_activities(self) -> None:
        client = FakeSuggestionRouterClient(
            {
                "goals": [
                    {
                        "title": "完成驾考",
                        "detail": None,
                        "horizon": "short",
                        "confidence": 0.9,
                    }
                ],
                "events": [
                    {
                        "subject": "驾考体检",
                        "date": "2099-07-28",
                        "start_time": "09:00",
                        "end_time": "10:00",
                        "all_day": False,
                        "confidence": 0.9,
                    }
                ],
                "activities": [
                    {
                        "goal_id": "g_drive",
                        "kind": "progress",
                        "evidence_span": "最近每天刷科目一的题",
                        "confidence": 0.8,
                    }
                ],
            }
        )

        parsed = suggestion_router.call_suggestion_router(
            client,
            "m",
            user_input="最近每天刷科目一的题，下周一要去体检",
            context="# 当前目标\n\n- [g_drive] 考驾照（短期）",
        )

        self.assertEqual(1, len(client.calls))
        self.assertEqual(1, len(parsed["goals"]))
        self.assertEqual(1, len(parsed["events"]))
        self.assertEqual(1, len(parsed["activities"]))

    def test_reply_pipeline_skips_router_when_both_kinds_disabled(self) -> None:
        env = {
            suggestion_pipeline.GOAL_SUGGESTIONS_ENABLED_ENV: "0",
            suggestion_pipeline.SCHEDULE_SUGGESTIONS_ENABLED_ENV: "0",
        }
        with patch.dict(os.environ, env), patch(
            "core.suggestion_pipeline.suggestion_router.call_suggestion_router"
        ) as router:
            result = suggestion_pipeline.collect_reply_suggestions(
                user_input="明天下午三点去打疫苗",
                evidence_ref="chat:3",
                client=object(),
                model="m",
            )
        self.assertEqual([], result)
        router.assert_not_called()

    def test_reply_pipeline_persists_schedule_when_only_schedule_is_enabled(self) -> None:
        event = {
            "subject": "打疫苗",
            "date": "2026-07-20",
            "start_time": "15:00",
            "end_time": "16:00",
            "all_day": False,
            "confidence": 0.9,
        }
        env = {
            suggestion_pipeline.GOAL_SUGGESTIONS_ENABLED_ENV: "0",
            suggestion_pipeline.SCHEDULE_SUGGESTIONS_ENABLED_ENV: "1",
        }
        with patch.dict(os.environ, env), patch(
            "core.suggestion_pipeline.suggestion_router.call_suggestion_router",
            return_value={"goals": [], "events": [event]},
        ) as router:
            result = suggestion_pipeline.collect_reply_suggestions(
                user_input="周一下午三点去打疫苗",
                evidence_ref="chat:4",
                client=object(),
                model="m",
            )
        self.assertEqual(1, len(result))
        self.assertEqual("schedule", result[0]["kind"])
        self.assertEqual("打疫苗", result[0]["payload"]["subject"])
        router.assert_called_once()
