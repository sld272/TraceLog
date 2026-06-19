from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_unit_service as mus,
)
from core.llm import reply_router


class RelationshipMemorySourceTest(unittest.TestCase):
    """In v2 read mode the relationship block must come from units/views, never
    from the legacy whole-file soul_memory (which leaked private chat publicly)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self.soul = SimpleNamespace(name="温柔树洞", soul="人格设定", soul_memory="LEGACY相处记忆")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_legacy_mode_uses_soul_memory(self) -> None:
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "legacy"}):
            out = reply_router._relationship_memory(self.soul, channel="public_post", query="q")
        self.assertEqual(out, "LEGACY相处记忆")

    def test_v2_mode_never_injects_legacy_soul_memory(self) -> None:
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "units"}):
            out = reply_router._relationship_memory(self.soul, channel="public_post", query="q")
        self.assertNotIn("LEGACY相处记忆", out)  # legacy block must not leak in v2


class AttributionTest(unittest.TestCase):
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

    def test_comment_section_attributes_other_souls_thread(self) -> None:
        owner = mes.soul_scope("毒舌好友")
        vis = mes.thread_visibility("p1")
        with db.transaction() as conn:
            ev = mes.record_comment_mutation(
                conn, comment_id=1, post_id="p1", soul_name="毒舌好友", role="user",
                op="create", content="用户说喜欢直球", occurred_at=1.0,
            ).id
        mus.add_unit(
            owner_scope=owner, visibility_scope=vis, source_channel="comment",
            type="insight", content="用户喜欢直球反馈", confidence=0.7, tier="contextual",
            importance=0.6, evidence_event_ids=[ev],
        )
        # another soul replying in a comment can read it AND see whose thread it is
        section = memory_read.build_memory_section("comment", "温柔树洞", "直球反馈").text
        self.assertIn("用户喜欢直球反馈", section)
        self.assertIn("用户在 毒舌好友 的评论区", section)


class RetrieveGateTest(unittest.TestCase):
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

    def test_zero_overlap_units_are_gated_out(self) -> None:
        with db.transaction() as conn:
            ev = mes.record_post_mutation(conn, post_id="p1", op="create", content="弹吉他", occurred_at=1.0).id
        mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="insight", content="用户喜欢弹吉他", confidence=0.7, tier="contextual",
            importance=0.6, evidence_event_ids=[ev],
        )
        # query overlaps -> returned
        self.assertEqual(len(memory_read.retrieve_units("吉他", "public_post", None)), 1)
        # query shares nothing -> gated out instead of importance-ranked filler
        self.assertEqual(memory_read.retrieve_units("量子物理", "public_post", None), [])


class UnitDetailTest(unittest.TestCase):
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

    def test_unit_detail_hydrates_evidence(self) -> None:
        with db.transaction() as conn:
            e1 = mes.record_post_mutation(conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0).id
        uid = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="goal", content="用户在准备考研", confidence=0.9, tier="core",
            importance=0.85, evidence_event_ids=[e1],
        )
        detail = memory_read.unit_detail(uid)
        self.assertEqual(detail.content, "用户在准备考研")
        self.assertEqual(len(detail.evidence), 1)
        self.assertEqual(detail.evidence[0].event_id, e1)
        self.assertEqual(detail.evidence[0].content, "我在准备考研")

    def test_unit_detail_missing_returns_none(self) -> None:
        self.assertIsNone(memory_read.unit_detail("nope"))


class LastUserTextTest(unittest.TestCase):
    def test_returns_last_user_message(self) -> None:
        msgs = [
            SimpleNamespace(role="user", content="A"),
            SimpleNamespace(role="assistant", content="B"),
            SimpleNamespace(role="user", content="C"),
        ]
        self.assertEqual(reply_router._last_user_text(msgs), "C")

    def test_empty_messages(self) -> None:
        self.assertEqual(reply_router._last_user_text([]), "")


if __name__ == "__main__":
    unittest.main()
