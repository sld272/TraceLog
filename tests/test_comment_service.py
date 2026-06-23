from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import (
    comment_service,
    db,
    logging_service,
    memory_read,
    memory_unit_service,
    memory_view_service,
    query_rewriter,
    retrieval,
    soul_relationship_memory,
    soul_service,
    suggestion_pipeline,
    tool_config_service,
    web_search_gate,
    web_search_service,
)
from tests.helpers import require_not_none


class FakeClient:
    def __init__(self, payload: dict | None = None, content: str | None = None) -> None:
        self.payload = payload or {"reply": "我看到了，继续说。", "todos_to_upsert": [], "todos_to_delete": []}
        self.content = content
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.content if self.content is not None else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class CommentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        # suggestion extraction is on by default; disable it for tests that
        # aren't about suggestions so it doesn't consume the FakeClient queue
        suggestions_off = patch.dict(
            os.environ,
            {
                suggestion_pipeline.GOAL_SUGGESTIONS_ENABLED_ENV: "0",
                suggestion_pipeline.TODO_SUGGESTIONS_ENABLED_ENV: "0",
            },
        )
        suggestions_off.start()
        self.addCleanup(suggestions_off.stop)

        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_hybrid_docs = retrieval.hybrid_search_documents
        self.old_web_search_config = web_search_service.CONFIG_FILE

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        soul_service.SOULS_DIR = self.workspace / "souls"
        retrieval.hybrid_search_documents = lambda *args, **kwargs: []
        web_search_service.CONFIG_FILE = str(Path(self.tmp.name) / "config.json")

        db.init_db()
        logging_service.init_logging({"enabled": True, "llm_payload": "off"})
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
        self._insert_post_and_comment("20260525-001", "拾迹者", "我陪你继续拆。")
        self._insert_post_and_comment("20260525-001", "毒舌好友", "别装了，继续讲重点。")

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        soul_service.SOULS_DIR = self.old_souls_dir
        retrieval.hybrid_search_documents = self.old_hybrid_docs
        web_search_service.CONFIG_FILE = self.old_web_search_config
        self.tmp.cleanup()

    def test_conversation_is_keyed_by_post_and_soul(self) -> None:
        first = comment_service.get_conversation("20260525-001", "拾迹者")
        second = comment_service.get_conversation("20260525-001", "拾迹者")
        other = comment_service.get_conversation("20260525-001", "毒舌好友")

        self.assertEqual(first.post_id, second.post_id)
        self.assertEqual(first.soul_name, second.soul_name)
        self.assertNotEqual(first.root_comment_id, other.root_comment_id)
        self.assertEqual("拾迹者", first.soul_name)

    def test_list_post_conversations_uses_post_soul_order_snapshot(self) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("p-order", "2026-06-01T10:00:00+08:00", "排序测试", 1.0, 1.0),
        )
        for soul_name, sort_order in [("拾迹者", 0), ("毒舌好友", 1)]:
            db.execute(
                """
                INSERT INTO post_soul_orders(post_id, soul_name, sort_order, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("p-order", soul_name, sort_order, 1.0),
            )
        db.execute("UPDATE souls SET sort_order = ? WHERE name = ?", (9, "拾迹者"))
        db.execute("UPDATE souls SET sort_order = ? WHERE name = ?", (0, "毒舌好友"))
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
            VALUES (?, ?, 'assistant', ?, 0, ?)
            """,
            ("p-order", "毒舌好友", "先完成", 2.0),
        )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
            VALUES (?, ?, 'assistant', ?, 0, ?)
            """,
            ("p-order", "拾迹者", "后完成", 3.0),
        )

        conversations = comment_service.list_post_conversations("p-order")

        self.assertEqual(["拾迹者", "毒舌好友"], [conversation.soul_name for conversation in conversations])

    def test_comment_reply_only_writes_selected_soul_conversation(self) -> None:
        client = FakeClient({"reply": "好，我只在这里接住这句。", "todos_to_upsert": [], "todos_to_delete": []})

        result = comment_service.call_comment_reply("20260525-001", "拾迹者", "只回复拾迹者", client, "fake-model")
        default_messages = comment_service.list_conversation_messages("20260525-001", "拾迹者", include_root=False)
        other_messages = comment_service.list_conversation_messages("20260525-001", "毒舌好友", include_root=False)

        self.assertTrue(result.ok)
        self.assertEqual("拾迹者", result.soul_name)
        self.assertEqual(["user", "assistant"], [message.role for message in default_messages])
        self.assertEqual([1, 2], [message.seq for message in default_messages])
        self.assertEqual([], other_messages)

    def test_build_comment_context_separates_evidence_and_messages(self) -> None:
        comment_service.append_comment("20260525-001", "拾迹者", "user", "继续聊练歌")
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "整理歌单", "未完成", 1.0, 1.0),
        )

        context = comment_service.build_comment_context("20260525-001", "拾迹者", "继续聊练歌")

        self.assertIn("测试用户", context.context)
        self.assertIn("今天想认真练歌", context.context)
        self.assertIn("我陪你继续拆", context.context)
        self.assertIn("整理歌单", context.context)
        self.assertNotIn("继续聊练歌", context.context)
        self.assertEqual(["继续聊练歌"], [message.content for message in context.messages])

    def test_build_comment_context_uses_post_and_recent_user_messages_as_retrieval_query(self) -> None:
        comment_service.append_comment("20260525-001", "拾迹者", "user", "第一条")
        comment_service.append_comment("20260525-001", "拾迹者", "assistant", "这句不该进检索")
        comment_service.append_comment("20260525-001", "拾迹者", "user", "第二条")
        comment_service.append_comment("20260525-001", "拾迹者", "user", "第三条")
        comment_service.append_comment("20260525-001", "拾迹者", "user", "第四条")
        captured: dict[str, str] = {}

        def fake_search(query: str, **kwargs: object) -> list:
            del kwargs
            captured["query"] = query
            return []

        retrieval.hybrid_search_documents = fake_search

        context = comment_service.build_comment_context("20260525-001", "拾迹者", "第四条")

        expected = "今天想认真练歌。\n第二条\n第三条\n第四条"
        self.assertEqual(expected, context.retrieval_query)
        self.assertEqual(expected, captured["query"])
        self.assertNotIn("这句不该进检索", context.retrieval_query)

    def test_other_soul_user_comment_excluded_from_memory_section(self) -> None:
        # All public-post comments share the global/public bucket, so without the
        # fix the freshness seam surfaced the user's comment to ANOTHER soul as the
        # current user's recent evidence (cross-talk). It may appear as labeled
        # background, but must NOT be in the # 记忆 (memory/freshness) section.
        comment_service.append_comment(
            "20260525-001", "毒舌好友", "user", "实则躺平唯一标记XYZ"
        )
        context = comment_service.build_comment_context("20260525-001", "拾迹者", "继续聊")
        memory_section = context.context.split("# 记忆", 1)
        memory_text = memory_section[1] if len(memory_section) > 1 else ""
        self.assertNotIn("实则躺平唯一标记XYZ", memory_text)

    def test_build_comment_context_includes_other_soul_threads_with_user_followups(self) -> None:
        soul_service.create_soul("安静好友", "安静人格")
        self._insert_post_and_comment("20260525-001", "安静好友", "只首评，不应进入")
        comment_service.append_comment("20260525-001", "毒舌好友", "user", "其他追问一")
        comment_service.append_comment("20260525-001", "毒舌好友", "assistant", "其他回复二")
        comment_service.append_comment("20260525-001", "毒舌好友", "user", "其他追问三")
        comment_service.append_comment("20260525-001", "毒舌好友", "assistant", "其他回复四")
        comment_service.append_comment("20260525-001", "毒舌好友", "user", "其他追问五")
        comment_service.append_comment("20260525-001", "毒舌好友", "assistant", "其他回复六")
        comment_service.append_comment("20260525-001", "毒舌好友", "user", "其他追问七")

        context = comment_service.build_comment_context("20260525-001", "拾迹者", "继续聊")

        self.assertIn("# 本帖其他评论区（公开氛围，仅供你知道）", context.context)
        self.assertIn("默认不要把这里的话题扯进你的回复", context.context)
        self.assertIn("直接相关", context.context)
        self.assertIn("## 毒舌好友", context.context)
        # the user line in another SOUL's thread is disambiguated as addressed to
        # that SOUL, never a bare follow-up the current SOUL might answer
        self.assertIn("用户对 毒舌好友", context.context)
        self.assertNotIn("只首评，不应进入", context.context)
        self.assertNotIn("别装了，继续讲重点。", context.context)
        background = context.context.split("# 记忆", 1)[0]
        self.assertNotIn("其他追问一", background)
        self.assertIn("其他回复二", context.context)
        self.assertIn("其他追问三", context.context)
        self.assertIn("其他回复四", context.context)
        self.assertIn("其他追问五", context.context)
        self.assertIn("其他回复六", context.context)
        self.assertIn("其他追问七", context.context)

    def test_build_comment_context_excludes_current_post_and_post_comments_from_retrieval(self) -> None:
        comment_service.append_comment("20260525-001", "拾迹者", "user", "继续聊练歌")
        captured: dict[str, object] = {}

        def fake_search(query: str, **kwargs: object) -> list:
            del query
            captured["exclusion"] = kwargs.get("exclusion")
            return []

        retrieval.hybrid_search_documents = fake_search

        comment_service.build_comment_context("20260525-001", "拾迹者", "继续聊练歌")

        exclusion = captured["exclusion"]
        self.assertIsInstance(exclusion, retrieval.RetrievalExclusion)
        self.assertEqual(frozenset({"20260525-001"}), exclusion.post_ids)
        self.assertEqual(frozenset({"20260525-001"}), exclusion.comment_post_ids)

    def test_build_comment_context_loads_current_soul_relationship_view_only(self) -> None:
        for soul_name, content in (
            ("拾迹者", "拾迹者评论记忆"),
            ("毒舌好友", "其他 SOUL 评论记忆"),
        ):
            memory_unit_service.add_unit(
                owner_scope=f"soul:{soul_name}",
                visibility_scope=f"private:soul:{soul_name}",
                source_channel="user",
                type="relationship",
                content=content,
                confidence=1.0,
                tier="core",
                importance=1.0,
                source="user_authored",
                actor="user",
            )
            soul_relationship_memory.refresh_relationship_memory(soul_name)

        context = comment_service.build_comment_context("20260525-001", "拾迹者", "评论context词")

        relationship = memory_read.relationship_memory_for("拾迹者")
        self.assertIn("拾迹者评论记忆", relationship)
        self.assertNotIn("其他 SOUL 评论记忆", relationship)

    def test_build_comment_context_uses_query_rewrite_for_related_memories(self) -> None:
        comment_service.append_comment("20260525-001", "拾迹者", "user", "继续聊图书馆学习效率")
        captured: dict[str, object] = {}

        def fake_search(
            query: str,
            semantic_query: str | None = None,
            fts_keywords: list[str] | None = None,
            **kwargs: object,
        ) -> list:
            del kwargs
            captured["query"] = query
            captured["semantic_query"] = semantic_query
            captured["fts_keywords"] = fts_keywords
            return []

        retrieval.hybrid_search_documents = fake_search
        rewritten = query_rewriter.RewrittenQuery(
            raw_query="raw",
            semantic_query="用户是否表达过图书馆学习效率更高",
            keywords=["图书馆", "学习效率"],
            used_rewrite=True,
        )

        with patch("core.reply_context.query_rewriter.rewrite_query", return_value=rewritten) as rewrite:
            comment_service.build_comment_context("20260525-001", "拾迹者", "图书馆学习", FakeClient(), "fake-model")

        rewrite.assert_called_once()
        self.assertEqual("用户是否表达过图书馆学习效率更高", captured["semantic_query"])
        self.assertEqual(["图书馆", "学习效率"], captured["fts_keywords"])
        event = self._last_log_event("query_rewrite_result")
        self.assertEqual("comment", event["channel"])
        self.assertTrue(event["used_rewrite"])

    def test_build_comment_context_injects_web_search_results_when_gate_requests_search(self) -> None:
        comment_service.append_comment("20260525-001", "拾迹者", "user", "查一下 Python 3.13 稳定版")
        settings = web_search_service.WebSearchConfig(
            enabled=True,
            provider="duckduckgo",
            tavily_api_key=None,
            max_results=5,
            timeout_s=8,
            cache_ttl_s=0,
        )
        decision = web_search_gate.WebSearchDecision(
            should_search=True,
            queries=["Python 3.13 stable release"],
            reason="当前版本事实",
            freshness_required=True,
        )
        run = web_search_service.WebSearchRun(
            used=True,
            provider="duckduckgo",
            queries=decision.queries,
            results=[
                web_search_service.WebSearchResult(
                    title="Python release",
                    url="https://example.com/python",
                    snippet="Python 版本信息",
                    provider="duckduckgo",
                )
            ],
            error=None,
            elapsed_ms=1,
        )

        with (
            patch("core.reply_context.web_search_service.effective_config", return_value=settings),
            patch("core.reply_context.web_search_gate.decide", return_value=decision) as decide,
            patch("core.reply_context.web_search_service.search", return_value=run) as search,
            patch(
                "core.reply_context.query_rewriter.rewrite_query",
                return_value=query_rewriter.RewrittenQuery(
                    raw_query="raw",
                    semantic_query="Python 3.13 stable release",
                    keywords=[],
                    used_rewrite=True,
                ),
            ),
        ):
            context = comment_service.build_comment_context(
                "20260525-001",
                "拾迹者",
                "查一下 Python 3.13 稳定版",
                FakeClient(),
                "fake-model",
            )

        self.assertIn("# 网页搜索结果", context.context)
        self.assertIn("Python release", context.context)
        self.assertIn("Python 版本信息", context.context)
        decide.assert_called_once()
        search.assert_called_once()

    def test_comment_reply_sends_multi_turn_messages(self) -> None:
        client = FakeClient({"reply": "我在。"})

        result = comment_service.call_comment_reply("20260525-001", "拾迹者", "继续聊练歌", client, "fake-model")
        messages = client.calls[-1]["messages"]

        self.assertTrue(result.ok)
        self.assertEqual("system", messages[0]["role"])
        self.assertIn("证据边界", messages[0]["content"])
        self.assertIn("比喻、场景感、小剧场和幽默想象", messages[0]["content"])
        self.assertIn("不能伪造事实", messages[0]["content"])
        self.assertIn("听起来像", messages[0]["content"])
        self.assertIn("没有证据时", messages[0]["content"])
        # context-first, query-last: background folds into the FINAL user turn
        # (no separate competing evidence turn), the real message comes last
        self.assertEqual(2, len(messages))
        self.assertEqual("user", messages[-1]["role"])
        self.assertIn("参考背景", messages[-1]["content"])
        self.assertTrue(messages[-1]["content"].rstrip().endswith("继续聊练歌"))

    def test_comment_reply_multi_turn_preserves_conversation_order(self) -> None:
        comment_service.call_comment_reply("20260525-001", "拾迹者", "你好", FakeClient({"reply": "你好呀"}), "fake-model")
        client = FakeClient({"reply": "继续。"})

        comment_service.call_comment_reply("20260525-001", "拾迹者", "再说说", client, "fake-model")
        # history stays as turns after system; the latest message is the final
        # user turn (background folded in, actual text at the end)
        thread_messages = client.calls[-1]["messages"][1:]

        self.assertEqual(["user", "assistant", "user"], [message["role"] for message in thread_messages])
        self.assertEqual("你好", thread_messages[0]["content"])
        self.assertEqual('{"reply": "你好呀"}', thread_messages[1]["content"])
        self.assertTrue(thread_messages[2]["content"].rstrip().endswith("再说说"))

    def test_comment_reply_failure_preserves_user_message_and_failed_assistant(self) -> None:
        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value=None):
            result = comment_service.call_comment_reply("20260525-001", "拾迹者", "这句先记下", FakeClient(), "fake-model")

        messages = comment_service.list_conversation_messages("20260525-001", "拾迹者", include_root=False)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.assistant_message_id)
        self.assertEqual(["user", "assistant"], [message.role for message in messages])
        self.assertEqual("", messages[-1].content)
        self.assertEqual("failed", json.loads(messages[-1].metadata or "{}")["status"])

    def test_rerun_failed_assistant_comment_updates_same_message(self) -> None:
        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value=None):
            result = comment_service.call_comment_reply("20260525-001", "拾迹者", "这句先记下", FakeClient(), "fake-model")
        self.assertIsNotNone(result.assistant_message_id)

        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value={"reply": "重试后的回复"}):
            rerun = comment_service.rerun_latest_assistant_message(result.assistant_message_id, FakeClient(), "fake-model")

        messages = rerun["messages"]
        self.assertEqual(["assistant", "user", "assistant"], [message.role for message in messages])
        self.assertEqual("重试后的回复", messages[-1].content)
        self.assertEqual("ok", json.loads(messages[-1].metadata or "{}")["status"])

    def test_comment_reply_metadata_includes_evidence_snapshot(self) -> None:
        root_id = require_not_none(comment_service.get_conversation("20260525-001", "毒舌好友").root_comment_id)
        retrieval.hybrid_search_documents = lambda *args, **kwargs: [
            retrieval.RetrievalDocHit(
                doc_id=f"comment-{root_id}",
                type="comment",
                source_id=str(root_id),
                score=0.73,
                rank=1,
                metadata={
                    "type": "comment",
                    "comment_id": root_id,
                    "post_id": "20260525-001",
                    "soul_name": "毒舌好友",
                },
                sources=["vector"],
                reasons=["vector:rank=1"],
                distance=0.42,
            )
        ]

        result = comment_service.call_comment_reply("20260525-001", "拾迹者", "继续聊", FakeClient({"reply": "继续拆。"}), "fake-model")
        message = comment_service.get_message(require_not_none(result.assistant_message_id))
        metadata = json.loads(message.metadata or "{}")

        self.assertEqual("ok", metadata["status"])
        item = metadata["evidence"]["items"][0]
        self.assertEqual(f"comment-{root_id}", item["doc_id"])
        self.assertEqual("comment", item["type"])
        self.assertEqual(0.73, item["score"])
        self.assertEqual(0.42, item["distance"])
        self.assertIn("别装了，继续讲重点", item["snippet"])

    def test_comment_reply_attaches_inline_goal_suggestion_when_enabled(self) -> None:
        candidate = {
            "title": "完成课程项目",
            "detail": "交付演示版本",
            "horizon": "short",
            "confidence": 0.88,
        }
        with patch.dict(os.environ, {suggestion_pipeline.GOAL_SUGGESTIONS_ENABLED_ENV: "1"}), patch(
            "core.suggestion_pipeline.goal_router.call_goal_router",
            return_value=[candidate],
        ):
            result = comment_service.call_comment_reply(
                "20260525-001",
                "拾迹者",
                "我这周要完成课程项目",
                FakeClient({"reply": "先把交付标准定下来。"}),
                "fake-model",
            )

        message = comment_service.get_message(require_not_none(result.assistant_message_id))
        metadata = json.loads(message.metadata or "{}")
        self.assertEqual("完成课程项目", result.suggestions[0]["payload"]["title"])
        self.assertTrue(result.suggestions[0]["payload"]["focus"])
        self.assertEqual(result.suggestions, metadata["suggestions"])

    def test_rerun_failed_assistant_comment_keeps_failure_on_same_message(self) -> None:
        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value=None):
            result = comment_service.call_comment_reply("20260525-001", "拾迹者", "这句先记下", FakeClient(), "fake-model")
        self.assertIsNotNone(result.assistant_message_id)

        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value=None):
            rerun = comment_service.rerun_latest_assistant_message(result.assistant_message_id, FakeClient(), "fake-model")

        message = rerun["message"]
        metadata = json.loads(message.metadata or "{}")
        self.assertEqual(result.assistant_message_id, message.id)
        self.assertEqual("", message.content)
        self.assertEqual("failed", metadata["status"])
        self.assertEqual("comment rerun failed", metadata["error"])
        self.assertIsNotNone(message.rerun_at)

    def test_delete_assistant_comment_is_rejected(self) -> None:
        comment_service.append_comment("20260525-001", "拾迹者", "user", "继续聊")
        comment_service.append_comment("20260525-001", "拾迹者", "assistant", "继续聊的回复")
        root = comment_service.get_conversation("20260525-001", "拾迹者").root_comment_id
        self.assertIsNotNone(root)

        with self.assertRaises(ValueError):
            comment_service.delete_message(root)

        messages = comment_service.list_conversation_messages("20260525-001", "拾迹者")
        self.assertEqual(["assistant", "user", "assistant"], [message.role for message in messages])

    def test_delete_comment_message_deletes_that_message_and_later_messages(self) -> None:
        first_user = comment_service.append_comment("20260525-001", "拾迹者", "user", "第一条")
        comment_service.append_comment("20260525-001", "拾迹者", "assistant", "第一条回复")
        comment_service.append_comment("20260525-001", "拾迹者", "user", "第二条")

        result = comment_service.delete_message(first_user.id)
        messages = comment_service.list_conversation_messages("20260525-001", "拾迹者")

        self.assertEqual([first_user.id, first_user.id + 1, first_user.id + 2], result["deleted_message_ids"])
        self.assertEqual(["assistant"], [message.role for message in messages])
        self.assertEqual([0], [message.seq for message in messages])

    def test_rerun_latest_assistant_comment_updates_same_message_and_marks_rerun(self) -> None:
        result = comment_service.call_comment_reply(
            "20260525-001",
            "拾迹者",
            "继续聊练歌",
            FakeClient({"reply": "原回复"}),
            "fake-model",
        )
        self.assertIsNotNone(result.assistant_message_id)

        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value={"reply": "重跑后的回复"}):
            rerun = comment_service.rerun_latest_assistant_message(result.assistant_message_id, FakeClient(), "fake-model")

        messages = rerun["messages"]
        self.assertEqual([0, 1, 2], [message.seq for message in messages])
        self.assertEqual("重跑后的回复", messages[-1].content)
        self.assertIsNotNone(messages[-1].rerun_at)

    def test_rerun_latest_assistant_comment_uses_image_summary_from_user_message(self) -> None:
        attachment = self._insert_attachment("att-comment-image")
        result = comment_service.call_comment_reply(
            "20260525-001",
            "拾迹者",
            "这张图是什么发布信息",
            FakeClient({"reply": "原回复"}),
            "fake-model",
            attachment_ids=[attachment.id],
        )
        self.assertIsNotNone(result.assistant_message_id)
        captured = {}

        def fake_reply(client, model, context, soul, *, trace_context=None):
            del client, model, soul, trace_context
            captured["messages"] = context.messages
            return {"reply": "图片评论重跑回复"}

        with (
            patch(
                "core.comment_service.vision_service.content_with_cached_summaries",
                return_value="这张图是什么发布信息\n\n[图片理解摘要]\n- 图片 1: 截图显示 Python 3.13 已发布",
            ) as content_with_cached,
            patch("core.comment_service.reply_router.call_soul_comment_reply", side_effect=fake_reply),
        ):
            rerun = comment_service.rerun_latest_assistant_message(result.assistant_message_id, FakeClient(), "fake-model")

        content_with_cached.assert_called()
        self.assertEqual("图片评论重跑回复", rerun["message"].content)
        self.assertIn("Python 3.13 已发布", captured["messages"][-1].content)

    def test_rerun_root_assistant_comment_uses_public_post_reply_path(self) -> None:
        root_id = comment_service.get_conversation("20260525-001", "拾迹者").root_comment_id
        self.assertIsNotNone(root_id)
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("20260520-001", "2026-05-20T10:00:00+08:00", "历史相关帖子。", 0.5, 0.5),
        )
        captured = {}

        def fake_rewrite(client, model, raw_query, channel, trace_context=None):
            del client, model, trace_context
            captured["rewrite_channel"] = channel
            captured["rewrite_query"] = raw_query
            return query_rewriter.RewrittenQuery(
                raw_query=raw_query,
                semantic_query="认真练歌",
                keywords=["练歌"],
                used_rewrite=True,
            )

        def fake_search(*args, **kwargs):
            captured["search_args"] = args
            captured["exclusion"] = kwargs.get("exclusion")
            captured["search_trace"] = kwargs.get("trace_context")
            return ["20260520-001"]

        def fake_post_reply(user_input, client, model, shared_context, soul, *, trace_context=None):
            del client, model
            captured["user_input"] = user_input
            captured["shared_context"] = shared_context
            captured["soul_name"] = soul.name
            captured["post_reply_trace"] = trace_context
            return {"reply": "根评论重跑回复"}

        with (
            patch("core.app_services.public_post_pipeline.query_rewriter.rewrite_query", side_effect=fake_rewrite),
            patch("core.app_services.public_post_pipeline.retrieval.hybrid_search", side_effect=fake_search),
            patch("core.comment_service.reply_router.call_soul_post_reply", side_effect=fake_post_reply),
            patch(
                "core.comment_service.reply_router.call_soul_comment_reply",
                side_effect=AssertionError("root rerun should not use comment reply"),
            ),
        ):
            rerun = comment_service.rerun_latest_assistant_message(root_id, FakeClient(), "fake-model")

        self.assertEqual("根评论重跑回复", rerun["message"].content)
        self.assertEqual("public_post", captured["rewrite_channel"])
        self.assertIn("今天想认真练歌", captured["rewrite_query"])
        self.assertEqual(frozenset({"20260525-001"}), captured["exclusion"].post_ids)
        self.assertEqual("public_post", captured["search_trace"]["channel"])
        self.assertIn("今天想认真练歌", captured["user_input"])
        self.assertIn("历史相关帖子", captured["shared_context"])
        self.assertEqual("拾迹者", captured["soul_name"])
        self.assertEqual(["20260520-001"], captured["post_reply_trace"]["relevant_post_ids"])
        metadata = json.loads(rerun["message"].metadata or "{}")
        self.assertTrue(metadata["rerun"])
        self.assertEqual("post", metadata["evidence"]["items"][0]["type"])
        self.assertEqual("20260520-001", metadata["evidence"]["items"][0]["post_id"])
        self.assertIsNotNone(rerun["message"].rerun_at)

    def test_rerun_comment_requires_latest_assistant_message(self) -> None:
        first = comment_service.call_comment_reply(
            "20260525-001",
            "拾迹者",
            "第一轮",
            FakeClient({"reply": "第一轮回复"}),
            "fake-model",
        )
        comment_service.append_comment("20260525-001", "拾迹者", "user", "第二轮用户消息")
        self.assertIsNotNone(first.assistant_message_id)

        with self.assertRaises(ValueError):
            comment_service.rerun_latest_assistant_message(first.assistant_message_id, FakeClient(), "fake-model")

    def test_comment_reply_ignores_todo_fields(self) -> None:
        client = FakeClient(
            {
                "reply": "我记下来了。",
                "todos_to_upsert": [
                    {
                        "id": None,
                        "task": "今晚整理歌单",
                        "date": "2026-05-25",
                        "start_time": None,
                        "end_time": None,
                        "status": "未完成",
                    }
                ],
                "todos_to_delete": [],
            }
        )

        result = comment_service.call_comment_reply("20260525-001", "拾迹者", "提醒我今晚整理歌单", client, "fake-model")
        row = require_not_none(db.query_one("SELECT COUNT(*) AS count FROM todos"))

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.assistant_message_id)
        self.assertEqual(0, row["count"])

    def test_comment_context_omits_todos_when_tool_disabled(self) -> None:
        tool_config_service.set_tool_enabled("todo", False)
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "整理歌单", "未完成", 1.0, 1.0),
        )

        context = comment_service.build_comment_context("20260525-001", "拾迹者", "继续聊")

        self.assertNotIn("整理歌单", context.context)
        self.assertNotIn("# 待办事项", context.context)

    def test_comment_reply_only_records_evidence_until_reconcile_runs(self) -> None:
        comment_service.call_comment_reply("20260525-001", "拾迹者", "这是一条评论回复", FakeClient(), "fake-model")

        self.assertGreater(
            require_not_none(
                db.query_one("SELECT COUNT(*) AS count FROM memory_ingest_events")
            )["count"],
            0,
        )
        self.assertEqual(
            0,
            require_not_none(
                db.query_one("SELECT COUNT(*) AS count FROM memory_reconcile_runs")
            )["count"],
        )

    def _insert_post_and_comment(self, post_id: str, soul_name: str, comment: str) -> None:
        if db.query_one("SELECT 1 FROM posts WHERE id = ?", (post_id,)) is None:
            db.execute(
                """
                INSERT INTO posts(id, ts, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (post_id, "2026-05-25T10:00:00+08:00", "今天想认真练歌。", 1.0, 1.0),
            )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
            VALUES (?, ?, 'assistant', ?, 0, ?)
            """,
            (post_id, soul_name, comment, 2.0),
        )

    def _last_log_event(self, event_name: str) -> dict:
        log_path = self.workspace / "logs" / "current.jsonl"
        records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matches = [record for record in records if record.get("event") == event_name]
        self.assertTrue(matches)
        return matches[-1]

    def _insert_attachment(self, attachment_id: str):
        relative = f"attachments/images/2026/06/{attachment_id}.jpg"
        target = self.workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake image")
        now = 1.0
        db.execute(
            """
            INSERT INTO attachments(
                id, file_path, mime_type, file_size, width, height, sha256,
                original_filename, linked_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attachment_id,
                relative,
                "image/jpeg",
                10,
                100,
                80,
                "sha",
                "image.jpg",
                now,
                now,
            ),
        )
        row = require_not_none(db.query_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,)))
        return SimpleNamespace(
            id=row["id"],
            file_path=row["file_path"],
            mime_type=row["mime_type"],
            file_size=int(row["file_size"]),
            width=int(row["width"]),
            height=int(row["height"]),
            sha256=row["sha256"],
            original_filename=row["original_filename"],
            linked_at=float(row["linked_at"]) if row["linked_at"] is not None else None,
            created_at=float(row["created_at"]),
            url=f"/attachments/{row['id']}",
        )


if __name__ == "__main__":
    unittest.main()
