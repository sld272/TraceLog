from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_init_db_creates_state_db_with_owner_only_permissions(self) -> None:
        self.assertEqual(0o600, stat.S_IMODE(db.DB_PATH.stat().st_mode))

    def test_init_db_ignores_permission_hardening_failure(self) -> None:
        with patch("core.db.os.chmod", side_effect=OSError("unsupported permissions")):
            db.init_db()

        self.assertIsNotNone(db.query_one("SELECT value FROM meta WHERE key = 'schema_version'"))

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

    def test_legacy_goals_gain_schedule_expectation_and_link_table(self) -> None:
        legacy = Path(self.tmp.name) / "legacy-goals-workspace"
        old_ws, old_path = db.WORKSPACE_DIR, db.DB_PATH
        db.WORKSPACE_DIR = legacy
        db.DB_PATH = legacy / "state.db"
        try:
            legacy.mkdir(parents=True, exist_ok=True)
            conn = db.connect()
            conn.execute(
                """
                CREATE TABLE goals (
                    id TEXT PRIMARY KEY,
                    horizon TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            db.init_db()

            columns = {row["name"] for row in db.query_all("PRAGMA table_info(goals)")}
            link_table = db.query_one(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("goal_schedule_links",),
            )
            self.assertIn("schedule_expectation", columns)
            self.assertIsNotNone(link_table)
        finally:
            db.WORKSPACE_DIR = old_ws
            db.DB_PATH = old_path

    def test_legacy_schedule_events_gain_account_id_and_backfill_idempotently(self) -> None:
        legacy = Path(self.tmp.name) / "legacy-schedule-workspace"
        old_ws, old_path = db.WORKSPACE_DIR, db.DB_PATH
        db.WORKSPACE_DIR = legacy
        db.DB_PATH = legacy / "state.db"
        try:
            legacy.mkdir(parents=True, exist_ok=True)
            conn = db.connect()
            conn.execute(
                """
                CREATE TABLE schedule_events (
                    id TEXT PRIMARY KEY,
                    start_ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO schedule_events(id, start_ts) VALUES ('legacy-event', 1)"
            )
            conn.commit()
            conn.close()

            db.init_db()
            db.init_db()

            columns = {
                row["name"] for row in db.query_all("PRAGMA table_info(schedule_events)")
            }
            indexes = {
                row["name"] for row in db.query_all("PRAGMA index_list(schedule_events)")
            }
            event = db.query_one(
                "SELECT account_id FROM schedule_events WHERE id = 'legacy-event'"
            )
            accounts = db.query_all(
                "SELECT id, provider, display_name FROM calendar_accounts ORDER BY id"
            )
            self.assertIn("account_id", columns)
            self.assertIn("idx_schedule_events_account", indexes)
            self.assertEqual("outlook", event["account_id"])
            self.assertEqual(
                [("outlook", "outlook", "Outlook")],
                [
                    (row["id"], row["provider"], row["display_name"])
                    for row in accounts
                ],
            )
        finally:
            db.WORKSPACE_DIR = old_ws
            db.DB_PATH = old_path

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
        self.assertNotIn("todos", tables)
        self.assertEqual("1", version["value"])
        self.assertIn("jobs", tables)
        self.assertIn("post_events", tables)
        self.assertIn("vector_docs", tables)
        self.assertIn("vector_doc_tombstones", tables)
        self.assertIn("vector_index_collections", tables)
        self.assertIn("vector_index_items", tables)
        self.assertIn("vector_outbox", tables)
        self.assertIn("post_soul_orders", tables)
        self.assertIn("goal_schedule_links", tables)

    def test_init_drops_retired_todos_table(self) -> None:
        conn = db.connect()
        try:
            conn.execute(
                """
                CREATE TABLE todos (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL
                )
                """
            )
            conn.execute("INSERT INTO todos(id, task) VALUES ('legacy-1', '旧数据')")
            conn.commit()
        finally:
            conn.close()

        db.init_db()

        self.assertIsNone(
            db.query_one(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'todos'"
            )
        )

    def test_message_mutation_marker_columns_exist(self) -> None:
        comment_columns = {row["name"] for row in db.query_all("PRAGMA table_info(comments)")}
        chat_columns = {row["name"] for row in db.query_all("PRAGMA table_info(chat_messages)")}

        self.assertIn("edited_at", comment_columns)
        self.assertIn("rerun_at", comment_columns)
        self.assertIn("edited_at", chat_columns)
        self.assertIn("rerun_at", chat_columns)
        self.assertIn("metadata", chat_columns)
        self.assertIn("client_request_id", chat_columns)

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
