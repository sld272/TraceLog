from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core import db, memory_events_service as mes


class MemoryEventsServiceTest(unittest.TestCase):
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

    # --- fixtures ----------------------------------------------------------

    def _seed_business_rows(self) -> None:
        now = 1000.0
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO souls(name, file_path, enabled, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                ("luna", "souls/luna.md", now, now),
            )
            conn.execute(
                "INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("20260101-001", "2026-01-01T00:00:00+00:00", "考研倒计时", now, now),
            )
            conn.execute(
                """
                INSERT INTO comments(id, post_id, soul_name, role, content, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (1, "20260101-001", "luna", "user", "我有点焦虑", 1, now),
            )
            conn.execute(
                """
                INSERT INTO comments(id, post_id, soul_name, role, content, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (2, "20260101-001", "luna", "assistant", "我懂你的感受", 2, now),
            )
            conn.execute(
                "INSERT INTO chat_threads(id, soul_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (1, "luna", now, now),
            )
            conn.execute(
                "INSERT INTO chat_messages(id, thread_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (1, 1, "user", "晚安", now),
            )

    def _all_events(self) -> list[sqlite3.Row]:
        return db.query_all("SELECT * FROM memory_ingest_events ORDER BY id ASC")

    # --- backfill ----------------------------------------------------------

    def test_backfill_seeds_create_events_with_boundaries(self) -> None:
        self._seed_business_rows()
        with db.transaction() as conn:
            inserted = mes.backfill_from_existing(conn)
        self.assertEqual(inserted, 4)

        by_source = {
            (row["source_type"], row["source_id"]): row for row in self._all_events()
        }

        post = by_source[("post", "20260101-001")]
        self.assertEqual(post["owner_scope"], "global")
        self.assertEqual(post["visibility_scope"], "public")
        self.assertEqual(post["op"], "create")
        self.assertEqual(post["source_revision"], 1)

        user_comment = by_source[("comment_message", "1")]
        self.assertEqual(user_comment["owner_scope"], "global")  # public comments join the global portrait
        self.assertEqual(user_comment["visibility_scope"], "public")
        self.assertEqual(user_comment["author"], "user")

        soul_comment = by_source[("comment_message", "2")]
        self.assertEqual(soul_comment["owner_scope"], "global")
        self.assertEqual(soul_comment["visibility_scope"], "public")

        chat = by_source[("chat_message", "1")]
        self.assertEqual(chat["owner_scope"], "soul:luna")
        self.assertEqual(chat["visibility_scope"], "private:soul:luna")

    def test_backfill_is_idempotent(self) -> None:
        self._seed_business_rows()
        with db.transaction() as conn:
            first = mes.backfill_from_existing(conn)
        with db.transaction() as conn:
            second = mes.backfill_from_existing(conn)
        self.assertEqual(first, 4)
        self.assertEqual(second, 0)
        self.assertEqual(len(self._all_events()), 4)

    def test_content_hash_recorded(self) -> None:
        self._seed_business_rows()
        with db.transaction() as conn:
            mes.backfill_from_existing(conn)
        post = db.query_one(
            "SELECT content_hash FROM memory_ingest_events WHERE source_type='post'"
        )
        self.assertIsNotNone(post["content_hash"])
        self.assertEqual(len(post["content_hash"]), 64)  # sha256 hex

    # --- append_event ------------------------------------------------------

    def test_revision_is_monotonic_per_source(self) -> None:
        with db.transaction() as conn:
            e1 = mes.record_post_mutation(conn, post_id="p1", op="create", content="a", occurred_at=1.0)
            e2 = mes.record_post_mutation(conn, post_id="p1", op="edit", content="b", occurred_at=2.0)
            e3 = mes.record_post_mutation(conn, post_id="p2", op="create", content="c", occurred_at=3.0)
        self.assertEqual(e1.source_revision, 1)
        self.assertEqual(e2.source_revision, 2)
        self.assertEqual(e3.source_revision, 1)
        self.assertLess(e1.id, e2.id)

    def test_duplicate_revision_rejected(self) -> None:
        with db.transaction() as conn:
            mes.append_event(
                conn,
                owner_scope="global",
                visibility_scope="public",
                source_channel="post",
                source_type="post",
                source_id="p1",
                op="create",
                content_snapshot="x",
                occurred_at=1.0,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO memory_ingest_events(
                        owner_scope, visibility_scope, source_channel, source_type,
                        source_id, source_revision, op, content_snapshot, content_hash,
                        occurred_at, created_at
                    ) VALUES ('global','public','post','post','p1',1,'edit','y',NULL,2.0,2.0)
                    """
                )

    def test_append_rolls_back_with_transaction(self) -> None:
        try:
            with db.transaction() as conn:
                mes.record_post_mutation(conn, post_id="p1", op="create", content="a", occurred_at=1.0)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(len(self._all_events()), 0)

    def test_invalid_op_rejected(self) -> None:
        with self.assertRaises(ValueError):
            with db.transaction() as conn:
                mes.record_post_mutation(conn, post_id="p1", op="bogus", content="a", occurred_at=1.0)

    # --- cursors -----------------------------------------------------------

    def test_cursor_advances_forward_only(self) -> None:
        self.assertEqual(mes.get_cursor("global", "public"), 0)
        with db.transaction() as conn:
            mes.advance_cursor(conn, "global", "public", 5)
        self.assertEqual(mes.get_cursor("global", "public"), 5)
        with db.transaction() as conn:
            mes.advance_cursor(conn, "global", "public", 3)  # stale, must not regress
        self.assertEqual(mes.get_cursor("global", "public"), 5)
        with db.transaction() as conn:
            mes.advance_cursor(conn, "global", "public", 9)
        self.assertEqual(mes.get_cursor("global", "public"), 9)

    def test_list_events_after_filters_by_bucket(self) -> None:
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="a", occurred_at=1.0)
            mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create", content="hi", occurred_at=2.0)
            mes.record_post_mutation(conn, post_id="p2", op="create", content="b", occurred_at=3.0)
        public = mes.list_events_after("global", "public", 0)
        self.assertEqual([r["source_id"] for r in public], ["p1", "p2"])
        private = mes.list_events_after("soul:luna", "private:soul:luna", 0)
        self.assertEqual([r["source_id"] for r in private], ["1"])


if __name__ == "__main__":
    unittest.main()
