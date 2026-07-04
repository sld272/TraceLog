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

    def test_legacy_table_gains_migrated_columns_on_init(self) -> None:
        # a DB created before contested_at existed: CREATE IF NOT EXISTS skips
        # the table, so only the column migration can add it.
        legacy = Path(self.tmp.name) / "legacy-workspace"
        old_ws, old_path = db.WORKSPACE_DIR, db.DB_PATH
        db.WORKSPACE_DIR = legacy
        db.DB_PATH = legacy / "state.db"
        try:
            legacy.mkdir(parents=True, exist_ok=True)
            conn = db.connect()
            # the columns schema.sql's indexes/triggers reference must already
            # exist, as they would in any real pre-migration DB
            conn.execute(
                """
                CREATE TABLE memory_units (
                    id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL DEFAULT 'global',
                    visibility_scope TEXT NOT NULL DEFAULT 'public',
                    status TEXT NOT NULL DEFAULT 'active',
                    prompt_policy TEXT NOT NULL DEFAULT 'allow',
                    in_portrait INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()
            conn.close()
            db.init_db()
            conn = db.connect()
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_units)")}
            conn.close()
            self.assertIn("contested_at", columns)
        finally:
            db.WORKSPACE_DIR = old_ws
            db.DB_PATH = old_path

    def test_validate_fts5_trigram_uses_unique_probe_and_cleans_up(self) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("__fts5_probe__", "1970-01-01T00:00:00+00:00", "fts probe", 0.0, 0.0),
        )

        conn = db.connect()
        try:
            db._validate_fts5_trigram(conn)
            db._validate_fts5_trigram(conn)
            conn.commit()
        finally:
            conn.close()

        probe = db.query_one("SELECT id FROM posts WHERE id = ?", ("__fts5_probe__",))
        generated = db.query_all("SELECT id FROM posts WHERE id LIKE ?", ("__fts5_probe__:%",))
        self.assertIsNotNone(probe)
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
