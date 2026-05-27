from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import context_builder, db, observation_service, profile_service, soul_memory_service, soul_service


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
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与现状\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()
        self._insert_post("p-1", "历史 post")

    def tearDown(self) -> None:
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

        built = context_builder.build_context(relevant_post_ids=[], query="公开context词")

        self.assertIn("# 相关记忆", built.shared_context)
        self.assertIn("L1", built.shared_context)
        self.assertIn("公开全局记忆", built.shared_context)
        self.assertNotIn("私聊不该出现", built.shared_context)

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

    def _insert_post(self, post_id: str, content: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-25T10:00:00+08:00", content, 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
