from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_linker,
    memory_unit_service as mus,
)


class MemoryLinkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self._seq = 0

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _public_unit(self, content: str, **kwargs) -> str:
        self._seq += 1
        with db.transaction() as conn:
            ev = mes.record_post_mutation(
                conn, post_id=f"p{self._seq}", op="create", content=content,
                occurred_at=float(self._seq),
            ).id
        return mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type=kwargs.pop("type", "state"), content=content,
            evidence_event_ids=[ev], **kwargs,
        )

    def _private_unit(self, content: str, soul: str = "gotoh", **kwargs) -> str:
        self._seq += 1
        with db.transaction() as conn:
            ev = mes.record_chat_mutation(
                conn, message_id=self._seq, soul_name=soul, op="create",
                content=content, occurred_at=float(self._seq), role="user",
            ).id
        return mus.add_unit(
            owner_scope=f"soul:{soul}", visibility_scope=f"private:soul:{soul}",
            source_channel="chat", type=kwargs.pop("type", "state"), content=content,
            evidence_event_ids=[ev], **kwargs,
        )

    # --- link primitives ----------------------------------------------------

    def test_add_link_normalizes_order_and_replaces_relation(self) -> None:
        a = self._public_unit("在考研")
        b = self._private_unit("其实已经放弃考研了")
        mus.add_unit_link(b, a, "contradicts")
        self.assertTrue(mus.linked_pair_exists(a, b))
        # a later verdict replaces the relation instead of stacking
        mus.add_unit_link(a, b, "context_variant")
        links = mus.links_for_units([a, b])
        self.assertEqual(1, len(links))
        self.assertEqual("context_variant", links[0]["relation"])

    def test_add_link_rejects_self_and_unknown_relation(self) -> None:
        a = self._public_unit("在考研")
        with self.assertRaises(ValueError):
            mus.add_unit_link(a, a, "same_fact")
        b = self._private_unit("x")
        with self.assertRaises(ValueError):
            mus.add_unit_link(a, b, "friends")

    # --- linker pass ---------------------------------------------------------

    def test_contradiction_marks_public_side_contested(self) -> None:
        public = self._public_unit("在准备考研", tier="core", importance=0.8, confidence=0.9)
        private = self._private_unit("已经放弃考研了")
        from core import memory_view_service as mvs
        self.assertIn(public, mvs.recompute_portrait_membership("global", "public"))

        neighbor = SimpleNamespace(
            doc_id=f"unit-{private}", rank=1, distance=0.2,
            metadata={"type": "unit", "unit_id": private},
        )

        def judge(pairs):
            return [{"a": pairs[0]["a"]["unit_id"], "b": pairs[0]["b"]["unit_id"],
                     "relation": "contradicts"}]

        with patch("core.vectorstore.query_documents", return_value=[neighbor]):
            result = memory_linker.run_linker_pass(None, "m", judge=judge)

        self.assertEqual(result.linked, 1)
        self.assertEqual(result.contested, 1)
        self.assertTrue(mus.linked_pair_exists(public, private))
        unit = mus.get_unit(public)
        self.assertTrue(unit["contested_at"])          # the more-public side
        self.assertEqual(unit["in_portrait"], 0)       # out of the assertive portrait
        self.assertIsNone(mus.get_unit(private)["contested_at"])

    def test_same_fact_links_without_contesting(self) -> None:
        public = self._public_unit("喜欢安静的咖啡馆")
        private = self._private_unit("喜欢安静的咖啡馆")  # exact twin -> no vector needed

        def judge(pairs):
            return [{"a": pairs[0]["a"]["unit_id"], "b": pairs[0]["b"]["unit_id"],
                     "relation": "same_fact"}]

        result = memory_linker.run_linker_pass(None, "m", judge=judge)
        self.assertEqual(result.linked, 1)
        self.assertEqual(result.contested, 0)
        self.assertTrue(mus.linked_pair_exists(public, private))
        self.assertIsNone(mus.get_unit(public)["contested_at"])

    def test_unrelated_verdict_and_fabricated_pairs_ignored(self) -> None:
        a = self._public_unit("喜欢跑步")
        b = self._private_unit("喜欢跑步")

        def judge(pairs):
            return [
                {"a": pairs[0]["a"]["unit_id"], "b": pairs[0]["b"]["unit_id"], "relation": "unrelated"},
                {"a": "mu_fake1", "b": "mu_fake2", "relation": "contradicts"},
            ]

        result = memory_linker.run_linker_pass(None, "m", judge=judge)
        self.assertEqual(result.linked, 0)
        self.assertFalse(mus.linked_pair_exists(a, b))

    def test_judge_failure_keeps_cursor_for_retry(self) -> None:
        self._public_unit("喜欢安静的咖啡馆")
        self._private_unit("喜欢安静的咖啡馆")
        result = memory_linker.run_linker_pass(None, "m", judge=lambda pairs: None)
        self.assertEqual(result.judged_pairs, 1)
        self.assertEqual(result.linked, 0)
        # cursor untouched -> the same pair is offered again next run
        retry = memory_linker.run_linker_pass(
            None, "m",
            judge=lambda pairs: [{"a": pairs[0]["a"]["unit_id"],
                                  "b": pairs[0]["b"]["unit_id"],
                                  "relation": "same_fact"}],
        )
        self.assertEqual(retry.linked, 1)

    def test_pass_skips_already_linked_pairs_and_advances_cursor(self) -> None:
        self._public_unit("喜欢安静的咖啡馆")
        self._private_unit("喜欢安静的咖啡馆")

        def judge(pairs):
            return [{"a": p["a"]["unit_id"], "b": p["b"]["unit_id"], "relation": "same_fact"}
                    for p in pairs]

        first = memory_linker.run_linker_pass(None, "m", judge=judge)
        self.assertEqual(first.linked, 1)
        second = memory_linker.run_linker_pass(None, "m", judge=judge)
        self.assertEqual(second.scanned, 0)  # cursor advanced, nothing re-scanned

    def test_layer_label_hides_soul_name_from_judge(self) -> None:
        self._public_unit("喜欢安静的咖啡馆")
        self._private_unit("喜欢安静的咖啡馆", soul="gotoh")
        captured: dict = {}

        def judge(pairs):
            captured["pairs"] = pairs
            return []

        memory_linker.run_linker_pass(None, "m", judge=judge)
        layers = {p["a"]["layer"] for p in captured["pairs"]} | {
            p["b"]["layer"] for p in captured["pairs"]
        }
        self.assertEqual(layers, {"公开", "私聊"})
        self.assertNotIn("gotoh", str(captured["pairs"]))

    # --- link maintenance (回访闭环) ------------------------------------------

    def _contested_pair(self) -> tuple[str, str]:
        public = self._public_unit("在准备考研")
        private = self._private_unit("已经放弃考研了")
        mus.add_unit_link(public, private, "contradicts")
        mus.mark_contested(public)
        return public, private

    def test_dead_end_drops_link_and_clears_contested(self) -> None:
        public, private = self._contested_pair()
        mus.retract_unit(private, by="user", reason="outdated")
        handled = memory_linker.maintain_links(lambda payload: [])
        self.assertEqual(handled, 1)
        self.assertFalse(mus.linked_pair_exists(public, private))
        self.assertIsNone(mus.get_unit(public)["contested_at"])

    def test_revisit_answer_dissolves_contradiction(self) -> None:
        # the user's answer flowed back: reconcile revised the private unit,
        # bumping last_confirmed past the link; the re-judge downgrades the pair
        public, private = self._contested_pair()
        self._seq += 1
        with db.transaction() as conn:
            ev = mes.record_chat_mutation(
                conn, message_id=self._seq, soul_name="gotoh", op="create",
                content="没有啦我还在考", occurred_at=float(self._seq), role="user",
            ).id
        mus.revise_unit(private, content="用户仍在考研", evidence_event_ids=[ev])

        def judge(payload):
            return [{"a": p["a"]["unit_id"], "b": p["b"]["unit_id"], "relation": "same_fact"}
                    for p in payload]

        handled = memory_linker.maintain_links(judge)
        self.assertEqual(handled, 1)
        links = mus.links_for_units([public, private])
        self.assertEqual("same_fact", links[0]["relation"])
        self.assertIsNone(mus.get_unit(public)["contested_at"])

    def test_standing_contradiction_remarks_after_fresh_public_evidence(self) -> None:
        # fresh public evidence clears the mark optimistically; if the private
        # side still contradicts, the re-judge honestly re-marks it
        public, private = self._contested_pair()
        del private
        self._seq += 1
        with db.transaction() as conn:
            ev = mes.record_post_mutation(
                conn, post_id=f"p{self._seq}", op="create", content="考研冲刺中",
                occurred_at=float(self._seq),
            ).id
        mus.confirm_unit(public, evidence_event_ids=[ev])
        self.assertIsNone(mus.get_unit(public)["contested_at"])  # cleared by confirm

        def judge(payload):
            return [{"a": p["a"]["unit_id"], "b": p["b"]["unit_id"], "relation": "contradicts"}
                    for p in payload]

        memory_linker.maintain_links(judge)
        self.assertTrue(mus.get_unit(public)["contested_at"])

    # --- contested lifecycle -------------------------------------------------

    def test_confirm_with_fresh_evidence_clears_contested(self) -> None:
        public = self._public_unit("在准备考研")
        mus.mark_contested(public)
        self.assertTrue(mus.get_unit(public)["contested_at"])
        self._seq += 1
        with db.transaction() as conn:
            ev = mes.record_post_mutation(
                conn, post_id=f"p{self._seq}", op="create", content="考研冲刺中",
                occurred_at=float(self._seq),
            ).id
        mus.confirm_unit(public, evidence_event_ids=[ev])
        self.assertIsNone(mus.get_unit(public)["contested_at"])

    def test_clear_contested_restores_portrait_eligibility(self) -> None:
        public = self._public_unit("在准备考研", tier="core", importance=0.8, confidence=0.9)
        from core import memory_view_service as mvs
        mus.confirm_unit(public)  # hysteresis seed
        self.assertIn(public, mvs.recompute_portrait_membership("global", "public"))
        mus.mark_contested(public)
        self.assertEqual(mus.get_unit(public)["in_portrait"], 0)
        mus.clear_contested(public)
        self.assertEqual(mus.get_unit(public)["in_portrait"], 1)


if __name__ == "__main__":
    unittest.main()
