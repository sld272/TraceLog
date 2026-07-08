from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_unit_service as mus,
    memory_view_service as mvs,
    soul_relationship_memory as srm,
)
from core.llm import reply_router
from core.soul_service import SoulContext


class SoulRelationshipMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self.seq = 0

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _event(self, soul: str, visibility: str, content: str) -> int:
        # relationship memory is now sourced only from private 1:1 chat
        self.seq += 1
        with db.transaction() as conn:
            return mes.record_chat_mutation(
                conn,
                message_id=self.seq,
                soul_name=soul,
                role="user",
                op="create",
                content=content,
                occurred_at=float(self.seq),
            ).id

    def _unit(
        self,
        soul: str,
        visibility: str,
        content: str,
        *,
        type: str = "relationship",
        tier: str = "core",
        confidence: float = 0.9,
        importance: float = 0.9,
    ) -> str:
        event_id = self._event(soul, visibility, content)
        return mus.add_unit(
            owner_scope=f"soul:{soul}",
            visibility_scope=visibility,
            source_channel="chat",
            type=type,
            content=content,
            confidence=confidence,
            tier=tier,
            importance=importance,
            evidence_event_ids=[event_id],
        )

    def test_selects_relationship_units_from_private_bucket(self) -> None:
        rel = self._unit("luna", "private:soul:luna", "双方习惯称呼彼此为老友")
        self._unit("luna", "private:soul:luna", "用户喜欢爵士乐", type="preference")
        self._unit("nova", "private:soul:nova", "nova 的私聊关系")

        selected = srm.relationship_units_for_soul("luna")
        self.assertEqual({row["id"] for row in selected}, {rel})

    def test_contextual_relationship_units_enter_narrative(self) -> None:
        # The portrait's triple bar (tier=core ∧ conf>=0.82 ∧ imp>=0.70) would
        # exclude both; the relationship predicate admits soft texture but still
        # floors out near-zero confidence.
        soft = self._unit(
            "luna", "private:soul:luna", "用户喜欢被轻轻调侃",
            tier="contextual", confidence=0.6, importance=0.4,
        )
        low_conf = self._unit(
            "luna", "private:soul:luna", "也许用户讨厌早起",
            tier="contextual", confidence=0.3, importance=0.4,
        )
        selected = {row["id"] for row in srm.relationship_units_for_soul("luna")}
        self.assertIn(soft, selected)
        self.assertNotIn(low_conf, selected)

    def test_no_prompt_relationship_unit_excluded(self) -> None:
        muted = self._unit(
            "luna", "private:soul:luna", "用户的某个小习惯",
            tier="contextual", confidence=0.7, importance=0.5,
        )
        mus.set_prompt_policy(muted, prompt_policy="no_prompt")
        selected = {row["id"] for row in srm.relationship_units_for_soul("luna")}
        self.assertNotIn(muted, selected)

    def test_narrative_count_capped(self) -> None:
        for i in range(srm.REL_MAX_UNITS + 3):
            self._unit(
                "luna", "private:soul:luna", f"相处默契 {i}",
                tier="contextual", confidence=0.7, importance=0.5,
            )
        self.assertEqual(
            len(srm.relationship_units_for_soul("luna")), srm.REL_MAX_UNITS
        )

    def test_public_relationship_unit_enters_persona_memory(self) -> None:
        # route A: a relationship belief formed from public comments
        # (visibility=public) is part of the persona's relationship memory
        # alongside private-chat ones; souls_needing_view picks the soul up.
        with db.transaction() as conn:
            mes.record_comment_mutation(
                conn, comment_id=1, post_id="p1", soul_name="luna",
                role="user", op="create", content="老地方见", occurred_at=1.0,
            )
        rel_event = db.query_one(
            "SELECT id FROM memory_ingest_events "
            "WHERE source_type='comment_relationship' AND source_id='1'"
        )["id"]
        public_rel = mus.add_unit(
            owner_scope="soul:luna", visibility_scope="public",
            source_channel="comment", type="relationship",
            content="用户和 luna 把评论区叫老地方",
            confidence=0.7, tier="contextual", importance=0.5,
            evidence_event_ids=[rel_event],
        )
        private_rel = self._unit(
            "luna", "private:soul:luna", "私下会互道晚安",
            tier="contextual", confidence=0.7, importance=0.5,
        )
        selected = {row["id"] for row in srm.relationship_units_for_soul("luna")}
        self.assertEqual(selected, {public_rel, private_rel})
        self.assertIn("luna", srm.souls_needing_view())

    def test_refresh_persists_relationship_view_and_reads_body(self) -> None:
        self._unit("luna", "private:soul:luna", "平时可以轻微互损")
        self._unit("luna", "private:soul:luna", "用户低落时希望先安静陪伴")

        view = srm.refresh_relationship_memory(
            "luna",
            synthesizer=lambda units, budget: "我们熟悉彼此的节奏：平时可以轻微互损，低落时先安静陪伴。",
        )
        self.assertEqual(view.view_type, mvs.VIEW_SOUL_RELATIONSHIP)
        self.assertEqual(view.visibility_scope, srm.VIEW_VISIBILITY)
        self.assertEqual(
            srm.read_relationship_memory("luna"),
            "我们熟悉彼此的节奏：平时可以轻微互损，低落时先安静陪伴。",
        )

    def test_relation_unit_edit_and_retract_mark_view_stale(self) -> None:
        unit_id = self._unit("luna", "private:soul:luna", "用户喜欢直球提醒")
        srm.refresh_relationship_memory("luna")
        ref = srm.view_ref("luna")
        self.assertEqual(
            mvs.get_view(ref.owner_scope, ref.visibility_scope, ref.view_type)["status"],
            "fresh",
        )

        mus.update_unit(unit_id, content="用户喜欢直球提醒，但难过时要收住")
        self.assertEqual(
            mvs.get_view(ref.owner_scope, ref.visibility_scope, ref.view_type)["status"],
            "stale",
        )
        srm.refresh_relationship_memory("luna")
        mus.retract_unit(unit_id, by="user", reason="outdated")
        self.assertEqual(
            mvs.get_view(ref.owner_scope, ref.visibility_scope, ref.view_type)["status"],
            "stale",
        )
        self.assertEqual(srm.read_relationship_memory("luna"), "")

    def test_reply_uses_relationship_view_in_public_and_private(self) -> None:
        self._unit("luna", "private:soul:luna", "用户允许 luna 用老友称呼")
        srm.refresh_relationship_memory(
            "luna",
            synthesizer=lambda units, budget: "我们会自然地称呼彼此为老友。",
        )
        soul = SoulContext(
            name="luna",
            description=None,
            sort_order=0,
            soul="你是 luna。",
        )
        public = reply_router._relationship_memory(
            soul, channel="public_post", query="你好"
        )
        private = reply_router._relationship_memory(
            soul, channel="chat", query="你好"
        )
        self.assertIn("老友", public)
        self.assertIn("拿不准时不要主动公开", public)
        self.assertEqual(public, private)


if __name__ == "__main__":
    unittest.main()
