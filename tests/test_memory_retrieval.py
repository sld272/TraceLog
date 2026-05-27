from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, memory_retrieval, observation_service
from tests.helpers import require_not_none


class MemoryRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self._insert_soul("默认")
        self._insert_soul("毒舌好友")
        self._insert_post("p-1", "用户公开说喜欢短回复。")
        self._insert_post("p-2", "另一个 post。")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_public_post_memory_only_returns_global(self) -> None:
        self._create_global("全局短回复", "用户偏好短回复。检索边界词")
        self._create_soul_scoped("默认", "私聊短回复", "默认知道的私聊短回复。检索边界词")
        self._create_post_visible("p-1", "评论短回复", "评论线程里的短回复。检索边界词")

        context = memory_retrieval.search_public_post_memory("检索边界词", [], limit=5)

        self.assertIn("全局短回复", context)
        self.assertNotIn("私聊短回复", context)
        self.assertNotIn("评论短回复", context)

    def test_chat_memory_returns_global_and_current_soul_scoped_only(self) -> None:
        self._create_global("全局偏好", "用户有全局偏好。私聊检索词")
        self._create_soul_scoped("默认", "默认私聊偏好", "默认可见的私聊记忆。私聊检索词")
        self._create_soul_scoped("毒舌好友", "毒舌私聊偏好", "毒舌可见的私聊记忆。私聊检索词")

        context = memory_retrieval.search_chat_memory("私聊检索词", "默认", [], limit=5)

        self.assertIn("全局偏好", context)
        self.assertIn("默认私聊偏好", context)
        self.assertNotIn("毒舌私聊偏好", context)

    def test_comment_memory_returns_global_and_same_post_visible_only(self) -> None:
        self._create_global("全局评论记忆", "全局评论可见。评论检索词")
        self._create_post_visible("p-1", "当前 post 评论记忆", "当前 post 可见。评论检索词")
        self._create_post_visible("p-2", "其他 post 评论记忆", "其他 post 可见。评论检索词")
        self._create_soul_scoped("默认", "私聊不该进评论", "私聊不该进评论。评论检索词")

        context = memory_retrieval.search_comment_memory("评论检索词", "p-1", [], limit=5)

        self.assertIn("全局评论记忆", context)
        self.assertIn("当前 post 评论记忆", context)
        self.assertNotIn("其他 post 评论记忆", context)
        self.assertNotIn("私聊不该进评论", context)

    def test_stale_observations_are_not_returned(self) -> None:
        active_id = self._create_global("活跃记忆", "活跃记忆 stale检索词")
        merged_id = self._create_global("合并记忆", "合并记忆 stale检索词")
        superseded_id = self._create_global("覆盖记忆", "覆盖记忆 stale检索词")
        archived_id = self._create_global("归档记忆", "归档记忆 stale检索词")

        observation_service.mark_merged(merged_id, active_id)
        observation_service.mark_superseded(superseded_id, active_id)
        observation_service.archive_observation(archived_id)

        context = memory_retrieval.search_public_post_memory("stale检索词", [], limit=10)

        self.assertIn("活跃记忆", context)
        self.assertNotIn("合并记忆", context)
        self.assertNotIn("覆盖记忆", context)
        self.assertNotIn("归档记忆", context)

    def test_indirect_semantic_returns_global_observation_from_related_post(self) -> None:
        self._create_global("间接语义记忆", "这条 narrative 不包含查询词。", source_post_id="p-1")

        context = memory_retrieval.search_public_post_memory("完全不同的查询", ["p-1"], limit=5)

        self.assertIn("间接语义记忆", context)

    def test_fts_and_indirect_hits_are_deduped(self) -> None:
        self._create_global("重复命中记忆", "这条同时被 FTS 和 post source 命中。dedupe检索词", source_post_id="p-1")

        context = memory_retrieval.search_public_post_memory("dedupe检索词", ["p-1"], limit=5)

        self.assertEqual(1, context.count("重复命中记忆"))

    def test_formatted_context_does_not_include_source_excerpt(self) -> None:
        observation_id = observation_service.create_observation(
            {
                "type": "insight",
                "title": "安全格式",
                "narrative": "只展示 narrative。安全检索词",
                "source_channel": "chat",
                "visibility_scope": "soul_scoped",
                "scope_soul_name": "默认",
            },
            [
                {
                    "source_type": "chat_message",
                    "source_id": "123",
                    "excerpt": "这是一段不该进入 prompt 的私聊原文",
                    "evidence_access": "source_soul_only",
                }
            ],
        )
        self.assertIsNotNone(require_not_none(observation_service.get_observation(observation_id)))

        context = memory_retrieval.search_chat_memory("安全检索词", "默认", [], limit=5)

        self.assertIn("安全格式", context)
        self.assertIn("只展示 narrative", context)
        self.assertIn("这是一段不该进入 prompt 的私聊原文", context)

    def test_public_post_expands_all_evidence_only(self) -> None:
        self._create_global(
            "公开可展开",
            "公开 evidence 可展开。公开证据词",
            excerpt="公开 post 摘录可以进入公开上下文",
        )
        self._create_post_visible(
            "p-1",
            "评论不可公开展开",
            "评论 evidence 不该进入公开 post。公开证据词",
            excerpt="评论线程摘录不该进入公开 post",
        )
        self._create_soul_scoped(
            "默认",
            "私聊不可公开展开",
            "私聊 evidence 不该进入公开 post。公开证据词",
            excerpt="私聊摘录不该进入公开 post",
        )

        context = memory_retrieval.search_public_post_memory("公开证据词", [], limit=5)

        self.assertIn("L2", context)
        self.assertIn("公开 post 摘录可以进入公开上下文", context)
        self.assertNotIn("评论线程摘录不该进入公开 post", context)
        self.assertNotIn("私聊摘录不该进入公开 post", context)

    def test_chat_expands_current_soul_private_evidence_only(self) -> None:
        self._create_soul_scoped(
            "默认",
            "默认可展开",
            "默认私聊 evidence 可展开。私聊证据词",
            excerpt="默认私聊摘录可以展开",
        )
        self._create_soul_scoped(
            "毒舌好友",
            "毒舌不可展开",
            "毒舌私聊 evidence 不可给默认。私聊证据词",
            excerpt="毒舌私聊摘录不该出现",
        )

        context = memory_retrieval.search_chat_memory("私聊证据词", "默认", [], limit=5)

        self.assertIn("默认私聊摘录可以展开", context)
        self.assertNotIn("毒舌私聊摘录不该出现", context)

    def test_comment_expands_same_post_visible_evidence_only(self) -> None:
        self._create_post_visible(
            "p-1",
            "当前 post 可展开",
            "当前 post 评论 evidence 可展开。评论证据词",
            excerpt="当前 post 评论摘录可以展开",
        )
        self._create_post_visible(
            "p-2",
            "其他 post 不可展开",
            "其他 post 评论 evidence 不可展开。评论证据词",
            excerpt="其他 post 评论摘录不该出现",
        )

        context = memory_retrieval.search_comment_memory("评论证据词", "p-1", [], limit=5)

        self.assertIn("当前 post 评论摘录可以展开", context)
        self.assertNotIn("其他 post 评论摘录不该出现", context)

    def test_evidence_access_none_never_expands(self) -> None:
        observation_service.create_observation(
            {
                "type": "insight",
                "title": "无展开证据",
                "narrative": "这条不展开 none evidence。none证据词",
                "source_channel": "post",
                "visibility_scope": "global",
            },
            [
                {
                    "source_type": "post",
                    "source_id": "p-1",
                    "excerpt": "none evidence 不该出现",
                    "evidence_access": "none",
                }
            ],
        )

        context = memory_retrieval.search_public_post_memory("none证据词", [], limit=5)

        self.assertIn("无展开证据", context)
        self.assertIn("L1", context)
        self.assertNotIn("none evidence 不该出现", context)

    def test_l2_excerpt_is_truncated(self) -> None:
        long_excerpt = "长摘录" * 80
        self._create_global(
            "长摘录记忆",
            "这条有很长 evidence。长摘录词",
            excerpt=long_excerpt,
        )

        context = memory_retrieval.search_public_post_memory("长摘录词", [], limit=5)

        self.assertIn("长摘录记忆", context)
        self.assertIn("evidence(post:p-1):", context)
        self.assertNotIn(long_excerpt, context)

    def test_only_top_two_eligible_hits_expand_l2(self) -> None:
        for index in range(4):
            self._create_global(
                f"可展开记忆 {index}",
                f"top two disclosure marker {index}",
                excerpt=f"可展开摘录 {index}",
                observed_at=20.0 - index,
            )

        context = memory_retrieval.search_public_post_memory("top two disclosure marker", [], limit=4)

        self.assertEqual(2, context.count(" L2 "))
        self.assertEqual(2, context.count(" L1 "))
        self.assertIn("可展开摘录 0", context)
        self.assertIn("可展开摘录 1", context)
        self.assertNotIn("可展开摘录 2", context)
        self.assertNotIn("可展开摘录 3", context)

    def _create_global(
        self,
        title: str,
        narrative: str,
        source_post_id: str = "p-1",
        excerpt: str | None = None,
        observed_at: float = 10.0,
    ) -> int:
        return observation_service.create_observation(
            {
                "type": "preference",
                "title": title,
                "narrative": narrative,
                "source_channel": "post",
                "visibility_scope": "global",
                "importance": 0.7,
                "confidence": 0.8,
                "observed_at": observed_at,
            },
            [
                {
                    "source_type": "post",
                    "source_id": source_post_id,
                    "excerpt": excerpt,
                    "evidence_access": "all",
                }
            ],
        )

    def _create_soul_scoped(self, soul_name: str, title: str, narrative: str, excerpt: str | None = None) -> int:
        return observation_service.create_observation(
            {
                "type": "correction",
                "title": title,
                "narrative": narrative,
                "source_channel": "chat",
                "visibility_scope": "soul_scoped",
                "scope_soul_name": soul_name,
                "importance": 0.7,
                "confidence": 0.8,
                "observed_at": 10.0,
            },
            [
                {
                    "source_type": "chat_message",
                    "source_id": "1",
                    "excerpt": excerpt,
                    "evidence_access": "source_soul_only",
                }
            ],
        )

    def _create_post_visible(self, post_id: str, title: str, narrative: str, excerpt: str | None = None) -> int:
        return observation_service.create_observation(
            {
                "type": "state",
                "title": title,
                "narrative": narrative,
                "source_channel": "comment_thread",
                "visibility_scope": "post_visible",
                "scope_post_id": post_id,
                "importance": 0.7,
                "confidence": 0.8,
                "observed_at": 10.0,
            },
            [
                {
                    "source_type": "comment_message",
                    "source_id": "1",
                    "excerpt": excerpt,
                    "evidence_access": "post_visible",
                }
            ],
        )

    def _insert_soul(self, name: str) -> None:
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            (name, f"souls/{name}.md", 1.0, 1.0),
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
