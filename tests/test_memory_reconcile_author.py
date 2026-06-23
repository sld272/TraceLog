from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    memory_events_service as mes,
    memory_reconciler as recon,
    memory_unit_service as mus,
)


class ReconcileAuthorFilterTest(unittest.TestCase):
    """Reconcile must only mine USER-authored evidence into beliefs; assistant
    output (SOUL replies) must never become memory."""

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

    def _producer_adds_from_each_event(self):
        def producer(*, boundary, events, active_units, tombstones):
            ops = [
                {"op": "add", "type": "insight", "content": f"belief::{e['content_snapshot']}",
                 "evidence_event_ids": [e["id"]]}
                for e in events
            ]
            return {"ops": ops, "summary": ""}
        return producer

    def test_assistant_only_comment_produces_nothing(self) -> None:
        # a SOUL's own comment -> global/public, author assistant
        with db.transaction() as conn:
            mes.record_comment_mutation(
                conn, comment_id=1, post_id="20260616-001", soul_name="gotoh",
                role="assistant", op="create", content="后藤独有社交恐惧症", occurred_at=1.0,
            )
        summary = recon.reconcile_bucket(
            mes.GLOBAL_SCOPE, mes.PUBLIC_VISIBILITY,
            op_producer=self._producer_adds_from_each_event(),
            run_type=recon.RECONCILE_GLOBAL,
        )
        # no user evidence -> nothing produced, but cursor still advances
        self.assertIsNone(summary)
        self.assertEqual(len(mus.list_units(mes.GLOBAL_SCOPE, mes.PUBLIC_VISIBILITY)), 0)
        self.assertGreater(mes.get_cursor(mes.GLOBAL_SCOPE, mes.PUBLIC_VISIBILITY), 0)

    def test_private_bucket_uses_only_user_messages(self) -> None:
        with db.transaction() as conn:
            mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create",
                                     content="我希望你回复简短一点", occurred_at=1.0, role="user")
            mes.record_chat_mutation(conn, message_id=2, soul_name="luna", op="create",
                                     content="好的，我会注意的（这是我作为AI的回复）", occurred_at=2.0, role="assistant")
        summary = recon.reconcile_bucket(
            "soul:luna", "private:soul:luna",
            op_producer=self._producer_adds_from_each_event(),
            run_type=recon.RECONCILE_SOUL_PRIVATE,
        )
        units = mus.list_units("soul:luna", "private:soul:luna")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["content"], "belief::我希望你回复简短一点")
        # cursor advanced past BOTH events so the assistant one is never revisited
        self.assertEqual(mes.get_cursor("soul:luna", "private:soul:luna"), 2)

    def test_assistant_evidence_cannot_be_cited_by_ops(self) -> None:
        # producer maliciously/incorrectly cites the assistant event id
        with db.transaction() as conn:
            mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create",
                                     content="用户消息", occurred_at=1.0, role="user")
            assistant = mes.record_chat_mutation(conn, message_id=2, soul_name="luna", op="create",
                                                 content="AI回复", occurred_at=2.0, role="assistant").id

        def producer(*, boundary, events, active_units, tombstones):
            return {"ops": [{"op": "add", "type": "insight", "content": "引用了AI证据",
                             "evidence_event_ids": [assistant]}], "summary": ""}

        summary = recon.reconcile_bucket(
            "soul:luna", "private:soul:luna", op_producer=producer,
            run_type=recon.RECONCILE_SOUL_PRIVATE,
        )
        # assistant event id is not in the allowed (user) set -> op skipped
        self.assertEqual(summary.applied, 0)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(len(mus.list_units("soul:luna", "private:soul:luna")), 0)

    def test_user_comment_builds_global_memory(self) -> None:
        # user commenting on a public post -> global/public, author user
        with db.transaction() as conn:
            mes.record_comment_mutation(
                conn, comment_id=1, post_id="20260616-001", soul_name="gotoh",
                role="user", op="create", content="我也想学吉他，但怕坚持不下来", occurred_at=1.0,
            )
            mes.record_comment_mutation(
                conn, comment_id=2, post_id="20260616-001", soul_name="gotoh",
                role="assistant", op="create", content="（后藤独的回复）一起加油！", occurred_at=2.0,
            )
        summary = recon.reconcile_bucket(
            mes.GLOBAL_SCOPE, mes.PUBLIC_VISIBILITY,
            op_producer=self._producer_adds_from_each_event(),
            run_type=recon.RECONCILE_GLOBAL,
        )
        units = mus.list_units(mes.GLOBAL_SCOPE, mes.PUBLIC_VISIBILITY)
        self.assertEqual(len(units), 1)  # only the user comment becomes a belief
        self.assertEqual(units[0]["content"], "belief::我也想学吉他，但怕坚持不下来")

    def test_event_author_recorded(self) -> None:
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="帖", occurred_at=1.0)
            mes.record_comment_mutation(conn, comment_id=1, post_id="p1", soul_name="luna",
                                        role="user", op="create", content="用户评论", occurred_at=2.0)
            mes.record_comment_mutation(conn, comment_id=2, post_id="p1", soul_name="luna",
                                        role="assistant", op="create", content="soul评论", occurred_at=3.0)
            mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create",
                                     content="私聊", occurred_at=4.0, role="assistant")
        rows = {(r["source_type"], r["source_id"]): r["author"]
                for r in db.query_all("SELECT source_type, source_id, author FROM memory_ingest_events")}
        self.assertEqual(rows[("post", "p1")], "user")
        self.assertEqual(rows[("comment_message", "1")], "user")
        self.assertEqual(rows[("comment_message", "2")], "assistant")
        self.assertEqual(rows[("chat_message", "1")], "assistant")


if __name__ == "__main__":
    unittest.main()
