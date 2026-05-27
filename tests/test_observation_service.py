from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core import db, observation_service
from tests.helpers import require_not_none


class ObservationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self._insert_soul("默认")
        self._insert_post("p-1")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_create_and_read_global_post_and_soul_scoped_observations(self) -> None:
        chat_message_id = self._insert_chat_message("默认", "私聊证据")
        global_id = observation_service.create_observation(
            {
                "type": "preference",
                "title": "短回复偏好",
                "summary": "用户偏好直接表达",
                "narrative": "用户偏好简短直接的回复。",
                "source_channel": "post",
                "visibility_scope": "global",
                "importance": 0.7,
                "confidence": 0.8,
                "observed_at": 1.0,
                "metadata": {"kind": "test"},
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
        )
        post_id = observation_service.create_observation(
            {
                "type": "insight",
                "title": "评论线程洞察",
                "narrative": "这条讨论属于公开 post 语境。",
                "source_channel": "comment_thread",
                "visibility_scope": "post_visible",
                "scope_post_id": "p-1",
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "post_visible"}],
        )
        soul_id = observation_service.create_observation(
            {
                "type": "correction",
                "title": "私聊纠正",
                "narrative": "用户纠正默认以后少铺垫。",
                "source_channel": "chat",
                "visibility_scope": "soul_scoped",
                "scope_soul_name": "默认",
            },
            [
                {
                    "source_type": "chat_message",
                    "source_id": chat_message_id,
                    "evidence_access": "source_soul_only",
                }
            ],
        )

        global_observation = require_not_none(observation_service.get_observation(global_id))
        post_observation = require_not_none(observation_service.get_observation(post_id))
        soul_observation = require_not_none(observation_service.get_observation(soul_id))

        self.assertEqual("短回复偏好", global_observation["title"])
        self.assertEqual("global", global_observation["visibility_scope"])
        self.assertEqual("post_visible", post_observation["visibility_scope"])
        self.assertEqual("p-1", post_observation["scope_post_id"])
        self.assertEqual("soul_scoped", soul_observation["visibility_scope"])
        self.assertEqual("默认", soul_observation["scope_soul_name"])
        self.assertEqual("chat_message", soul_observation["sources"][0]["source_type"])

    def test_scope_validation_rejects_invalid_boundaries(self) -> None:
        with self.assertRaises(ValueError):
            observation_service.create_observation(
                {
                    "type": "insight",
                    "title": "缺少 post scope",
                    "narrative": "缺少 scope_post_id。",
                    "source_channel": "comment_thread",
                    "visibility_scope": "post_visible",
                },
                [{"source_type": "post", "source_id": "p-1", "evidence_access": "post_visible"}],
            )
        with self.assertRaises(ValueError):
            observation_service.create_observation(
                {
                    "type": "correction",
                    "title": "缺少 soul scope",
                    "narrative": "缺少 scope_soul_name。",
                    "source_channel": "chat",
                    "visibility_scope": "soul_scoped",
                },
                [{"source_type": "post", "source_id": "p-1", "evidence_access": "source_soul_only"}],
            )
        with self.assertRaises(ValueError):
            observation_service.create_observation(
                {
                    "type": "preference",
                    "title": "错误全局 soul",
                    "narrative": "global 不应有 scope_soul_name。",
                    "source_channel": "post",
                    "visibility_scope": "global",
                    "scope_soul_name": "默认",
                },
                [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
            )
        with self.assertRaises(ValueError):
            observation_service.create_observation(
                {
                    "type": "state",
                    "title": "私密阻断",
                    "narrative": "这条不进入检索。",
                    "source_channel": "post",
                    "visibility_scope": "private_blocked",
                },
                [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
            )

    def test_scope_foreign_keys_require_existing_records(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            observation_service.create_observation(
                {
                    "type": "correction",
                    "title": "不存在的 soul",
                    "narrative": "scope_soul_name 必须存在。",
                    "source_channel": "chat",
                    "visibility_scope": "soul_scoped",
                    "scope_soul_name": "不存在",
                },
                [{"source_type": "post", "source_id": "p-1", "evidence_access": "source_soul_only"}],
            )

    def test_list_active_observations_filters_by_scope(self) -> None:
        self._create_global("全局偏好", observed_at=1.0)
        observation_service.create_observation(
            {
                "type": "insight",
                "title": "公开评论",
                "narrative": "公开评论线程观察。",
                "source_channel": "comment_thread",
                "visibility_scope": "post_visible",
                "scope_post_id": "p-1",
                "observed_at": 2.0,
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "post_visible"}],
        )
        observation_service.create_observation(
            {
                "type": "correction",
                "title": "私聊纠正",
                "narrative": "私聊纠正只属于默认。",
                "source_channel": "chat",
                "visibility_scope": "soul_scoped",
                "scope_soul_name": "默认",
                "observed_at": 3.0,
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "source_soul_only"}],
        )

        global_rows = observation_service.list_active_observations(visibility_scope="global")
        post_rows = observation_service.list_active_observations(
            visibility_scope="post_visible",
            scope_post_id="p-1",
        )
        soul_rows = observation_service.list_active_observations(
            visibility_scope="soul_scoped",
            scope_soul_name="默认",
        )

        self.assertEqual(["全局偏好"], [row["title"] for row in global_rows])
        self.assertEqual(["公开评论"], [row["title"] for row in post_rows])
        self.assertEqual(["私聊纠正"], [row["title"] for row in soul_rows])

    def test_status_updates_remove_observation_from_fts(self) -> None:
        target_id = self._create_global("保留目标")
        merged_id = self._create_global("会被合并")
        superseded_id = self._create_global("会被覆盖")
        archived_id = self._create_global("会被归档")

        observation_service.mark_merged(merged_id, target_id)
        observation_service.mark_superseded(superseded_id, target_id)
        observation_service.archive_observation(archived_id)

        rows = db.query_all(
            "SELECT rowid FROM observations_fts WHERE observations_fts MATCH ?",
            ("会被合并",),
        )
        merged = require_not_none(observation_service.get_observation(merged_id))
        superseded = require_not_none(observation_service.get_observation(superseded_id))
        archived = require_not_none(observation_service.get_observation(archived_id))

        self.assertEqual([], rows)
        self.assertEqual("merged", merged["status"])
        self.assertEqual(target_id, merged["merged_into"])
        self.assertEqual("superseded", superseded["status"])
        self.assertEqual(target_id, superseded["superseded_by"])
        self.assertEqual("archived", archived["status"])

    def test_replace_post_observations_replaces_global_post_extraction(self) -> None:
        first_ids = observation_service.replace_post_observations(
            "p-1",
            [
                {
                    "type": "insight",
                    "title": "旧 post 观察",
                    "narrative": "old_post_observation_marker",
                    "importance": 0.5,
                    "confidence": 0.6,
                }
            ],
            observed_at=2.0,
            excerpt="原文摘录",
        )
        second_ids = observation_service.replace_post_observations(
            "p-1",
            [
                {
                    "type": "decision",
                    "title": "新 post 观察",
                    "narrative": "new_post_observation_marker",
                    "importance": 0.8,
                    "confidence": 0.9,
                }
            ],
            observed_at=3.0,
            excerpt="新摘录",
        )

        self.assertEqual(1, len(first_ids))
        self.assertEqual(1, len(second_ids))
        self.assertIsNone(observation_service.get_observation(first_ids[0]))
        observation = require_not_none(observation_service.get_observation(second_ids[0]))
        self.assertEqual("global", observation["visibility_scope"])
        self.assertEqual("post", observation["source_channel"])
        self.assertEqual(3.0, observation["observed_at"])
        self.assertEqual("all", observation["sources"][0]["evidence_access"])
        self.assertEqual("新摘录", observation["sources"][0]["excerpt"])
        old_fts = db.query_all(
            "SELECT rowid FROM observations_fts WHERE observations_fts MATCH ?",
            ("old_post_observation_marker",),
        )
        new_fts = db.query_all(
            "SELECT rowid FROM observations_fts WHERE observations_fts MATCH ?",
            ("new_post_observation_marker",),
        )
        self.assertEqual([], old_fts)
        self.assertEqual([second_ids[0]], [row["rowid"] for row in new_fts])

    def test_cursor_set_get_overwrites_existing_value(self) -> None:
        observation_service.set_cursor("chat_thread", "1", "10", metadata={"phase": "first"})
        observation_service.set_cursor("chat_thread", "1", "12", metadata={"phase": "second"})

        row = require_not_none(
            db.query_one(
                """
                SELECT cursor_value, metadata
                FROM observation_cursors
                WHERE source_kind = ? AND source_key = ?
                """,
                ("chat_thread", "1"),
            )
        )

        self.assertEqual("12", observation_service.get_cursor("chat_thread", "1"))
        self.assertEqual("12", row["cursor_value"])
        self.assertIn("second", row["metadata"])

    def test_cleanup_orphan_observations_removes_missing_sources(self) -> None:
        observation_id = observation_service.create_observation(
            {
                "type": "insight",
                "title": "孤儿来源",
                "narrative": "这条来源不存在。",
                "source_channel": "post",
                "visibility_scope": "global",
            },
            [{"source_type": "post", "source_id": "missing-post", "evidence_access": "all"}],
        )

        deleted = observation_service.cleanup_orphan_observations()

        self.assertEqual(1, deleted)
        self.assertIsNone(observation_service.get_observation(observation_id))

    def test_post_and_soul_delete_do_not_leave_orphan_observations(self) -> None:
        post_observation = observation_service.create_observation(
            {
                "type": "insight",
                "title": "公开来源",
                "narrative": "这条跟 post 绑定。",
                "source_channel": "comment_thread",
                "visibility_scope": "post_visible",
                "scope_post_id": "p-1",
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "post_visible"}],
        )
        soul_observation = observation_service.create_observation(
            {
                "type": "correction",
                "title": "私聊来源",
                "narrative": "这条跟 soul 绑定。",
                "source_channel": "chat",
                "visibility_scope": "soul_scoped",
                "scope_soul_name": "默认",
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "source_soul_only"}],
        )

        db.execute("DELETE FROM posts WHERE id = ?", ("p-1",))
        db.execute("DELETE FROM souls WHERE name = ?", ("默认",))

        self.assertIsNone(observation_service.get_observation(post_observation))
        self.assertIsNone(observation_service.get_observation(soul_observation))
        self.assertEqual(
            0,
            require_not_none(db.query_one("SELECT COUNT(*) AS count FROM observation_sources"))["count"],
        )

    def _create_global(self, title: str, observed_at: float = 1.0) -> int:
        return observation_service.create_observation(
            {
                "type": "preference",
                "title": title,
                "narrative": f"{title} narrative",
                "source_channel": "post",
                "visibility_scope": "global",
                "observed_at": observed_at,
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
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

    def _insert_chat_message(self, soul_name: str, content: str) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_threads(soul_name, title, created_at, updated_at, last_message_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (soul_name, "测试私聊", 1.0, 1.0, 1.0),
            )
            thread_id = db.require_lastrowid(cursor, "chat thread insert")
            cursor = conn.execute(
                """
                INSERT INTO chat_messages(thread_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, "user", content, 1.0),
            )
            return db.require_lastrowid(cursor, "chat message insert")


if __name__ == "__main__":
    unittest.main()
