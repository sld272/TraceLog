from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_revisit,
    memory_unit_service as mus,
)

DAY = 86400.0


class MemoryRevisitTest(unittest.TestCase):
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

    def _contested_pair(self, soul: str = "gotoh") -> tuple[str, str]:
        """A contested public belief + the contradicting private one, linked."""
        self._seq += 1
        with db.transaction() as conn:
            pub_ev = mes.record_post_mutation(
                conn, post_id=f"p{self._seq}", op="create", content="在准备考研",
                occurred_at=float(self._seq),
            ).id
            priv_ev = mes.record_chat_mutation(
                conn, message_id=self._seq, soul_name=soul, op="create",
                content="其实已经放弃考研了", occurred_at=float(self._seq), role="user",
            ).id
        public = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="用户在准备考研", evidence_event_ids=[pub_ev],
        )
        private = mus.add_unit(
            owner_scope=f"soul:{soul}", visibility_scope=f"private:soul:{soul}",
            source_channel="chat", type="state", content="用户已放弃考研",
            evidence_event_ids=[priv_ev],
        )
        mus.add_unit_link(public, private, "contradicts")
        mus.mark_contested(public)
        return public, private

    # --- gating --------------------------------------------------------------

    def test_directive_only_in_private_chat(self) -> None:
        public, private = self._contested_pair()
        self.assertEqual("", memory_revisit.revisit_directive("public_post", "gotoh", [public]))
        self.assertEqual("", memory_revisit.revisit_directive("chat", None, [public]))
        self.assertIn("[回访]", memory_revisit.revisit_directive("chat", "gotoh", [private]))

    def test_directive_needs_topic_relevance(self) -> None:
        self._contested_pair()
        self.assertEqual("", memory_revisit.revisit_directive("chat", "gotoh", []))
        self.assertEqual("", memory_revisit.revisit_directive("chat", "gotoh", ["mu_other"]))

    def test_other_soul_never_revisits(self) -> None:
        public, _ = self._contested_pair(soul="gotoh")
        self.assertEqual("", memory_revisit.revisit_directive("chat", "kita", [public]))

    def test_optout_switch_silences_revisit(self) -> None:
        public, _ = self._contested_pair()
        memory_revisit.set_revisit_enabled(False)
        self.assertEqual("", memory_revisit.revisit_directive("chat", "gotoh", [public]))
        memory_revisit.set_revisit_enabled(True)
        self.assertIn("[回访]", memory_revisit.revisit_directive("chat", "gotoh", [public]))

    # --- ladder & rate limit ---------------------------------------------------

    def test_ladder_rung1_then_rate_limit_then_rung2(self) -> None:
        public, _ = self._contested_pair()
        now = db.now_ts()
        first = memory_revisit.revisit_directive("chat", "gotoh", [public], now=now)
        # rung 1: a natural status question, no mention of any difference
        self.assertIn("自然的关心", first)
        self.assertNotIn("场合", first)
        # within the window: silence, not nagging
        self.assertEqual(
            "", memory_revisit.revisit_directive("chat", "gotoh", [public], now=now + DAY)
        )
        # after the window: rung 2 — gentle verification with reply directions,
        # and the anti-confrontation iron rule spelled out
        second = memory_revisit.revisit_directive(
            "chat", "gotoh", [public], now=now + 4 * DAY
        )
        self.assertIn("台阶", second)
        self.assertIn("场合不同", second)
        self.assertIn("不许出现", second)

    def test_directive_rides_private_chat_memory_section(self) -> None:
        public, private = self._contested_pair()
        del public
        prompt = memory_read.build_memory_section("chat", "gotoh", "考研")
        self.assertIn("[回访]", prompt.text)
        self.assertIn(private, prompt.used_unit_ids)

    def test_public_scene_memory_section_never_carries_revisit(self) -> None:
        self._contested_pair()
        prompt = memory_read.build_memory_section("public_post", "gotoh", "考研")
        self.assertNotIn("[回访]", prompt.text)


if __name__ == "__main__":
    unittest.main()
