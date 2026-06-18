from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    memory_events_service as mes,
    memory_reconcile_runner as runner,
    memory_reconciler as recon,
    memory_unit_service as mus,
)


class DryRunTest(unittest.TestCase):
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

    def _public_events(self, n: int) -> list[int]:
        ids = []
        with db.transaction() as conn:
            for i in range(n):
                ids.append(mes.record_post_mutation(conn, post_id=f"p{i}", op="create", content=f"c{i}", occurred_at=float(i)).id)
        return ids

    def _producer(self, ops):
        return lambda *, boundary, events, active_units, tombstones: {"ops": list(ops), "summary": "s"}

    def test_dry_run_previews_without_persisting(self) -> None:
        ids = self._public_events(2)
        producer = self._producer([
            {"op": "add", "type": "goal", "content": "预览不落库", "evidence_event_ids": ids},
        ])
        summary = recon.reconcile_bucket(
            "global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL, dry_run=True
        )
        # summary reflects what WOULD happen
        self.assertEqual(summary.applied, 1)
        self.assertEqual(summary.by_op, {"add": 1})
        # but nothing persisted, cursor not advanced
        self.assertEqual(len(mus.list_units("global", "public")), 0)
        self.assertEqual(mes.get_cursor("global", "public"), 0)
        self.assertEqual(len(db.query_all("SELECT * FROM reflections")), 0)
        self.assertIsNone(summary.reflection_id)

    def test_live_run_after_dry_run_still_works(self) -> None:
        ids = self._public_events(1)
        producer = self._producer([
            {"op": "add", "type": "goal", "content": "正式落库", "evidence_event_ids": ids},
        ])
        recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL, dry_run=True)
        recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL, dry_run=False)
        self.assertEqual(len(mus.list_units("global", "public")), 1)
        self.assertEqual(mes.get_cursor("global", "public"), ids[-1])


class BucketDiscoveryTest(unittest.TestCase):
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

    def test_buckets_with_pending_events(self) -> None:
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="a", occurred_at=1.0)
            mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create", content="b", occurred_at=2.0)
        buckets = mes.buckets_with_pending_events()
        self.assertIn(("global", "public"), buckets)
        self.assertIn(("soul:luna", "private:soul:luna"), buckets)

        # after consuming public, only the private bucket remains pending
        with db.transaction() as conn:
            mes.advance_cursor(conn, "global", "public", 10_000)
        buckets = mes.buckets_with_pending_events()
        self.assertNotIn(("global", "public"), buckets)
        self.assertIn(("soul:luna", "private:soul:luna"), buckets)

    def test_runner_reconciles_all_buckets_with_injected_producer(self) -> None:
        with db.transaction() as conn:
            pe = mes.record_post_mutation(conn, post_id="p1", op="create", content="公开", occurred_at=1.0).id
            ce = mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create", content="私聊", occurred_at=2.0).id

        def producer(*, boundary, events, active_units, tombstones):
            ids = [e["id"] for e in events]
            return {"ops": [{"op": "add", "type": "insight", "content": f"来自 {boundary['visibility_scope']}",
                             "evidence_event_ids": ids}], "summary": ""}

        summaries = runner.run_pending_reconcile(client=object(), model="m", op_producer=producer)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(len(mus.list_units("global", "public")), 1)
        self.assertEqual(len(mus.list_units("soul:luna", "private:soul:luna")), 1)

    def test_reflection_type_mapping(self) -> None:
        self.assertEqual(runner.reflection_type_for_visibility("public"), recon.RECONCILE_GLOBAL)
        self.assertEqual(runner.reflection_type_for_visibility("thread:20260101-001"), recon.RECONCILE_THREAD)
        self.assertEqual(runner.reflection_type_for_visibility("private:soul:luna"), recon.RECONCILE_SOUL_PRIVATE)


if __name__ == "__main__":
    unittest.main()
