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


class MemoryReconcilerTest(unittest.TestCase):
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

    def _emit_public_events(self, n: int) -> list[int]:
        ids: list[int] = []
        with db.transaction() as conn:
            for i in range(n):
                ids.append(
                    mes.record_post_mutation(
                        conn, post_id=f"p{i}", op="create", content=f"内容{i}", occurred_at=float(i)
                    ).id
                )
        return ids

    def _producer(self, ops, summary="ok"):
        def producer(*, boundary, events, active_units, tombstones):
            return {"ops": list(ops), "summary": summary}
        return producer

    # --- basic add + cursor ------------------------------------------------

    def test_reconcile_adds_unit_and_advances_cursor(self) -> None:
        ids = self._emit_public_events(2)
        producer = self._producer([
            {"op": "add", "type": "goal", "content": "用户在准备考研，焦虑但坚持",
             "confidence": 0.7, "tier": "core", "importance": 0.8, "evidence_event_ids": ids},
        ])
        summary = recon.reconcile_bucket(
            "global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL
        )
        self.assertIsNotNone(summary)
        self.assertEqual(summary.applied, 1)
        self.assertEqual(summary.by_op, {"add": 1})
        units = mus.list_active_units_in_bucket("global", "public")
        self.assertEqual(len(units), 1)
        self.assertEqual(mes.get_cursor("global", "public"), ids[-1])

        # op log links to the reflection row
        ops = mus.list_unit_ops(reflection_id=summary.reflection_id)
        self.assertEqual([o["op"] for o in ops], ["add"])

    def test_reconcile_returns_none_when_no_new_events(self) -> None:
        producer = self._producer([])
        self.assertIsNone(
            recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL)
        )

    def test_second_run_only_sees_new_events(self) -> None:
        ids1 = self._emit_public_events(1)
        seen = {}

        def producer(*, boundary, events, active_units, tombstones):
            seen["ids"] = [e["id"] for e in events]
            return {"ops": []}

        recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(seen["ids"], ids1)

        ids2 = self._emit_public_events(1)  # creates p0 again -> different id
        recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(seen["ids"], ids2)

    # --- confirm / revise / retract ---------------------------------------

    def test_confirm_and_revise_target_existing_unit(self) -> None:
        ids = self._emit_public_events(1)
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content="喜欢安静", confidence=0.6, evidence_event_ids=ids,
        )
        new_ids = self._emit_public_events(1)
        producer = self._producer([
            {"op": "confirm", "target_id": unit_id, "evidence_event_ids": new_ids, "confidence": 0.8},
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.by_op, {"confirm": 1})
        self.assertAlmostEqual(mus.get_unit(unit_id)["confidence"], 0.8, places=6)

    def test_retract_via_reconcile(self) -> None:
        ids = self._emit_public_events(1)
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="临时状态", evidence_event_ids=ids,
        )
        self._emit_public_events(1)
        producer = self._producer([{"op": "retract", "target_id": unit_id, "reason": "outdated"}])
        recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(mus.get_unit(unit_id)["status"], "retracted_by_model")

    # --- validation / safety ----------------------------------------------

    def test_add_referencing_out_of_batch_event_is_skipped(self) -> None:
        first = self._emit_public_events(1)  # consumed in run 1
        producer1 = self._producer([])
        recon.reconcile_bucket("global", "public", op_producer=producer1, reflection_type=recon.RECONCILE_GLOBAL)

        second = self._emit_public_events(1)
        # op references an event id from the FIRST (already consumed) batch -> not allowed
        producer2 = self._producer([
            {"op": "add", "type": "goal", "content": "x", "evidence_event_ids": first},
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer2, reflection_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.applied, 0)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 0)
        # cursor still advances past the second batch
        self.assertEqual(mes.get_cursor("global", "public"), second[-1])

    def test_op_targeting_other_bucket_unit_is_skipped(self) -> None:
        # a private unit
        with db.transaction() as conn:
            pe = mes.record_chat_mutation(conn, message_id=1, soul_name="luna", op="create", content="私聊", occurred_at=1.0).id
        priv = mus.add_unit(
            owner_scope="soul:luna", visibility_scope="private:soul:luna", source_channel="chat",
            type="state", content="私聊状态", evidence_event_ids=[pe],
        )
        ids = self._emit_public_events(1)
        producer = self._producer([{"op": "confirm", "target_id": priv, "evidence_event_ids": ids}])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.applied, 0)

    def test_partial_batch_applies_good_skips_bad(self) -> None:
        ids = self._emit_public_events(2)
        producer = self._producer([
            {"op": "add", "type": "goal", "content": "好的信念", "evidence_event_ids": [ids[0]]},
            {"op": "add", "type": "bogus_type", "content": "坏 type", "evidence_event_ids": [ids[1]]},
            {"op": "add", "type": "insight", "content": "", "evidence_event_ids": [ids[1]]},  # empty content
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.applied, 1)
        self.assertEqual(summary.skipped, 2)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 1)


if __name__ == "__main__":
    unittest.main()
