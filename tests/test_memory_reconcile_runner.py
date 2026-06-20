from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    legacy_relationship_migration as lrm,
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
        # preview shows the unit contents that WOULD result
        self.assertEqual(len(summary.preview_units), 1)
        self.assertEqual(summary.preview_units[0]["content"], "预览不落库")

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

    def _legacy_candidate(self, soul_name: str, content: str) -> str:
        unit_id = mus.new_unit_id()
        now = db.now_ts()
        db.execute(
            """
            INSERT INTO memory_units(
                id, owner_scope, visibility_scope, source_channel, prompt_policy,
                type, content, confidence, source, status, tier, profile_policy,
                importance, sensitivity, first_seen, last_confirmed,
                metadata, created_at, updated_at
            ) VALUES (?, ?, ?, 'migration', 'no_prompt', 'relationship', ?,
                      0.4, 'migrated', 'pending', 'contextual', 'auto', 0.5,
                      'normal', ?, ?, ?, ?, ?)
            """,
            (
                unit_id,
                f"soul:{soul_name}",
                f"private:soul:{soul_name}",
                content,
                now,
                now,
                json.dumps({"migration": "legacy_soul_memory"}),
                now,
                now,
            ),
        )
        return unit_id

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

    def test_runner_verifies_legacy_candidate_into_evidence_bucket(self) -> None:
        candidate_id = self._legacy_candidate(
            "luna",
            "用户难过时希望先陪伴，不急着讲道理",
        )
        with db.transaction() as conn:
            event_id = mes.record_chat_mutation(
                conn,
                message_id=1,
                soul_name="luna",
                role="user",
                op="create",
                content="我难过的时候你先陪我一会儿，别急着分析",
                occurred_at=1.0,
            ).id

        def migration_judge(*, candidate, evidence):
            self.assertEqual(candidate["id"], candidate_id)
            self.assertIn(event_id, {item["id"] for item in evidence})
            return {
                "decision": "revise",
                "content": "用户难过时希望 luna 先陪伴，再考虑分析",
                "evidence_event_ids": [event_id],
                "confidence": 0.9,
                "importance": 0.85,
            }

        result = runner.run_pending_reconcile(
            client=object(),
            model="m",
            op_producer=self._producer([]),
            migration_judge=migration_judge,
        )

        self.assertEqual(result.migration_failures, [])
        candidate = mus.get_unit(candidate_id)
        self.assertEqual(candidate["status"], "superseded")
        active = mus.list_active_units_in_bucket(
            "soul:luna",
            "private:soul:luna",
        )
        self.assertEqual(1, len(active))
        self.assertEqual(
            "用户难过时希望 luna 先陪伴，再考虑分析",
            active[0]["content"],
        )
        self.assertEqual("core", active[0]["tier"])
        self.assertEqual(
            [event_id],
            [row["id"] for row in mus.get_unit_evidence(active[0]["id"])],
        )
        self.assertFalse(result.has_pending_after_run)

    def test_deferred_legacy_candidate_waits_for_new_evidence(self) -> None:
        candidate_id = self._legacy_candidate("luna", "双方习惯称呼彼此为老友")
        calls = []

        result = runner.run_pending_reconcile(
            client=object(),
            model="m",
            op_producer=self._producer([]),
            migration_judge=lambda **kwargs: calls.append(kwargs),
        )
        self.assertEqual(calls, [])
        self.assertFalse(result.has_pending_after_run)
        self.assertFalse(lrm.has_due_candidates())

        with db.transaction() as conn:
            event_id = mes.record_chat_mutation(
                conn,
                message_id=2,
                soul_name="luna",
                role="user",
                op="create",
                content="以后还是别叫我老友了",
                occurred_at=2.0,
            ).id
        self.assertTrue(lrm.has_due_candidates())

        result2 = runner.run_pending_reconcile(
            client=object(),
            model="m",
            op_producer=self._producer([]),
            migration_judge=lambda **kwargs: {
                "decision": "retract",
                "content": "",
                "evidence_event_ids": [event_id],
                "confidence": 0.9,
                "importance": 0.8,
            },
        )
        self.assertEqual(result2.migration_failures, [])
        self.assertEqual("retracted_by_model", mus.get_unit(candidate_id)["status"])

    def test_legacy_candidate_rejects_cross_bucket_evidence(self) -> None:
        candidate_id = self._legacy_candidate("luna", "用户希望先陪伴再建议")
        with db.transaction() as conn:
            private_id = mes.record_chat_mutation(
                conn,
                message_id=3,
                soul_name="luna",
                role="user",
                op="create",
                content="先陪我一会儿",
                occurred_at=3.0,
            ).id
            thread_id = mes.record_comment_mutation(
                conn,
                comment_id=4,
                post_id="p1",
                soul_name="luna",
                role="user",
                op="create",
                content="然后再给建议",
                occurred_at=4.0,
            ).id

        result = runner.run_pending_reconcile(
            client=object(),
            model="m",
            op_producer=self._producer([]),
            migration_judge=lambda **kwargs: {
                "decision": "confirm",
                "content": "",
                "evidence_event_ids": [private_id, thread_id],
                "confidence": 0.9,
                "importance": 0.8,
            },
        )
        self.assertEqual(1, len(result.migration_failures))
        self.assertIn("同一 bucket", result.migration_failures[0].error)
        self.assertEqual("pending", mus.get_unit(candidate_id)["status"])

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

    def test_reflection_type_mapping(self) -> None:
        self.assertEqual(runner.reflection_type_for_visibility("public"), recon.RECONCILE_GLOBAL)
        self.assertEqual(runner.reflection_type_for_visibility("thread:20260101-001"), recon.RECONCILE_THREAD)
        self.assertEqual(runner.reflection_type_for_visibility("private:soul:luna"), recon.RECONCILE_SOUL_PRIVATE)


if __name__ == "__main__":
    unittest.main()
