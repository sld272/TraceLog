from __future__ import annotations

import json
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
            "global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL, dry_run=True
        )
        # summary reflects what WOULD happen
        self.assertEqual(summary.applied, 1)
        self.assertEqual(summary.by_op, {"add": 1})
        # but nothing persisted, cursor not advanced
        self.assertEqual(len(mus.list_units("global", "public")), 0)
        self.assertEqual(mes.get_cursor("global", "public"), 0)
        self.assertEqual(len(db.query_all("SELECT * FROM memory_reconcile_runs")), 0)
        self.assertIsNone(summary.reconcile_run_id)
        # preview shows the unit contents that WOULD result
        self.assertEqual(len(summary.preview_units), 1)
        self.assertEqual(summary.preview_units[0]["content"], "预览不落库")

    def test_live_run_after_dry_run_still_works(self) -> None:
        ids = self._public_events(1)
        producer = self._producer([
            {"op": "add", "type": "goal", "content": "正式落库", "evidence_event_ids": ids},
        ])
        recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL, dry_run=True)
        recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL, dry_run=False)
        self.assertEqual(len(mus.list_units("global", "public")), 1)
        self.assertEqual(mes.get_cursor("global", "public"), ids[-1])


class TombstoneClaimBackfillTest(unittest.TestCase):
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

    def _retracted_unit(self, content: str, reason: str = "false") -> str:
        with db.transaction() as conn:
            ev = mes.record_post_mutation(
                conn, post_id=f"p-{content}", op="create", content=content, occurred_at=1.0
            ).id
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content=content, evidence_event_ids=[ev],
        )
        mus.retract_unit(unit_id, by="user", reason=reason)
        return unit_id

    def test_backfill_stores_claims_from_injected_normalizer(self) -> None:
        unit_id = self._retracted_unit("咖啡这种东西我可太讨厌了")
        seen: dict = {}

        def normalizer(items):
            seen["items"] = items
            return {items[0]["unit_id"]: "用户讨厌咖啡"}

        stored = runner.backfill_tombstone_claims(None, "model", normalizer=normalizer)
        self.assertEqual(stored, 1)
        self.assertEqual(seen["items"][0]["unit_id"], unit_id)
        self.assertEqual(mus.get_unit(unit_id)["normalized_claim"], "用户讨厌咖啡")
        # backfilled rows are not re-selected next run
        self.assertEqual(runner.backfill_tombstone_claims(None, "model", normalizer=normalizer), 0)

    def test_backfill_ignores_unknown_ids_and_empty_result(self) -> None:
        unit_id = self._retracted_unit("讨厌跑步")
        stored = runner.backfill_tombstone_claims(
            None, "model", normalizer=lambda items: {"mu_bogus": "编造", "": "空"}
        )
        self.assertEqual(stored, 0)
        self.assertIsNone(mus.get_unit(unit_id)["normalized_claim"])
        self.assertEqual(
            runner.backfill_tombstone_claims(None, "model", normalizer=lambda items: None), 0
        )


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

    def _public_events(self, n: int) -> list[int]:
        ids = []
        with db.transaction() as conn:
            for i in range(n):
                ids.append(
                    mes.record_post_mutation(
                        conn,
                        post_id=f"p{i}",
                        op="create",
                        content=f"c{i}",
                        occurred_at=float(i),
                    ).id
                )
        return ids

    @staticmethod
    def _producer(ops):
        return lambda *, boundary, events, active_units, tombstones: {
            "ops": list(ops),
            "summary": "s",
        }

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
            ce = mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create", content="私聊", occurred_at=2.0, role="user").id

        def producer(*, boundary, events, active_units, tombstones):
            ids = [e["id"] for e in events]
            return {"ops": [{"op": "add", "type": "insight", "content": f"来自 {boundary['visibility_scope']}",
                             "evidence_event_ids": ids}], "summary": ""}

        result = runner.run_pending_reconcile(client=object(), model="m", op_producer=producer)
        self.assertEqual(len(result.summaries), 2)
        self.assertEqual(result.failures, [])
        self.assertFalse(result.has_pending_after_run)
        self.assertEqual(len(mus.list_units("global", "public")), 1)
        self.assertEqual(len(mus.list_units("soul:luna", "private:soul:luna")), 1)

    def test_runner_piggybacks_reflection_decaying_stale_state(self) -> None:
        # Deep reflection rides the live reconcile pass: a stale state (>30d
        # unconfirmed) in a reconciled owner is retired to dormant.
        with db.transaction() as conn:
            ev = mes.record_post_mutation(
                conn, post_id="seed", op="create", content="立个状态", occurred_at=1.0
            ).id
        stale = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="上周在搬家", confidence=0.8, importance=0.5,
            tier="contextual", source="reflected", evidence_event_ids=[ev],
        )
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memory_units SET last_confirmed = ? WHERE id = ?",
                (db.now_ts() - 40 * 86400.0, stale),
            )

        result = runner.run_pending_reconcile(
            client=object(), model="m", op_producer=self._producer([])
        )

        self.assertIn("global", {summary.owner_scope for summary in result.summaries})
        self.assertEqual(mus.get_unit(stale)["status"], "dormant")

    def test_runner_collects_failure_and_continues_other_buckets(self) -> None:
        with db.transaction() as conn:
            public_id = mes.record_post_mutation(
                conn, post_id="p1", op="create", content="公开", occurred_at=1.0
            ).id
            private_id = mes.record_chat_mutation(
                conn,
                message_id=1,
                soul_name="luna",
                op="create",
                content="私聊",
                occurred_at=2.0,
                role="user",
            ).id

        def producer(*, boundary, events, active_units, tombstones):
            del active_units, tombstones
            if boundary["visibility_scope"] == "public":
                raise RuntimeError("public boom")
            return {"ops": [], "summary": f"consumed {len(events)}"}

        result = runner.run_pending_reconcile(client=object(), model="m", op_producer=producer)

        self.assertEqual(len(result.summaries), 1)
        self.assertEqual(
            result.failures,
            [runner.ReconcileBucketFailure("global", "public", "public boom")],
        )
        self.assertTrue(result.has_pending_after_run)
        self.assertEqual(mes.get_cursor("global", "public"), 0)
        self.assertEqual(mes.get_cursor("soul:luna", "private:soul:luna"), private_id)
        self.assertGreater(public_id, 0)

    def test_runner_reports_backlog_after_bounded_bucket_batch(self) -> None:
        event_ids = self._public_events(201)

        result = runner.run_pending_reconcile(
            client=object(),
            model="m",
            limit_per_bucket=200,
            op_producer=self._producer([]),
        )

        self.assertEqual(len(result.summaries), 1)
        self.assertEqual(result.summaries[0].event_count, 200)
        self.assertTrue(result.has_pending_after_run)
        self.assertEqual(mes.get_cursor("global", "public"), event_ids[199])

    def test_runner_dry_run_never_reports_cursor_backlog(self) -> None:
        self._public_events(201)

        result = runner.run_pending_reconcile(
            client=object(),
            model="m",
            dry_run=True,
            limit_per_bucket=200,
            op_producer=self._producer([]),
        )

        self.assertEqual(len(result.summaries), 1)
        self.assertFalse(result.has_pending_after_run)
        self.assertEqual(mes.get_cursor("global", "public"), 0)

    def test_more_than_500_buckets_leave_backlog_for_continuation(self) -> None:
        with db.transaction() as conn:
            for index in range(501):
                mes.record_chat_mutation(
                    conn,
                    message_id=index + 1,
                    soul_name=f"s{index:03d}",
                    op="create",
                    content=f"message {index}",
                    occurred_at=float(index),
                    role="user",
                )

        result = runner.run_pending_reconcile(
            client=object(),
            model="m",
            op_producer=self._producer([]),
        )

        self.assertEqual(len(result.summaries), 500)
        self.assertTrue(result.has_pending_after_run)
        self.assertEqual(len(mes.buckets_with_pending_events()), 1)

    def test_run_type_mapping(self) -> None:
        self.assertEqual(runner.run_type_for_visibility("public"), recon.RECONCILE_GLOBAL)
        self.assertEqual(runner.run_type_for_visibility("thread:20260101-001"), recon.RECONCILE_THREAD)
        self.assertEqual(runner.run_type_for_visibility("private:soul:luna"), recon.RECONCILE_SOUL_PRIVATE)


if __name__ == "__main__":
    unittest.main()
