from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from core import context_builder, db, logging_service, profile_service, soul_memory_service, soul_service, tool_config_service, web_search_gate, web_search_service


class ContextBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_user_md_path = profile_service.USER_MD_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR
        self.old_web_search_config = web_search_service.CONFIG_FILE

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        web_search_service.CONFIG_FILE = str(Path(self.tmp.name) / "config.json")

        db.init_db()
        logging_service.init_logging({"enabled": True})
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与角色\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        web_search_service.CONFIG_FILE = self.old_web_search_config
        self.tmp.cleanup()

    def test_public_context_uses_profile_raw_related_posts_and_todos(self) -> None:
        self._insert_post("p-related", "raw related content one", created_at=5.0)
        self._insert_post("p-related-2", "raw related content two", created_at=6.0)
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "整理比赛材料", "未完成", 1.0, 1.0),
        )

        built = context_builder.build_context(
            relevant_post_ids=["p-related", "missing", "p-related-2", "p-related"],
            query="这个参数保留兼容，但不再触发 memory retrieval",
            fts_keywords=["不再使用"],
        )

        self.assertIn("# 用户档案", built.shared_context)
        self.assertIn("测试用户", built.shared_context)
        self.assertIn("# 当前用户的历史相关帖子", built.shared_context)
        self.assertEqual(1, built.shared_context.count("raw related content one"))
        self.assertIn("raw related content two", built.shared_context)
        self.assertIn('author: "current_user"', built.shared_context)
        self.assertIn('source: "current_user_public_post"', built.shared_context)
        self.assertIn("# 待办事项", built.shared_context)
        self.assertIn("整理比赛材料", built.shared_context)
        self.assertNotIn("# 相关记忆", built.shared_context)
        self.assertNotIn("# 近期帖子", built.shared_context)
        self.assertEqual(["p-related", "p-related-2"], built.relevant_post_ids)

        event = self._last_event("context_assembly_result")
        self.assertEqual("public_post", event["context_type"])
        self.assertEqual(["p-related", "p-related-2"], event["relevant_post_ids"])
        self.assertTrue(event["raw_related_posts_present"])
        self.assertEqual(["# 用户档案", "# 当前用户的历史相关帖子", "# 待办事项"], [item["title"] for item in event["sections"]])

    def test_public_context_omits_related_posts_when_none_are_found(self) -> None:
        built = context_builder.build_context(relevant_post_ids=["missing"])

        self.assertIn("# 用户档案", built.shared_context)
        self.assertNotIn("# 当前用户的历史相关帖子", built.shared_context)
        self.assertNotIn("# 近期帖子", built.shared_context)
        self.assertEqual([], built.relevant_post_ids)

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
