from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from unittest.mock import patch

from core import context_builder, db, goal_schedule_service, goal_service, logging_service, memory_unit_service, memory_view_service, query_rewriter, schedule_context, soul_service, turn_prep, web_search_gate, web_search_service
from core.schedule_service import ScheduleService


class ConnectedScheduleAuth:
    def client_id(self):
        return "client-id"

    def get_access_token(self):
        return "access-token"


class DisconnectedScheduleAuth:
    def client_id(self):
        return None

    def get_access_token(self):
        return None


class ContextBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_web_search_config = web_search_service.CONFIG_FILE

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        soul_service.SOULS_DIR = self.workspace / "souls"
        web_search_service.CONFIG_FILE = str(Path(self.tmp.name) / "config.json")

        db.init_db()
        logging_service.init_logging({"enabled": True})
        self.workspace.mkdir(parents=True, exist_ok=True)
        soul_service.sync_souls()
        memory_unit_service.add_unit(
            owner_scope="global",
            visibility_scope="public",
            source_channel="user",
            type="identity",
            content="测试用户",
            confidence=1.0,
            tier="core",
            importance=1.0,
            source="user_authored",
            actor="user",
        )
        memory_view_service.synthesize_view(
            "global", "public", memory_view_service.VIEW_USER_PORTRAIT
        )

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        soul_service.SOULS_DIR = self.old_souls_dir
        web_search_service.CONFIG_FILE = self.old_web_search_config
        self.tmp.cleanup()

    def test_public_context_uses_goals_without_portrait_or_related_posts(self) -> None:
        goal_service.create_goal("跨专业考研", None, "long")
        goal_service.create_goal("完成课程项目", None, "short", focus=True)

        built = context_builder.build_context(query="随便")

        # portrait + the rest of memory-v2 now come per-soul via # 记忆 downstream,
        # so the shared context carries neither the portrait nor any retrieval block
        self.assertNotIn("# 用户档案", built.shared_context)
        self.assertNotIn("测试用户", built.shared_context)
        self.assertNotIn("# 当前用户的历史相关帖子", built.shared_context)
        self.assertNotIn("# 相关记忆", built.shared_context)
        self.assertNotIn("# 记忆", built.shared_context)
        self.assertIn("# 长期目标", built.shared_context)
        self.assertIn("跨专业考研", built.shared_context)
        self.assertIn("# 当前状态", built.shared_context)
        self.assertIn("完成课程项目", built.shared_context)

        event = self._last_event("context_assembly_result")
        self.assertEqual("public_post", event["context_type"])
        # No calendar account exists in this fixture, so the schedule section is
        # gated off entirely — a user who never enabled the schedule feature gets
        # no schedule block.
        self.assertEqual(
            ["# 长期目标", "# 当前状态"],
            [item["title"] for item in event["sections"]],
        )

    def test_public_context_injects_web_search_once_for_enabled_souls(self) -> None:
        settings = web_search_service.WebSearchConfig(
            enabled=True,
            provider="tavily",
            tavily_api_key="tavily-key",
            max_results=5,
            timeout_s=8,
            cache_ttl_s=0,
        )
        decision = web_search_gate.WebSearchDecision(
            should_search=True,
            queries=["OpenAI latest model"],
            reason="当前公开事实",
            freshness_required=True,
        )
        run = web_search_service.WebSearchRun(
            used=True,
            provider="tavily",
            queries=decision.queries,
            results=[
                web_search_service.WebSearchResult(
                    title="OpenAI",
                    url="https://example.com/openai",
                    snippet="最新公开信息",
                    provider="tavily",
                )
            ],
            error=None,
            elapsed_ms=1,
        )

        # Gate + rewrite now share one merged turn-prep call; the merged JSON carries
        # both halves, and the public-post context must fire it exactly once.
        merged = {
            "should_search": True,
            "queries": ["OpenAI latest model"],
            "reason": "当前公开事实",
            "freshness_required": True,
            "semantic_query": "用户询问 OpenAI 最新模型",
            "keywords": ["OpenAI"],
        }
        with (
            patch("core.reply_context.web_search_service.effective_config", return_value=settings),
            patch("core.llm.turn_prep_router.call_turn_prep", return_value=merged) as call_turn_prep,
            patch("core.reply_context.web_search_service.search", return_value=run) as search,
        ):
            built = context_builder.build_context(
                query="今天 OpenAI 最新模型是什么",
                client=object(),
                model="fake-model",
                trace_context={"post_id": "post-1"},
            )

        self.assertIn("# 网页搜索结果", built.shared_context)
        self.assertIn("OpenAI", built.shared_context)
        self.assertIn("最新公开信息", built.shared_context)
        call_turn_prep.assert_called_once()
        search.assert_called_once()

    def test_public_context_injects_recent_before_prep_and_mentions_after_rewrite(self) -> None:
        recent = schedule_context.RecentScheduleContext(
            section="# 近期日程\n\n本周共 1 项安排",
            event_ids=frozenset({"recent-event"}),
        )
        rewrite = query_rewriter.RewrittenQuery(
            raw_query="聊聊马拉松",
            semantic_query="聊聊马拉松",
            keywords=["马拉松"],
            used_rewrite=True,
        )
        prep = turn_prep.TurnPrep(
            rewritten=rewrite,
            search_decision=web_search_gate.default_decision("disabled"),
        )
        with (
            patch(
                "core.context_builder.schedule_context.build_recent_schedule_context",
                return_value=recent,
            ),
            patch("core.context_builder.turn_prep.prepare_turn", return_value=prep) as prepare,
            patch(
                "core.context_builder.schedule_context.build_mentioned_schedule_section",
                return_value="# 提及的日程\n\n- [3 天后] 马拉松",
            ) as mentioned,
        ):
            built = context_builder.build_context(
                query="聊聊马拉松",
                client=object(),
                model="fake-model",
                today=date(2026, 7, 17),
            )

        hint = prepare.call_args.kwargs["context_hint"]
        self.assertIn("# 近期日程", hint)
        self.assertNotIn("# 提及的日程", hint)
        mentioned.assert_called_once_with(
            ["马拉松"],
            exclude_event_ids=frozenset({"recent-event"}),
            context_date=date(2026, 7, 17),
        )
        self.assertIn("# 近期日程", built.shared_context)
        self.assertIn("# 提及的日程", built.shared_context)
        self.assertLess(
            built.shared_context.index("# 近期日程"),
            built.shared_context.index("# 提及的日程"),
        )

    def test_public_context_includes_upcoming_schedule_goals_and_weekly_progress(self) -> None:
        zone = ZoneInfo("Asia/Shanghai")
        goal = goal_service.create_goal("每周健身", None, "short")
        goal_schedule_service.update_expectation(
            goal["id"],
            {"period": "week", "target": 3, "label": "每周健身 3 次"},
        )
        for event_id, subject, local_start in (
            ("event-1", "练背", datetime(2026, 7, 16, 19, 0, tzinfo=zone)),
            ("event-2", "练腿", datetime(2026, 7, 18, 19, 0, tzinfo=zone)),
            ("event-outside", "下周末", datetime(2026, 7, 24, 19, 0, tzinfo=zone)),
        ):
            self._insert_schedule_event(event_id, subject, local_start)
            goal_schedule_service.link(goal["id"], event_id)

        service = ScheduleService(auth=ConnectedScheduleAuth())
        with patch("core.schedule_context.ScheduleService", return_value=service):
            built = context_builder.build_context(today=date(2026, 7, 16))

        self.assertIn("# 近期日程", built.shared_context)
        self.assertIn("练背", built.shared_context)
        self.assertIn("练腿", built.shared_context)
        self.assertNotIn("下周末", built.shared_context)
        self.assertIn("目标：每周健身", built.shared_context)
        self.assertIn("每周健身 3 次，本周 2/3", built.shared_context)

    def test_public_context_includes_local_schedule_without_outlook(self) -> None:
        goal = goal_service.create_goal("本地目标", None, "short")
        service = ScheduleService(auth=DisconnectedScheduleAuth(), clock=lambda: 1.0)
        service.create_local_account()
        local = service.create_event(
            subject="本地专注时间",
            event_date=date(2026, 7, 16),
            goal_id=goal["id"],
        )

        with patch("core.schedule_context.ScheduleService", return_value=service):
            built = context_builder.build_context(today=date(2026, 7, 16))

        self.assertEqual("local", local["provider"])
        self.assertIn("# 近期日程", built.shared_context)
        self.assertIn("本地专注时间", built.shared_context)
        self.assertIn("目标：本地目标", built.shared_context)

    def test_public_context_skips_web_search_when_no_enabled_souls(self) -> None:
        for soul in soul_service.list_souls(enabled_only=True):
            soul_service.disable_soul(soul.name)

        with (
            patch("core.llm.turn_prep_router.call_turn_prep") as call_turn_prep,
            patch("core.reply_context.web_search_service.search") as search,
        ):
            built = context_builder.build_context(query="今天 OpenAI 最新模型是什么")

        self.assertEqual([], built.enabled_souls)
        call_turn_prep.assert_not_called()
        search.assert_not_called()

    def _insert_post(self, post_id: str, content: str, *, created_at: float = 1.0) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-25T10:00:00+08:00", content, created_at, created_at),
        )

    def _insert_schedule_event(self, event_id: str, subject: str, start: datetime) -> None:
        end = start + timedelta(hours=1)
        db.execute(
            """
            INSERT OR IGNORE INTO calendar_accounts(id, provider, display_name, created_at)
            VALUES ('outlook', 'outlook', 'Outlook', 1)
            """
        )
        db.execute(
            """
            INSERT INTO schedule_events(
                id, subject, start_ts, end_ts, start_local, end_local, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                subject,
                start.timestamp(),
                end.timestamp(),
                start.replace(tzinfo=None).isoformat(timespec="seconds"),
                end.replace(tzinfo=None).isoformat(timespec="seconds"),
                start.timestamp(),
            ),
        )

    def _last_event(self, event_name: str) -> dict:
        current = self.workspace / "logs" / "current.jsonl"
        records = [
            json.loads(line)
            for line in current.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matches = [record for record in records if record.get("event") == event_name]
        self.assertTrue(matches)
        return matches[-1]


if __name__ == "__main__":
    unittest.main()
