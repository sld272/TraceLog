from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, observation_service


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

    def test_observation_schema_is_initialized(self) -> None:
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
        version = db.query_one("SELECT value FROM meta WHERE key = ?", ("schema_version",))

        self.assertIn("observations", tables)
        self.assertIn("observation_sources", tables)
        self.assertIn("observations_fts", tables)
        self.assertIn("observation_cursors", tables)
        self.assertEqual("1", version["value"])

    def test_observations_fts_only_indexes_active_searchable_rows(self) -> None:
        self._insert_post("p-1")
        active_id = observation_service.create_observation(
            {
                "type": "preference",
                "title": "简短回复",
                "narrative": "用户偏好简短直接的回复。",
                "source_channel": "post",
                "visibility_scope": "global",
                "observed_at": 1.0,
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
        )
        blocked_id = observation_service.create_observation(
            {
                "type": "state",
                "title": "隐藏短语",
                "narrative": "这条不应该进入全文索引。",
                "source_channel": "post",
                "visibility_scope": "private_blocked",
                "observed_at": 2.0,
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "none"}],
        )

        active = db.query_all("SELECT rowid FROM observations_fts WHERE observations_fts MATCH ?", ("简短回复",))
        blocked = db.query_all("SELECT rowid FROM observations_fts WHERE observations_fts MATCH ?", ("隐藏短语",))
        observation_service.mark_merged(active_id, blocked_id)
        stale = db.query_all("SELECT rowid FROM observations_fts WHERE observations_fts MATCH ?", ("简短",))

        self.assertEqual([active_id], [row["rowid"] for row in active])
        self.assertEqual([], blocked)
        self.assertEqual([], stale)

    def test_deleting_post_cleans_observation_sources_and_source_less_observation(self) -> None:
        self._insert_post("p-1")
        observation_id = observation_service.create_observation(
            {
                "type": "insight",
                "title": "练歌洞察",
                "narrative": "用户把练歌当成阶段目标。",
                "source_channel": "post",
                "visibility_scope": "global",
            },
            [{"source_type": "post", "source_id": "p-1", "evidence_access": "all"}],
        )

        db.execute("DELETE FROM posts WHERE id = ?", ("p-1",))

        self.assertIsNone(db.query_one("SELECT id FROM observations WHERE id = ?", (observation_id,)))
        self.assertEqual(
            0,
            db.query_one("SELECT COUNT(*) AS count FROM observation_sources")["count"],
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
