from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    memory_events_service as mes,
    memory_unit_service as mus,
)


class CommentOwnershipMigrationTest(unittest.TestCase):
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

    def test_migration_drops_orphaned_global_thread_units_and_cursor(self) -> None:
        # Simulate pre-migration state: reconcile ran on the OLD (global, thread:*)
        # bucket and produced a unit + cursor before comment events were re-owned
        # to their soul. These are now orphaned and must be cleared so the new
        # soul-owned bucket reconciles the migrated evidence from scratch.
        with db.transaction() as conn:
            ev = mes.append_event(
                conn, owner_scope="global", visibility_scope="thread:p1",
                source_channel="comment", source_type="comment_message", source_id="1",
                op="create", content_snapshot="用户评论", occurred_at=1.0, author="user",
            ).id
        uid = mus.add_unit(
            owner_scope="global", visibility_scope="thread:p1", source_channel="comment",
            type="insight", content="用户喜欢直球", confidence=0.7, tier="contextual",
            importance=0.6, evidence_event_ids=[ev],
        )
        with db.transaction() as conn:
            mes.advance_cursor(conn, "global", "thread:p1", ev)

        with db.transaction() as conn:
            db._migrate_comment_event_ownership(conn)

        self.assertIsNone(mus.get_unit(uid))
        self.assertEqual(mes.get_cursor("global", "thread:p1"), 0)
        self.assertEqual(
            db.query_all("SELECT 1 FROM memory_unit_evidence WHERE unit_id = ?", (uid,)), []
        )

    def test_migration_leaves_public_units_untouched(self) -> None:
        # global+public is the user portrait bucket, NOT a comment thread — it
        # must survive the comment-ownership migration.
        with db.transaction() as conn:
            ev = mes.record_post_mutation(conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0).id
        uid = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="goal", content="用户在准备考研", confidence=0.9, tier="core",
            importance=0.85, evidence_event_ids=[ev],
        )
        with db.transaction() as conn:
            db._migrate_comment_event_ownership(conn)
        self.assertIsNotNone(mus.get_unit(uid))

    def test_migration_is_idempotent_in_steady_state(self) -> None:
        with db.transaction() as conn:
            db._migrate_comment_event_ownership(conn)
        with db.transaction() as conn:
            db._migrate_comment_event_ownership(conn)


if __name__ == "__main__":
    unittest.main()
