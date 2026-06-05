from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db
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

    def test_message_mutation_marker_columns_exist(self) -> None:
        comment_columns = {row["name"] for row in db.query_all("PRAGMA table_info(comments)")}
        chat_columns = {row["name"] for row in db.query_all("PRAGMA table_info(chat_messages)")}

        self.assertIn("edited_at", comment_columns)
        self.assertIn("rerun_at", comment_columns)
        self.assertIn("edited_at", chat_columns)
        self.assertIn("rerun_at", chat_columns)

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
