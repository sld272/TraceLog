from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, memory_events_service, memory_unit_service, memory_view_service
from tests.helpers import require_not_none


class DbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_validate_fts5_trigram_uses_unique_probe_and_cleans_up(self) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("__fts5_probe__", "1970-01-01T00:00:00+00:00", "legacy probe", 0.0, 0.0),
        )

        conn = db.connect()
        try:
            db._validate_fts5_trigram(conn)
            db._validate_fts5_trigram(conn)
            conn.commit()
        finally:
            conn.close()

        legacy = db.query_one("SELECT id FROM posts WHERE id = ?", ("__fts5_probe__",))
        generated = db.query_all("SELECT id FROM posts WHERE id LIKE ?", ("__fts5_probe__:%",))
        self.assertIsNotNone(legacy)
        self.assertEqual([], generated)

    def test_schema_version_is_current_and_observation_tables_are_absent(self) -> None:
        tables = {
            row["name"]
            for row in db.query_all(
                """
                SELECT name
                FROM sqlite_master
                WHERE type IN ('table', 'virtual table')
                """
            )
        }
        version = require_not_none(db.query_one("SELECT value FROM meta WHERE key = ?", ("schema_version",)))

        self.assertNotIn("observations", tables)
        self.assertNotIn("observation_sources", tables)
        self.assertNotIn("observations_fts", tables)
        self.assertNotIn("observation_cursors", tables)
        self.assertEqual("1", version["value"])
        self.assertIn("jobs", tables)
        self.assertIn("post_events", tables)
        self.assertIn("vector_docs", tables)
        self.assertIn("vector_doc_tombstones", tables)
        self.assertIn("vector_index_collections", tables)
        self.assertIn("vector_index_items", tables)
        self.assertIn("vector_outbox", tables)
        self.assertIn("post_soul_orders", tables)

    def test_message_mutation_marker_columns_exist(self) -> None:
        comment_columns = {row["name"] for row in db.query_all("PRAGMA table_info(comments)")}
        chat_columns = {row["name"] for row in db.query_all("PRAGMA table_info(chat_messages)")}

        self.assertIn("edited_at", comment_columns)
        self.assertIn("rerun_at", comment_columns)
        self.assertIn("edited_at", chat_columns)
        self.assertIn("rerun_at", chat_columns)
        self.assertIn("metadata", chat_columns)

    def test_post_soul_order_backfill_preserves_existing_display_order(self) -> None:
        self._insert_post("p-order")
        for name in ["A", "B"]:
            db.execute(
                """
                INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, 1, 0, ?, ?)
                """,
                (name, f"souls/{name}.md", 1.0, 1.0),
            )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
            VALUES (?, ?, 'assistant', ?, 0, ?)
            """,
            ("p-order", "B", "B reply", 2.0),
        )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
            VALUES (?, ?, 'assistant', ?, 0, ?)
            """,
            ("p-order", "A", "A reply", 3.0),
        )

        db.init_db()

        rows = db.query_all(
            """
            SELECT soul_name, sort_order
            FROM post_soul_orders
            WHERE post_id = ?
            ORDER BY sort_order ASC
            """,
            ("p-order",),
        )
        self.assertEqual([("B", 0), ("A", 1)], [(row["soul_name"], row["sort_order"]) for row in rows])

    def test_goal_unit_migration_is_idempotent_and_stales_portrait(self) -> None:
        with db.transaction() as conn:
            event = memory_events_service.record_post_mutation(
                conn,
                post_id="p-goal",
                op="create",
                content="这学期完成课程项目",
                occurred_at=1.0,
            )
        unit_id = memory_unit_service.add_unit(
            owner_scope="global",
            visibility_scope="public",
            source_channel="post",
            type="goal",
            content="这学期完成课程项目",
            confidence=0.95,
            tier="core",
            importance=0.9,
            evidence_event_ids=[event.id],
            source="user_authored",
        )
        legacy_view = memory_view_service.synthesize_view(
            "global", "public", memory_view_service.VIEW_USER_MD
        )
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET in_md_slice = 1 WHERE id = ?", (unit_id,))
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_view_units(view_id, unit_id, order_index)
                VALUES (?, ?, 0)
                """,
                (legacy_view.view_id, unit_id),
            )
            conn.execute(
                "UPDATE memory_views SET content_md = ?, status = 'fresh' WHERE id = ?",
                ("## 目标\n- 这学期完成课程项目", legacy_view.view_id),
            )
        db.execute(
            "DELETE FROM meta WHERE key = 'memory_v2_goaltool_migration_v1'"
        )

        db.init_db()

        goals = db.query_all("SELECT * FROM goals")
        unit = require_not_none(db.query_one("SELECT * FROM memory_units WHERE id = ?", (unit_id,)))
        view = require_not_none(
            db.query_one(
                """
                SELECT * FROM memory_views
                WHERE owner_scope = 'global' AND visibility_scope = 'public'
                  AND view_type = 'user_md'
                """
            )
        )
        self.assertEqual(1, len(goals))
        self.assertEqual("short", goals[0]["horizon"])
        self.assertEqual("suggested_accepted", goals[0]["source"])
        self.assertEqual(1, goals[0]["focus"])
        self.assertEqual("retracted_by_model", unit["status"])
        self.assertEqual("outdated", unit["retraction_reason"])
        self.assertEqual(0, unit["in_md_slice"])
        self.assertEqual("stale", view["status"])

        db.init_db()
        self.assertEqual(1, require_not_none(db.query_one("SELECT COUNT(*) AS count FROM goals"))["count"])

    def test_legacy_soul_memory_becomes_hidden_candidates_and_old_view_is_removed(self) -> None:
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES ('luna', 'souls/luna.md', 1, 0, 1.0, 1.0)
            """
        )
        memory_dir = self.workspace / "soul_memories"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "luna.md").write_text(
            "# luna 的相处记忆\n\n"
            "## 我们之间的互动约定\n"
            "- 用户难过时希望先陪伴，不急着讲道理\n"
            "- 双方习惯称呼彼此为老友\n",
            encoding="utf-8",
        )
        db.execute(
            """
            INSERT INTO memory_views(
                id, owner_scope, visibility_scope, view_type, content_md,
                source_unit_set_hash, renderer_version, status, generated_at, updated_at
            ) VALUES (
                'mv_old', 'soul:luna', 'private:soul:luna', 'soul_private_memory',
                'legacy', 'sha256:old', 'baseline-v1', 'fresh', 1.0, 1.0
            )
            """
        )
        db.execute(
            "DELETE FROM meta WHERE key = 'memory_v2_soul_relationship_migration_v1'"
        )

        db.init_db()

        rows = db.query_all(
            """
            SELECT *
            FROM memory_units
            WHERE owner_scope = 'soul:luna' AND source = 'migrated'
            ORDER BY content
            """
        )
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["type"] == "relationship" for row in rows))
        self.assertTrue(all(row["status"] == "pending" for row in rows))
        self.assertTrue(all(row["prompt_policy"] == "no_prompt" for row in rows))
        self.assertIsNone(db.query_one("SELECT 1 FROM memory_views WHERE id = 'mv_old'"))

        candidate = rows[0]
        memory_unit_service.update_unit(candidate["id"], content=candidate["content"])
        promoted = require_not_none(memory_unit_service.get_unit(candidate["id"]))
        self.assertEqual("active", promoted["status"])
        self.assertEqual("user_authored", promoted["source"])
        self.assertEqual("allow", promoted["prompt_policy"])

        db.init_db()
        count = require_not_none(
            db.query_one(
                "SELECT COUNT(*) AS count FROM memory_units "
                "WHERE owner_scope = 'soul:luna' AND source = 'migrated'"
            )
        )
        self.assertEqual(1, count["count"])

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
