from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_unit_service as mus,
)


class BoundaryTest(unittest.TestCase):
    """Regression guard for "人格不串戏": public memory is shared across souls,
    each soul's private memory is isolated, and own private surfaced in a public
    scene is discretion-flagged."""

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

    def _add(self, owner: str, vis: str, channel: str, ev: int, content: str) -> str:
        return mus.add_unit(
            owner_scope=owner, visibility_scope=vis, source_channel=channel,
            type="insight", content=content, confidence=0.7, tier="contextual",
            importance=0.6, evidence_event_ids=[ev],
        )

    def _post_unit(self, content: str) -> None:
        with db.transaction() as conn:
            ev = mes.record_post_mutation(conn, post_id="p1", op="create", content=content, occurred_at=1.0).id
        self._add(mes.GLOBAL_SCOPE, mes.PUBLIC_VISIBILITY, "post", ev, content)

    def _comment_unit(self, soul: str, content: str) -> None:
        # public-post comments now land in global/public (shared across souls)
        with db.transaction() as conn:
            ev = mes.record_comment_mutation(
                conn, comment_id=1, post_id="p1", soul_name=soul, role="user",
                op="create", content=content, occurred_at=1.0,
            ).id
        self._add(mes.GLOBAL_SCOPE, mes.PUBLIC_VISIBILITY, "comment", ev, content)

    def _chat_unit(self, soul: str, content: str) -> None:
        with db.transaction() as conn:
            ev = mes.record_chat_mutation(
                conn, message_id=1, soul_name=soul, op="create", content=content,
                occurred_at=1.0, role="user",
            ).id
        self._add(mes.soul_scope(soul), mes.private_visibility(soul), "chat", ev, content)

    def test_public_unit_readable_cross_soul(self) -> None:
        self._post_unit("用户喜欢爵士乐")
        hits = memory_read.retrieve_units("爵士乐", "public_post", "毒舌好友")
        self.assertTrue(any("爵士乐" in h.content for h in hits))

    def test_other_soul_thread_readable_in_comment(self) -> None:
        self._comment_unit("luna", "用户喜欢直球反馈")
        hits = memory_read.retrieve_units("直球", "comment", "毒舌好友")
        self.assertTrue(any("直球" in h.content for h in hits))

    def test_other_soul_private_not_readable(self) -> None:
        self._chat_unit("luna", "用户的秘密保密事项")
        # another soul cannot read luna's private chat memory
        self.assertEqual(memory_read.retrieve_units("保密", "chat", "毒舌好友"), [])
        # luna itself can
        hits_self = memory_read.retrieve_units("保密", "chat", "luna")
        self.assertTrue(any("保密" in h.content for h in hits_self))

    def test_own_private_in_public_needs_discretion(self) -> None:
        self._chat_unit("luna", "用户私下保密的事")
        hits = memory_read.retrieve_units("保密", "public_post", "luna")
        self.assertTrue(hits)
        self.assertTrue(all(h.needs_discretion for h in hits))


if __name__ == "__main__":
    unittest.main()
