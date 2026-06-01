from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import (
    comment_service,
    db,
    logging_service,
    profile_service,
    query_rewriter,
    retrieval,
    soul_memory_service,
    soul_service,
    tool_config_service,
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
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_user_md_path = profile_service.USER_MD_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR
        self.old_hybrid_docs = retrieval.hybrid_search_documents

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        retrieval.hybrid_search_documents = lambda *args, **kwargs: []

        db.init_db()
        logging_service.init_logging({"enabled": True, "llm_payload": "off"})
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与角色\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()
        self._insert_post_and_comment("20260525-001", "默认", "我陪你继续拆。")
        self._insert_post_and_comment("20260525-001", "毒舌好友", "别装了，继续讲重点。")

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        retrieval.hybrid_search_documents = self.old_hybrid_docs
        self.tmp.cleanup()

    def test_conversation_is_keyed_by_post_and_soul(self) -> None:
        first = comment_service.get_conversation("20260525-001", "默认")
        second = comment_service.get_conversation("20260525-001", "默认")
        other = comment_service.get_conversation("20260525-001", "毒舌好友")

        self.assertEqual(first.post_id, second.post_id)
        self.assertEqual(first.soul_name, second.soul_name)
        self.assertNotEqual(first.root_comment_id, other.root_comment_id)
        self.assertEqual("默认", first.soul_name)

    def test_comment_reply_only_writes_selected_soul_conversation(self) -> None:
        client = FakeClient({"reply": "好，我只在这里接住这句。", "todos_to_upsert": [], "todos_to_delete": []})

        result = comment_service.call_comment_reply("20260525-001", "默认", "只回复默认", client, "fake-model")
        default_messages = comment_service.list_conversation_messages("20260525-001", "默认", include_root=False)
        other_messages = comment_service.list_conversation_messages("20260525-001", "毒舌好友", include_root=False)

        self.assertTrue(result.ok)
        self.assertEqual("默认", result.soul_name)
        self.assertEqual(["user", "assistant"], [message.role for message in default_messages])
        self.assertEqual([1, 2], [message.seq for message in default_messages])
        self.assertEqual([], other_messages)

    def test_build_comment_context_separates_evidence_and_messages(self) -> None:
        comment_service.append_comment("20260525-001", "默认", "user", "继续聊练歌")
        db.execute(
            """
            INSERT INTO todos(id, task, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("todo-1", "整理歌单", "未完成", 1.0, 1.0),
        )

        context = comment_service.build_comment_context("20260525-001", "默认", "继续聊练歌")

        self.assertIn("测试用户", context.context)
        self.assertIn("今天想认真练歌", context.context)
        self.assertIn("我陪你继续拆", context.context)
        self.assertIn("整理歌单", context.context)
        self.assertNotIn("继续聊练歌", context.context)
        self.assertEqual(["继续聊练歌"], [message.content for message in context.messages])

    def test_build_comment_context_uses_post_and_recent_user_messages_as_retrieval_query(self) -> None:
        comment_service.append_comment("20260525-001", "默认", "user", "第一条")
        comment_service.append_comment("20260525-001", "默认", "assistant", "这句不该进检索")
        comment_service.append_comment("20260525-001", "默认", "user", "第二条")
        comment_service.append_comment("20260525-001", "默认", "user", "第三条")
        comment_service.append_comment("20260525-001", "默认", "user", "第四条")
        captured: dict[str, str] = {}

        def fake_search(query: str, **kwargs: object) -> list:
            del kwargs
            captured["query"] = query
            return []

        retrieval.hybrid_search_documents = fake_search

        context = comment_service.build_comment_context("20260525-001", "默认", "第四条")

        expected = "今天想认真练歌。\n第二条\n第三条\n第四条"
        self.assertEqual(expected, context.retrieval_query)
        self.assertEqual(expected, captured["query"])
        self.assertNotIn("这句不该进检索", context.retrieval_query)

    def test_build_comment_context_loads_current_soul_memory_only(self) -> None:
        soul_memory_service.write_soul_memory("默认", "# 默认的相处记忆\n\n## 对用户的理解\n默认评论记忆\n", source="user")
        soul_memory_service.write_soul_memory("毒舌好友", "# 毒舌好友的相处记忆\n\n## 对用户的理解\n其他 SOUL 评论记忆\n", source="user")

        context = comment_service.build_comment_context("20260525-001", "默认", "评论context词")

        self.assertIn("默认评论记忆", context.soul.soul_memory)
        self.assertNotIn("其他 SOUL 评论记忆", context.soul.soul_memory)

    def test_build_comment_context_uses_query_rewrite_for_related_memories(self) -> None:
        comment_service.append_comment("20260525-001", "默认", "user", "继续聊图书馆学习效率")
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
            comment_service.build_comment_context("20260525-001", "默认", "图书馆学习", FakeClient(), "fake-model")

        rewrite.assert_called_once()
        self.assertEqual("用户是否表达过图书馆学习效率更高", captured["semantic_query"])
        self.assertEqual(["图书馆", "学习效率"], captured["fts_keywords"])
        event = self._last_log_event("query_rewrite_result")
        self.assertEqual("comment", event["channel"])
        self.assertTrue(event["used_rewrite"])

    def test_comment_reply_sends_multi_turn_messages(self) -> None:
        client = FakeClient({"reply": "我在。"})

        result = comment_service.call_comment_reply("20260525-001", "默认", "继续聊练歌", client, "fake-model")
        messages = client.calls[-1]["messages"]

        self.assertTrue(result.ok)
        self.assertEqual("system", messages[0]["role"])
        self.assertIn("证据边界", messages[0]["content"])
        self.assertIn("比喻、场景感、小剧场和幽默想象", messages[0]["content"])
        self.assertIn("不能伪造事实", messages[0]["content"])
        self.assertIn("听起来像", messages[0]["content"])
        self.assertIn("没有证据时", messages[0]["content"])
        self.assertEqual("user", messages[1]["role"])
        self.assertIn("可参考的历史证据", messages[1]["content"])
        self.assertEqual([{"role": "user", "content": "继续聊练歌"}], messages[2:])

    def test_comment_reply_multi_turn_preserves_conversation_order(self) -> None:
        comment_service.call_comment_reply("20260525-001", "默认", "你好", FakeClient({"reply": "你好呀"}), "fake-model")
        client = FakeClient({"reply": "继续。"})

        comment_service.call_comment_reply("20260525-001", "默认", "再说说", client, "fake-model")
        thread_messages = client.calls[-1]["messages"][2:]

        self.assertEqual(["user", "assistant", "user"], [message["role"] for message in thread_messages])
        self.assertEqual(
            ["你好", '{"reply": "你好呀"}', "再说说"],
            [message["content"] for message in thread_messages],
        )

    def test_comment_reply_failure_preserves_user_message_only(self) -> None:
        with patch("core.comment_service.reply_router.call_soul_comment_reply", return_value=None):
            result = comment_service.call_comment_reply("20260525-001", "默认", "这句先记下", FakeClient(), "fake-model")

        messages = comment_service.list_conversation_messages("20260525-001", "默认", include_root=False)
        self.assertFalse(result.ok)
        self.assertIsNone(result.assistant_message_id)
        self.assertEqual(["user"], [message.role for message in messages])

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

        result = comment_service.call_comment_reply("20260525-001", "默认", "提醒我今晚整理歌单", client, "fake-model")
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

        context = comment_service.build_comment_context("20260525-001", "默认", "继续聊")

        self.assertNotIn("整理歌单", context.context)
        self.assertNotIn("# 待办事项", context.context)

    def test_comment_reply_does_not_write_light_reflection_tables_or_revisions(self) -> None:
        comment_service.call_comment_reply("20260525-001", "默认", "这是一条评论回复", FakeClient(), "fake-model")

        self.assertEqual(0, require_not_none(db.query_one("SELECT COUNT(*) AS count FROM entities"))["count"])
        self.assertEqual(0, require_not_none(db.query_one("SELECT COUNT(*) AS count FROM emotions"))["count"])
        self.assertEqual(0, require_not_none(db.query_one("SELECT COUNT(*) AS count FROM events"))["count"])
        self.assertEqual(0, require_not_none(db.query_one("SELECT COUNT(*) AS count FROM relations"))["count"])
        self.assertEqual(0, require_not_none(db.query_one("SELECT COUNT(*) AS count FROM user_md_revisions"))["count"])
        rows = db.query_all("SELECT source FROM soul_memory_revisions WHERE source != 'system'")
        self.assertEqual([], rows)

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


if __name__ == "__main__":
    unittest.main()
