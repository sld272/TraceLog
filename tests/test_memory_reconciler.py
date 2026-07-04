from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
            "global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL
        )
        self.assertIsNotNone(summary)
        self.assertEqual(summary.applied, 1)
        self.assertEqual(summary.by_op, {"add": 1})
        units = mus.list_active_units_in_bucket("global", "public")
        self.assertEqual(len(units), 1)
        self.assertEqual(mes.get_cursor("global", "public"), ids[-1])

        # op log links to the reconcile run
        ops = mus.list_unit_ops(reconcile_run_id=summary.reconcile_run_id)
        self.assertEqual([o["op"] for o in ops], ["add"])

    def test_reconcile_returns_none_when_no_new_events(self) -> None:
        producer = self._producer([])
        self.assertIsNone(
            recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        )

    def test_second_run_only_sees_new_events(self) -> None:
        ids1 = self._emit_public_events(1)
        seen = {}

        def producer(*, boundary, events, active_units, tombstones):
            seen["ids"] = [e["id"] for e in events]
            return {"ops": []}

        recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(seen["ids"], ids1)

        ids2 = self._emit_public_events(1)  # creates p0 again -> different id
        recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
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
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.by_op, {"confirm": 1})
        # LLM 0.8 snaps to the nearest anchored level (0.85): free floats are
        # not comparable across models, discrete anchors are.
        self.assertAlmostEqual(mus.get_unit(unit_id)["confidence"], 0.85, places=6)

    # --- tombstone double-insurance (P2) -----------------------------------

    def _false_tombstone(self, content: str, claim: str | None = None) -> str:
        ids = self._emit_public_events(1)
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content=content, evidence_event_ids=ids,
        )
        mus.retract_unit(unit_id, by="user", reason="false")
        if claim:
            mus.set_normalized_claim(unit_id, claim)
        return unit_id

    def test_add_blocked_by_false_tombstone_exact_content(self) -> None:
        self._false_tombstone("用户讨厌咖啡")
        with db.transaction() as conn:
            new_ids = [mes.record_post_mutation(
                conn, post_id="pz", op="create", content="再说一次", occurred_at=9.0
            ).id]
        producer = self._producer([
            {"op": "add", "type": "preference", "content": "用户讨厌咖啡",
             "confidence": 0.7, "tier": "contextual", "importance": 0.5,
             "evidence_event_ids": new_ids},
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 1)
        self.assertIn("tombstone", summary.skipped_details[0]["reason"])

    def test_add_blocked_by_false_tombstone_claim_match(self) -> None:
        self._false_tombstone("咖啡这种东西我可太讨厌了", claim="用户讨厌咖啡")
        with db.transaction() as conn:
            new_ids = [mes.record_post_mutation(
                conn, post_id="pz", op="create", content="再说一次", occurred_at=9.0
            ).id]
        producer = self._producer([
            {"op": "add", "type": "preference", "content": "用户讨厌咖啡",
             "confidence": 0.7, "tier": "contextual", "importance": 0.5,
             "evidence_event_ids": new_ids},
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 1)

    def test_add_blocked_by_false_tombstone_vector_paraphrase(self) -> None:
        dead = self._false_tombstone("用户讨厌咖啡", claim="用户讨厌咖啡")
        with db.transaction() as conn:
            new_ids = [mes.record_post_mutation(
                conn, post_id="pz", op="create", content="再说一次", occurred_at=9.0
            ).id]
        hit = SimpleNamespace(
            doc_id=f"tombstone-{dead}", rank=1, distance=0.05,
            metadata={"type": "tombstone", "unit_id": dead, "owner_scope": "global",
                      "visibility_scope": "public", "reason": "false"},
        )
        producer = self._producer([
            {"op": "add", "type": "preference", "content": "咖啡这种饮品用户不太喜欢",
             "confidence": 0.7, "tier": "contextual", "importance": 0.5,
             "evidence_event_ids": new_ids},
        ])
        with patch("core.vectorstore.query_documents", return_value=[hit]):
            summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 1)
        self.assertIn("同义", summary.skipped_details[0]["reason"])

    def test_model_tombstone_in_other_bucket_does_not_block(self) -> None:
        # cross-bucket suppression is a USER privilege ("别再记这件事"); a model
        # retraction's judgment is bucket-scoped, so its vector twin elsewhere
        # must not block.
        dead = self._private_false_tombstone(
            "咖啡这种东西我可太讨厌了", "用户讨厌咖啡", by="model"
        )
        with db.transaction() as conn:
            new_ids = [mes.record_post_mutation(
                conn, post_id="pz", op="create", content="再说一次", occurred_at=9.0
            ).id]
        hit = SimpleNamespace(
            doc_id=f"tombstone-{dead}", rank=1, distance=0.05,
            metadata={"type": "tombstone", "unit_id": dead, "owner_scope": "soul:gotoh",
                      "visibility_scope": "private:soul:gotoh", "reason": "false"},
        )
        producer = self._producer([
            {"op": "add", "type": "preference", "content": "咖啡这种饮品用户不太喜欢",
             "confidence": 0.7, "tier": "contextual", "importance": 0.5,
             "evidence_event_ids": new_ids},
        ])
        with patch("core.vectorstore.query_documents", return_value=[hit]):
            summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 0)
        self.assertEqual(summary.by_op.get("add"), 1)

    def test_user_tombstone_in_other_bucket_blocks_paraphrase(self) -> None:
        dead = self._private_false_tombstone("咖啡这种东西我可太讨厌了", "用户讨厌咖啡")
        with db.transaction() as conn:
            new_ids = [mes.record_post_mutation(
                conn, post_id="pz", op="create", content="再说一次", occurred_at=9.0
            ).id]
        hit = SimpleNamespace(
            doc_id=f"tombstone-{dead}", rank=1, distance=0.05,
            metadata={"type": "tombstone", "unit_id": dead, "owner_scope": "soul:gotoh",
                      "visibility_scope": "private:soul:gotoh", "reason": "false"},
        )
        producer = self._producer([
            {"op": "add", "type": "preference", "content": "咖啡这种饮品用户不太喜欢",
             "confidence": 0.7, "tier": "contextual", "importance": 0.5,
             "evidence_event_ids": new_ids},
        ])
        with patch("core.vectorstore.query_documents", return_value=[hit]):
            summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 1)

    def test_tombstone_vector_failure_fails_open(self) -> None:
        self._false_tombstone("用户讨厌咖啡", claim="用户讨厌咖啡")
        with db.transaction() as conn:
            new_ids = [mes.record_post_mutation(
                conn, post_id="pz", op="create", content="再说一次", occurred_at=9.0
            ).id]
        producer = self._producer([
            {"op": "add", "type": "preference", "content": "咖啡这种饮品用户不太喜欢",
             "confidence": 0.7, "tier": "contextual", "importance": 0.5,
             "evidence_event_ids": new_ids},
        ])
        with patch("core.vectorstore.query_documents", side_effect=RuntimeError("index down")):
            summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        # paraphrase slips through when the index is down (prompt-level
        # suppression is the remaining line) — but nothing crashes.
        self.assertEqual(summary.by_op.get("add"), 1)

    # --- owner-level suppression & tombstone lifecycle ----------------------

    def _private_false_tombstone(self, content: str, claim: str, *, by: str = "user") -> str:
        with db.transaction() as conn:
            ev = mes.record_chat_mutation(
                conn, message_id=999, soul_name="gotoh", op="create",
                content=content, occurred_at=9.0, role="user",
            ).id
        unit_id = mus.add_unit(
            owner_scope="soul:gotoh", visibility_scope="private:soul:gotoh",
            source_channel="chat", type="preference", content=content,
            evidence_event_ids=[ev],
        )
        mus.retract_unit(unit_id, by=by, reason="false")
        mus.set_normalized_claim(unit_id, claim)
        return unit_id

    def test_user_retract_suppresses_across_buckets_via_claim_only(self) -> None:
        self._private_false_tombstone("咖啡这种东西我可太讨厌了", "用户讨厌咖啡")
        tombs = recon._load_tombstones("global", "public")
        contents = [t["content"] for t in tombs]
        self.assertIn("用户讨厌咖啡", contents)          # the claim crosses buckets
        self.assertNotIn("咖啡这种东西我可太讨厌了", contents)  # raw private wording never does

    def test_model_retract_stays_bucket_local(self) -> None:
        self._private_false_tombstone("咖啡这种东西我可太讨厌了", "用户讨厌咖啡", by="model")
        self.assertEqual([], recon._load_tombstones("global", "public"))
        self.assertEqual(1, len(recon._load_tombstones("soul:gotoh", "private:soul:gotoh")))

    def test_cross_bucket_user_false_tombstone_blocks_exact_add(self) -> None:
        self._private_false_tombstone("咖啡这种东西我可太讨厌了", "用户讨厌咖啡")
        new_ids = self._emit_public_events(1)
        producer = self._producer([
            {"op": "add", "type": "preference", "content": "用户讨厌咖啡",
             "confidence": 0.7, "tier": "contextual", "importance": 0.5,
             "evidence_event_ids": new_ids},
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 1)

    def test_outdated_tombstone_expires_when_belief_reforms(self) -> None:
        ids = self._emit_public_events(2)
        dead = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="用户在准备考研", evidence_event_ids=[ids[0]],
        )
        mus.retract_unit(dead, by="user", reason="outdated")
        mus.set_normalized_claim(dead, "用户在准备考研")
        self.assertEqual(1, len(recon._load_tombstones("global", "public")))
        # the belief legitimately re-forms -> the gravestone retires
        mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="用户在准备考研", evidence_event_ids=[ids[1]],
        )
        self.assertEqual([], recon._load_tombstones("global", "public"))

    def test_retract_via_reconcile(self) -> None:
        ids = self._emit_public_events(1)
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="临时状态", evidence_event_ids=ids,
        )
        self._emit_public_events(1)
        producer = self._producer([{"op": "retract", "target_id": unit_id, "reason": "outdated"}])
        recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(mus.get_unit(unit_id)["status"], "retracted_by_model")

    # --- validation / safety ----------------------------------------------

    def test_add_referencing_out_of_batch_event_is_skipped(self) -> None:
        first = self._emit_public_events(1)  # consumed in run 1
        producer1 = self._producer([])
        recon.reconcile_bucket("global", "public", op_producer=producer1, run_type=recon.RECONCILE_GLOBAL)

        second = self._emit_public_events(1)
        # op references an event id from the FIRST (already consumed) batch -> not allowed
        producer2 = self._producer([
            {"op": "add", "type": "goal", "content": "x", "evidence_event_ids": first},
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer2, run_type=recon.RECONCILE_GLOBAL)
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
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.applied, 0)

    def test_low_importance_add_is_rejected_as_trivia(self) -> None:
        ids = self._emit_public_events(1)
        producer = self._producer([
            {"op": "add", "type": "state", "content": "用户当时正在上课",
             "confidence": 0.9, "tier": "episodic", "importance": 0.2, "evidence_event_ids": ids},
        ])
        summary = recon.reconcile_bucket(
            "global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL
        )
        self.assertEqual(summary.applied, 0)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 0)
        # cursor still advances
        self.assertEqual(mes.get_cursor("global", "public"), ids[-1])

    def test_meaningful_state_above_floor_is_kept(self) -> None:
        ids = self._emit_public_events(1)
        producer = self._producer([
            {"op": "add", "type": "state", "content": "这阵子在准备考研，压力大",
             "confidence": 0.8, "tier": "contextual", "importance": 0.5, "evidence_event_ids": ids},
        ])
        summary = recon.reconcile_bucket(
            "global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL
        )
        self.assertEqual(summary.applied, 1)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 1)

    def test_partial_batch_applies_good_skips_bad(self) -> None:
        ids = self._emit_public_events(2)
        producer = self._producer([
            {"op": "add", "type": "goal", "content": "好的信念", "evidence_event_ids": [ids[0]]},
            {"op": "add", "type": "bogus_type", "content": "坏 type", "evidence_event_ids": [ids[1]]},
            {"op": "add", "type": "insight", "content": "", "evidence_event_ids": [ids[1]]},  # empty content
        ])
        summary = recon.reconcile_bucket("global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL)
        self.assertEqual(summary.applied, 1)
        self.assertEqual(summary.skipped, 2)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 1)


if __name__ == "__main__":
    unittest.main()
