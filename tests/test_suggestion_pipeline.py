from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, goal_service, suggestion_pipeline


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
            with patch("core.suggestion_pipeline.goal_router.call_goal_router") as router:
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
            "core.suggestion_pipeline.goal_router.call_goal_router",
            return_value=[candidate],
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
            "core.suggestion_pipeline.goal_router.call_goal_router",
            return_value=[duplicate_candidate],
        ):
            duplicate = suggestion_pipeline.collect_goal_suggestions(
                user_input="继续",
                evidence_ref="chat:2",
                client=object(),
                model="m",
            )
        self.assertEqual([], duplicate)
