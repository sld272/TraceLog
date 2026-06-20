from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, memory_events_service as mes, memory_unit_service as mus


class MemoryUnitServiceTest(unittest.TestCase):
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

    def _public_event(self, post_id: str = "p1") -> int:
        with db.transaction() as conn:
            return mes.record_post_mutation(
                conn, post_id=post_id, op="create", content="证据", occurred_at=1.0
            ).id

    def _private_event(self, soul: str = "luna", message_id: int = 1) -> int:
        with db.transaction() as conn:
            return mes.record_chat_mutation(
                conn, message_id=message_id, soul_name=soul, op="create", content="私聊", occurred_at=1.0
            ).id

    # --- boundary validation ----------------------------------------------

    def test_validate_boundary_accepts_coherent_pairs(self) -> None:
        mus.validate_boundary("global", "public")
        mus.validate_boundary("soul:luna", "public")
        mus.validate_boundary("global", "thread:20260101-001")
        mus.validate_boundary("soul:luna", "thread:20260101-001")
        mus.validate_boundary("soul:luna", "private:soul:luna")

    def test_validate_boundary_rejects_incoherent(self) -> None:
        with self.assertRaises(mus.BoundaryError):
            mus.validate_boundary("global", "private:soul:luna")  # private must be soul-owned
        with self.assertRaises(mus.BoundaryError):
            mus.validate_boundary("soul:luna", "private:soul:nova")  # mismatched soul
        with self.assertRaises(mus.BoundaryError):
            mus.validate_boundary("weird", "public")
        with self.assertRaises(mus.BoundaryError):
            mus.validate_boundary("global", "nonsense")

    # --- add ---------------------------------------------------------------

    def test_add_unit_creates_row_op_and_evidence(self) -> None:
        event_id = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="goal", content="用户在准备考研，焦虑但坚持", confidence=0.7,
            evidence_event_ids=[event_id], tier="core", importance=0.8,
        )
        unit = mus.get_unit(unit_id)
        self.assertEqual(unit["status"], "active")
        self.assertEqual(unit["type"], "goal")
        self.assertEqual(unit["owner_scope"], "global")

        ops = mus.list_unit_ops(unit_id=unit_id)
        self.assertEqual([o["op"] for o in ops], ["add"])

        evidence = mus.get_unit_evidence(unit_id)
        self.assertEqual([e["id"] for e in evidence], [event_id])

    def test_add_unit_rejects_evidence_from_other_bucket(self) -> None:
        private_event = self._private_event()
        with self.assertRaises(mus.BoundaryError):
            mus.add_unit(
                owner_scope="global", visibility_scope="public", source_channel="post",
                type="goal", content="跨桶证据应被拒", evidence_event_ids=[private_event],
            )
        # nothing persisted
        self.assertEqual(len(mus.list_units("global", "public")), 0)

    def test_add_unit_rejects_unknown_type(self) -> None:
        with self.assertRaises(ValueError):
            mus.add_unit(
                owner_scope="global", visibility_scope="public", source_channel="post",
                type="bogus", content="x",
            )

    # --- confirm / revise / retract / supersede ---------------------------

    def test_confirm_bumps_confidence_keeps_content(self) -> None:
        e1 = self._public_event("p1")
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content="喜欢安静的咖啡馆", confidence=0.6, evidence_event_ids=[e1],
        )
        e2 = self._public_event("p2")
        mus.confirm_unit(unit_id, evidence_event_ids=[e2])
        unit = mus.get_unit(unit_id)
        self.assertAlmostEqual(unit["confidence"], 0.65, places=6)
        self.assertEqual(unit["content"], "喜欢安静的咖啡馆")
        self.assertEqual({e["id"] for e in mus.get_unit_evidence(unit_id)}, {e1, e2})
        self.assertEqual([o["op"] for o in mus.list_unit_ops(unit_id=unit_id)], ["add", "confirm"])

    def test_revise_updates_content(self) -> None:
        e1 = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="近期在找工作", evidence_event_ids=[e1],
        )
        mus.revise_unit(unit_id, content="近期已拿到 offer，状态轻松")
        self.assertEqual(mus.get_unit(unit_id)["content"], "近期已拿到 offer，状态轻松")
        self.assertEqual([o["op"] for o in mus.list_unit_ops(unit_id=unit_id)], ["add", "revise"])

    def test_user_authored_is_fully_reconcilable(self) -> None:
        # A user-authored unit is an ordinary unit (only confidence differs):
        # the model may revise/retract it like any other when evidence moves.
        e1 = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="user",
            type="state", content="用户在准备考研", source="user_authored",
            evidence_event_ids=[e1], confidence=0.95,
        )
        mus.revise_unit(unit_id, content="用户已放弃考研")
        self.assertEqual(mus.get_unit(unit_id)["content"], "用户已放弃考研")
        mus.retract_unit(unit_id, by="model", reason="outdated")
        self.assertEqual(mus.get_unit(unit_id)["status"], "retracted_by_model")

    def test_user_retract_records_intent(self) -> None:
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="user",
            type="identity", content="我是一名研究生", source="user_authored", confidence=1.0,
        )
        mus.retract_unit(unit_id, by="user", reason="false")
        unit = mus.get_unit(unit_id)
        self.assertEqual(unit["status"], "retracted_by_user")
        self.assertEqual(unit["retraction_reason"], "false")

    def test_update_unit_user_edit_semantics(self) -> None:
        e1 = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="用户在找工作", confidence=0.6, evidence_event_ids=[e1],
        )
        mus.update_unit(unit_id, content="用户已入职，状态稳定")
        unit = mus.get_unit(unit_id)
        self.assertEqual(unit["content"], "用户已入职，状态稳定")
        self.assertEqual(unit["source"], "user_authored")
        self.assertEqual(unit["status"], "active")  # live immediately
        self.assertGreaterEqual(unit["confidence"], 0.9)  # confidence bumped
        # links are kept but marked review_pending: not yet counted as support,
        # and a re-link review is queued for the AI pass (not a blunt drop).
        self.assertEqual([e["id"] for e in mus.get_unit_evidence(unit_id)], [e1])
        self.assertEqual(mus.current_effective_evidence_for_unit(unit_id), [])
        self.assertEqual(len(mus.list_pending_relinks()), 1)
        self.assertIn("user_edit", [o["op"] for o in mus.list_unit_ops(unit_id=unit_id)])

    def test_user_retract_rejects_superseded(self) -> None:
        e1 = self._public_event("p1")
        old = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="单身", evidence_event_ids=[e1],
        )
        e2 = self._public_event("p2")
        new = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="恋爱中", evidence_event_ids=[e2],
        )
        mus.supersede_unit(old, new)
        with self.assertRaises(ValueError):
            mus.retract_unit(old, by="user", reason="false")
        # superseded history stays intact
        self.assertEqual(mus.get_unit(old)["status"], "superseded")
        self.assertEqual(mus.get_unit(old)["superseded_by"], new)

    def test_update_unit_rejects_terminal_state(self) -> None:
        e1 = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="旧状态", evidence_event_ids=[e1],
        )
        mus.retract_unit(unit_id, by="user", reason="false")
        with self.assertRaises(ValueError):
            mus.update_unit(unit_id, content="想复活")

    def test_update_unit_resolves_pending_challenge(self) -> None:
        e1 = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="旧状态", evidence_event_ids=[e1],
        )
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET status='challenged' WHERE id=?", (unit_id,))
            conn.execute(
                "INSERT INTO memory_unit_reconcile_queue"
                "(unit_id, trigger_event_id, reason, status, created_at) "
                "VALUES (?, ?, 'edit', 'pending', 1.0)",
                (unit_id, e1),
            )
        mus.update_unit(unit_id, content="用户亲自修正")
        self.assertEqual(mus.get_unit(unit_id)["status"], "active")
        pending = db.query_all(
            "SELECT id FROM memory_unit_reconcile_queue "
            "WHERE unit_id=? AND status='pending'",
            (unit_id,),
        )
        self.assertEqual(pending, [])

    def test_challenge_includes_user_authored(self) -> None:
        e1 = self._public_event("p1")
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="user",
            type="state", content="用户亲述状态", source="user_authored",
            evidence_event_ids=[e1], confidence=0.95,
        )
        with db.transaction() as conn:
            edit = mes.record_post_mutation(
                conn, post_id="p1", op="edit", content="改了内容", occurred_at=2.0
            )
            challenged = mus.challenge_units_for_source(conn, edit.id)
        self.assertIn(unit_id, challenged)
        self.assertEqual(mus.get_unit(unit_id)["status"], "challenged")

    def test_set_prompt_and_profile_policy(self) -> None:
        e1 = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content="不想被提到的事", evidence_event_ids=[e1],
        )
        # seed it into the portrait slice to prove force_exclude pulls it out now
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET in_md_slice = 1 WHERE id = ?", (unit_id,))
        mus.set_prompt_policy(unit_id, prompt_policy="no_prompt")
        self.assertEqual(mus.get_unit(unit_id)["prompt_policy"], "no_prompt")
        self.assertEqual(mus.get_unit(unit_id)["in_md_slice"], 0)  # no_prompt drops slice now
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET in_md_slice = 1 WHERE id = ?", (unit_id,))
        mus.set_profile_policy(unit_id, profile_policy="force_exclude")
        self.assertEqual(mus.get_unit(unit_id)["profile_policy"], "force_exclude")
        self.assertEqual(mus.get_unit(unit_id)["in_md_slice"], 0)  # force_exclude drops slice now
        with self.assertRaises(ValueError):
            mus.set_prompt_policy(unit_id, prompt_policy="bogus")
        with self.assertRaises(ValueError):
            mus.set_profile_policy(unit_id, profile_policy="bogus")

    def test_retract_by_model(self) -> None:
        e1 = self._public_event()
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="临时状态", evidence_event_ids=[e1],
        )
        mus.retract_unit(unit_id, by="model")
        self.assertEqual(mus.get_unit(unit_id)["status"], "retracted_by_model")

    def test_supersede_links_and_requires_same_bucket(self) -> None:
        e1 = self._public_event("p1")
        old = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="单身", evidence_event_ids=[e1],
        )
        e2 = self._public_event("p2")
        new = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="恋爱中", evidence_event_ids=[e2],
        )
        mus.supersede_unit(old, new)
        old_row = mus.get_unit(old)
        self.assertEqual(old_row["status"], "superseded")
        self.assertEqual(old_row["superseded_by"], new)

        # cross-bucket supersede rejected
        pe = self._private_event(message_id=2)
        priv = mus.add_unit(
            owner_scope="soul:luna", visibility_scope="private:soul:luna", source_channel="chat",
            type="state", content="私聊状态", evidence_event_ids=[pe],
        )
        with self.assertRaises(mus.BoundaryError):
            mus.supersede_unit(priv, new)

    # --- transaction participation ----------------------------------------

    def test_add_unit_participates_in_caller_txn(self) -> None:
        event_id = self._public_event()
        try:
            with db.immediate_transaction() as conn:
                mus.add_unit(
                    owner_scope="global", visibility_scope="public", source_channel="post",
                    type="goal", content="将被回滚", evidence_event_ids=[event_id], conn=conn,
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(len(mus.list_units("global", "public")), 0)

    def test_list_active_units_in_bucket_filters(self) -> None:
        e1 = self._public_event("p1")
        mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="goal", content="公开信念", evidence_event_ids=[e1],
        )
        pe = self._private_event()
        mus.add_unit(
            owner_scope="soul:luna", visibility_scope="private:soul:luna", source_channel="chat",
            type="state", content="私聊信念", evidence_event_ids=[pe],
        )
        public = mus.list_active_units_in_bucket("global", "public")
        self.assertEqual([u["content"] for u in public], ["公开信念"])


if __name__ == "__main__":
    unittest.main()
