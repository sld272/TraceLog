from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import context_builder, db, logging_service, observation_service, profile_service, soul_memory_service, soul_service


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

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"

        db.init_db()
        logging_service.init_logging({"enabled": True})
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与现状\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()
        self._insert_post("p-1", "历史 post", created_at=10.0)

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        self.tmp.cleanup()

    def test_public_post_context_includes_global_memory_only(self) -> None:
        self._create_global("公开全局记忆", "用户公开偏好短回复。公开context词")
        self._create_soul_scoped("私聊不该出现", "私聊偏好。公开context词")
        self._create_post_visible("评论不该出现", "评论线程偏好。公开context词")

        built = context_builder.build_context(relevant_post_ids=[], query="公开context词")

        self.assertIn("# 相关记忆", built.shared_context)
        self.assertIn("L1", built.shared_context)
        self.assertIn("公开全局记忆", built.shared_context)
        self.assertNotIn("私聊不该出现", built.shared_context)
        self.assertNotIn("评论不该出现", built.shared_context)

    def test_public_context_is_observation_first_and_suppresses_raw_related_posts(self) -> None:
        self._seed_public_related_posts()
        self._create_global("公开主链路记忆", "这是公开主链路接管词。")

        built = context_builder.build_context(relevant_post_ids=["p-related"], query="公开主链路接管词")

        self.assertIn("# 相关记忆", built.shared_context)
        self.assertIn("# 近期帖子", built.shared_context)
        self.assertNotIn("# 相关帖子", built.shared_context)
        self.assertNotIn("raw related fallback content", built.shared_context)
        self.assertLess(built.shared_context.index("# 相关记忆"), built.shared_context.index("# 近期帖子"))
        self.assertEqual(["p-related"], built.relevant_post_ids)
        event = self._last_event("context_assembly_result")
        self.assertEqual("public_post", event["context_type"])
        self.assertTrue(event["related_memory_present"])
        self.assertTrue(event["memory_ids"])
        self.assertFalse(event["raw_related_post_fallback_used"])
        self.assertEqual(["p-related"], event["relevant_post_ids"])

    def test_public_context_uses_raw_related_posts_as_cold_start_fallback(self) -> None:
        self._seed_public_related_posts()

        built = context_builder.build_context(relevant_post_ids=["p-related"], query="没有 observation 命中")

        self.assertNotIn("# 相关记忆", built.shared_context)
        self.assertIn("# 近期帖子", built.shared_context)
        self.assertIn("# 相关帖子", built.shared_context)
        self.assertIn("raw related fallback content", built.shared_context)
        self.assertLess(built.shared_context.index("# 近期帖子"), built.shared_context.index("# 相关帖子"))
        self.assertEqual(["p-related"], built.relevant_post_ids)
        event = self._last_event("context_assembly_result")
        self.assertFalse(event["related_memory_present"])
        self.assertTrue(event["raw_related_post_fallback_used"])

    def test_public_context_can_use_rewrite_keywords_for_memory(self) -> None:
        self._create_global("图书馆效率", "用户提到晚上在图书馆学习效率更高。")

        built = context_builder.build_context(
            relevant_post_ids=[],
            query="完全不相关的原始查询",
            fts_keywords=["图书馆", "学习效率"],
        )

        self.assertIn("# 相关记忆", built.shared_context)
        self.assertIn("图书馆效率", built.shared_context)

    def _create_global(self, title: str, narrative: str) -> int:
        return observation_service.create_observation(
            {
                "type": "preference",
                "title": title,
                "narrative": narrative,
                "source_channel": "post",
                "visibility_scope": "global",
                "observed_at": 1.0,
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
        )

    def _create_soul_scoped(self, title: str, narrative: str) -> int:
        return observation_service.create_observation(
            {
                "type": "correction",
                "title": title,
                "narrative": narrative,
                "source_channel": "chat",
                "visibility_scope": "soul_scoped",
                "scope_soul_name": "默认",
                "observed_at": 1.0,
            },
            [{"source_type": "chat_message", "source_id": "1", "evidence_access": "source_soul_only"}],
        )

    def _create_post_visible(self, title: str, narrative: str) -> int:
        return observation_service.create_observation(
            {
                "type": "state",
                "title": title,
                "narrative": narrative,
                "source_channel": "comment_thread",
                "visibility_scope": "post_visible",
                "scope_post_id": "p-1",
                "observed_at": 1.0,
            },
            [{"source_type": "comment_message", "source_id": "1", "evidence_access": "post_visible"}],
        )

    def _seed_public_related_posts(self) -> None:
        self._insert_post("p-2", "recent post two", created_at=20.0)
        self._insert_post("p-3", "recent post three", created_at=30.0)
        self._insert_post("p-4", "recent post four", created_at=40.0)
        self._insert_post("p-related", "raw related fallback content", created_at=5.0)

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
