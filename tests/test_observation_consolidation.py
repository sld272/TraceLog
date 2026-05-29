from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import db, observation_consolidation, observation_service
from tests.helpers import require_not_none


class FakeClient:
    def __init__(self, content: str | None = None) -> None:
        self.content = content or json.dumps({"merge_groups": [], "supersede": []}, ensure_ascii=False)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


class ObservationConsolidationTest(unittest.TestCase):
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
        self._insert_post("p-1")
        self._insert_post("p-2")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_exact_duplicate_observations_merge_inside_same_bucket(self) -> None:
        kept = self._create_global("短回复偏好", "用户偏好短回复。", confidence=0.9)
        merged = self._create_global("短回复偏好", "用户偏好短回复。", confidence=0.6)

        result = observation_consolidation.run_observation_consolidation(FakeClient(), "fake-model")

        self.assertEqual(1, result.bucket_count)
        self.assertEqual(1, result.merged_count)
        self.assertEqual("active", require_not_none(observation_service.get_observation(kept))["status"])
        merged_row = require_not_none(observation_service.get_observation(merged))
        self.assertEqual("merged", merged_row["status"])
        self.assertEqual(kept, merged_row["merged_into"])

    def test_same_text_in_different_buckets_does_not_merge(self) -> None:
        global_id = self._create_global("同文记忆", "同一段 narrative。")
        soul_id = self._create_soul_scoped("默认", "同文记忆", "同一段 narrative。")
        other_soul_id = self._create_soul_scoped("毒舌好友", "同文记忆", "同一段 narrative。")

        observation_consolidation.run_observation_consolidation(FakeClient(), "fake-model")

        self.assertEqual("active", require_not_none(observation_service.get_observation(global_id))["status"])
        self.assertEqual("active", require_not_none(observation_service.get_observation(soul_id))["status"])
        self.assertEqual("active", require_not_none(observation_service.get_observation(other_soul_id))["status"])

    def test_comment_and_chat_observations_consolidate_inside_same_soul(self) -> None:
        kept = self._create_soul_scoped("默认", "练歌状态", "用户在默认面前聊练歌卡住。", confidence=0.9, observation_type="state")
        merged = self._create_comment_soul_scoped("默认", "p-1", "练歌状态", "用户在默认面前聊练歌卡住。", confidence=0.6)
        other = self._create_comment_soul_scoped("毒舌好友", "p-1", "练歌状态", "用户在默认面前聊练歌卡住。", confidence=0.5)

        observation_consolidation.run_observation_consolidation(FakeClient(), "fake-model")

        self.assertEqual("active", require_not_none(observation_service.get_observation(kept))["status"])
        self.assertEqual("merged", require_not_none(observation_service.get_observation(merged))["status"])
        self.assertEqual("active", require_not_none(observation_service.get_observation(other))["status"])

    def test_soul_scoped_only_consolidates_inside_same_soul(self) -> None:
        kept = self._create_soul_scoped("默认", "少铺垫", "用户要求该 SOUL 少铺垫。", confidence=0.9)
        merged = self._create_soul_scoped("默认", "少铺垫", "用户要求该 SOUL 少铺垫。", confidence=0.6)
        other = self._create_soul_scoped("毒舌好友", "少铺垫", "用户要求该 SOUL 少铺垫。", confidence=0.5)

        observation_consolidation.run_observation_consolidation(FakeClient(), "fake-model")

        self.assertEqual("active", require_not_none(observation_service.get_observation(kept))["status"])
        self.assertEqual("merged", require_not_none(observation_service.get_observation(merged))["status"])
        self.assertEqual("active", require_not_none(observation_service.get_observation(other))["status"])

    def test_llm_invalid_cross_bucket_missing_and_inactive_ids_are_skipped(self) -> None:
        active_a = self._create_global("全局 A", "全局 A narrative。")
        active_b = self._create_global("全局 B", "全局 B narrative。")
        other_bucket = self._create_soul_scoped("默认", "私聊 A", "私聊 A narrative。")
        inactive = self._create_global("旧全局", "旧全局 narrative。")
        observation_service.archive_observation(inactive)
        payload = {
            "merge_groups": [{"target_id": active_a, "merged_ids": [other_bucket, inactive, 999999]}],
            "supersede": [{"old_id": other_bucket, "new_id": active_b}],
        }

        result = observation_consolidation.run_observation_consolidation(
            FakeClient(json.dumps(payload, ensure_ascii=False)),
            "fake-model",
        )

        self.assertEqual(4, result.invalid_count)
        self.assertEqual(0, result.merged_count)
        self.assertEqual(0, result.superseded_count)
        self.assertEqual("active", require_not_none(observation_service.get_observation(active_a))["status"])
        self.assertEqual("active", require_not_none(observation_service.get_observation(active_b))["status"])
        self.assertEqual("active", require_not_none(observation_service.get_observation(other_bucket))["status"])

    def test_private_blocked_and_stale_observations_do_not_participate(self) -> None:
        active = self._create_global("活跃", "活跃 narrative。")
        merged = self._create_global("已合并", "已合并 narrative。")
        superseded = self._create_global("已覆盖", "已覆盖 narrative。")
        archived = self._create_global("已归档", "已归档 narrative。")
        private_id = observation_service.create_observation(
            {
                "type": "insight",
                "title": "私密阻断",
                "narrative": "不参与 consolidation。",
                "source_channel": "chat",
                "visibility_scope": "private_blocked",
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "none"}],
        )
        observation_service.mark_merged(merged, active)
        observation_service.mark_superseded(superseded, active)
        observation_service.archive_observation(archived)

        scopes = observation_consolidation.preview_consolidation_scopes()
        result = observation_consolidation.run_observation_consolidation(FakeClient(), "fake-model")

        self.assertEqual(["global"], [scope.bucket_key for scope in scopes])
        self.assertEqual(1, result.bucket_count)
        self.assertEqual("active", require_not_none(observation_service.get_observation(private_id))["status"])

    def test_preview_pending_counts_are_isolated_by_bucket_scope_type(self) -> None:
        self._insert_soul("shared")
        self._create_global("全局 pending", "全局 pending narrative。")
        self._create_soul_scoped("shared", "soul pending", "soul pending narrative。")
        self._create_comment_soul_scoped("shared", "p-1", "comment pending", "comment pending narrative。")

        scopes = {
            scope.bucket_key: scope
            for scope in observation_consolidation.preview_consolidation_scopes()
        }

        self.assertEqual(1, scopes["global"].pending_count)
        self.assertEqual(2, scopes["soul_scoped:shared"].pending_count)
        self.assertEqual("shared", scopes["soul_scoped:shared"].scope_value)

    def test_successful_llm_result_applies_supersede_and_advances_cursor(self) -> None:
        old_id = self._create_global("旧约定", "用户以前偏好详细解释。")
        new_id = self._create_global("新约定", "用户现在偏好简短回答。")
        payload = {"merge_groups": [], "supersede": [{"old_id": old_id, "new_id": new_id}]}

        result = observation_consolidation.run_observation_consolidation(
            FakeClient(json.dumps(payload, ensure_ascii=False)),
            "fake-model",
        )

        old_row = require_not_none(observation_service.get_observation(old_id))
        cursor = require_not_none(db.query_one(
            "SELECT value FROM meta WHERE key = ?",
            ("observation_consolidation_cursor:global",),
        ))
        self.assertEqual(1, result.superseded_count)
        self.assertEqual("superseded", old_row["status"])
        self.assertEqual(new_id, old_row["superseded_by"])
        self.assertEqual(str(new_id), cursor["value"])

    def test_invalid_llm_result_does_not_advance_cursor(self) -> None:
        self._create_global("全局 A", "全局 A narrative。")
        self._create_global("全局 B", "全局 B narrative。")

        result = observation_consolidation.run_observation_consolidation(FakeClient("not json"), "fake-model")

        self.assertEqual(1, result.invalid_count)
        self.assertIsNone(db.query_one(
            "SELECT value FROM meta WHERE key = ?",
            ("observation_consolidation_cursor:global",),
        ))

    def _create_global(self, title: str, narrative: str, confidence: float = 0.7) -> int:
        return observation_service.create_observation(
            {
                "type": "preference",
                "title": title,
                "narrative": narrative,
                "source_channel": "post",
                "visibility_scope": "global",
                "importance": 0.7,
                "confidence": confidence,
                "observed_at": float(confidence * 10),
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
        )

    def _create_comment_soul_scoped(
        self,
        soul_name: str,
        post_id: str,
        title: str,
        narrative: str,
        confidence: float = 0.7,
    ) -> int:
        return observation_service.create_observation(
            {
                "type": "state",
                "title": title,
                "narrative": narrative,
                "source_channel": "comment_thread",
                "visibility_scope": "soul_scoped",
                "scope_post_id": post_id,
                "scope_soul_name": soul_name,
                "importance": 0.7,
                "confidence": confidence,
                "observed_at": float(confidence * 10),
            },
            [{"source_type": "comment_message", "source_id": "1", "evidence_access": "source_soul_only"}],
        )

    def _create_soul_scoped(
        self,
        soul_name: str,
        title: str,
        narrative: str,
        confidence: float = 0.7,
        observation_type: str = "correction",
    ) -> int:
        return observation_service.create_observation(
            {
                "type": observation_type,
                "title": title,
                "narrative": narrative,
                "source_channel": "chat",
                "visibility_scope": "soul_scoped",
                "scope_soul_name": soul_name,
                "importance": 0.7,
                "confidence": confidence,
                "observed_at": float(confidence * 10),
            },
            [{"source_type": "chat_message", "source_id": "1", "evidence_access": "source_soul_only"}],
        )

    def _insert_soul(self, name: str) -> None:
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            (name, f"souls/{name}.md", 1.0, 1.0),
        )

    def _insert_post(self, post_id: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-27T00:00:00+08:00", "测试 post", 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
