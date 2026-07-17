from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datetime import datetime
from zoneinfo import ZoneInfo

from core import db, goal_service, suggestion_pipeline
from core.llm import suggestion_router


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
