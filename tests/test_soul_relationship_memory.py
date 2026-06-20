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
        self.seq += 1
        with db.transaction() as conn:
            if visibility.startswith("thread:"):
                return mes.record_comment_mutation(
                    conn,
                    comment_id=self.seq,
                    post_id=visibility[len("thread:"):],
                    soul_name=soul,
                    role="user",
                    op="create",
                    content=content,
                    occurred_at=float(self.seq),
                ).id
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
            source_channel="comment" if visibility.startswith("thread:") else "chat",
            type=type,
            content=content,
            confidence=confidence,
            tier=tier,
            importance=importance,
            evidence_event_ids=[event_id],
        )

    def test_selects_relationship_units_across_own_thread_and_private_only(self) -> None:
        thread = self._unit("luna", "thread:p1", "用户希望 luna 先共情再建议")
        private = self._unit("luna", "private:soul:luna", "双方习惯称呼彼此为老友")
        self._unit("luna", "thread:p2", "用户喜欢爵士乐", type="preference")
        self._unit("nova", "private:soul:nova", "nova 的私聊关系")

        selected = srm.relationship_units_for_soul("luna")
        self.assertEqual({row["id"] for row in selected}, {thread, private})

    def test_refresh_persists_one_cross_bucket_view_and_reads_body(self) -> None:
        self._unit("luna", "thread:p1", "评论区里双方习惯轻微互损")
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
        unit_id = self._unit("luna", "thread:p1", "用户喜欢直球提醒")
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

    def test_v2_reply_uses_relationship_view_in_public_and_private(self) -> None:
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
            soul_memory="LEGACY SHOULD NOT APPEAR",
        )
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "units"}):
            public = reply_router._relationship_memory(
                soul, channel="public_post", query="你好"
            )
            private = reply_router._relationship_memory(
                soul, channel="chat", query="你好"
            )
        self.assertIn("老友", public)
        self.assertIn("拿不准时不要主动公开", public)
        self.assertEqual(public, private)
        self.assertNotIn("LEGACY", public)


if __name__ == "__main__":
    unittest.main()
