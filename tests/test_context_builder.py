from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from core import context_builder, db, goal_service, logging_service, memory_unit_service, memory_view_service, soul_service, tool_config_service, web_search_gate, web_search_service


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

    def test_public_context_uses_goals_and_todos_without_portrait_or_related_posts(self) -> None:
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "整理比赛材料", "未完成", 1.0, 1.0),
        )
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
        self.assertIn("# 待办事项", built.shared_context)
        self.assertIn("整理比赛材料", built.shared_context)
        self.assertIn("# 长期目标", built.shared_context)
        self.assertIn("跨专业考研", built.shared_context)
        self.assertIn("# 当前状态", built.shared_context)
        self.assertIn("完成课程项目", built.shared_context)

        event = self._last_event("context_assembly_result")
        self.assertEqual("public_post", event["context_type"])
        self.assertEqual(
            ["# 长期目标", "# 当前状态", "# 待办事项"],
            [item["title"] for item in event["sections"]],
        )

    def test_public_context_omits_todos_when_tool_disabled(self) -> None:
        tool_config_service.set_tool_enabled("todo", False)
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "不应进入上下文", "未完成", 1.0, 1.0),
        )

        built = context_builder.build_context()

        self.assertNotIn("# 待办事项", built.shared_context)
        self.assertNotIn("不应进入上下文", built.shared_context)

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

        with (
            patch("core.reply_context.web_search_service.effective_config", return_value=settings),
            patch("core.reply_context.web_search_gate.decide", return_value=decision) as decide,
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
        decide.assert_called_once()
        search.assert_called_once()

    def test_public_context_skips_web_search_when_no_enabled_souls(self) -> None:
        for soul in soul_service.list_souls(enabled_only=True):
            soul_service.disable_soul(soul.name)

        with (
            patch("core.reply_context.web_search_gate.decide") as decide,
            patch("core.reply_context.web_search_service.search") as search,
        ):
            built = context_builder.build_context(query="今天 OpenAI 最新模型是什么")

        self.assertEqual([], built.enabled_souls)
        decide.assert_not_called()
        search.assert_not_called()

    def _insert_post(self, post_id: str, content: str, *, created_at: float = 1.0) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-25T10:00:00+08:00", content, created_at, created_at),
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
