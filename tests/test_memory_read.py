from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    goal_service,
    memory_events_service as mes,
    memory_read,
    memory_unit_service as mus,
    memory_view_service as mvs,
)


class MemoryReadTest(unittest.TestCase):
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

    def _ev(self, owner, vis, kind="post") -> int:
        self._seq += 1
        with db.transaction() as conn:
            if kind == "post":
                return mes.record_post_mutation(conn, post_id=f"p{self._seq}", op="create", content="e", occurred_at=float(self._seq)).id
            return mes.record_chat_mutation(conn, message_id=self._seq, soul_name=owner.split(":")[1], op="create", content="e", occurred_at=float(self._seq), role="user").id

    def _unit(self, owner, vis, *, type="insight", content="x", importance=0.5, last_confirmed=None, in_portrait=0):
        ev = self._ev(owner, vis, "post" if vis == "public" or vis.startswith("thread") else "chat")
        uid = mus.add_unit(
            owner_scope=owner, visibility_scope=vis, source_channel="post" if vis != "private:soul:" else "chat",
            type=type, content=content, confidence=0.8, importance=importance, evidence_event_ids=[ev],
        )
        sets = []
        params = []
        if last_confirmed is not None:
            sets.append("last_confirmed = ?"); params.append(last_confirmed)
        if in_portrait:
            sets.append("in_portrait = 1")
        if sets:
            with db.transaction() as conn:
                conn.execute(f"UPDATE memory_units SET {', '.join(sets)} WHERE id=?", (*params, uid))
        return uid

    # --- current-state block ----------------------------------------------

    def test_state_block_returns_recent_states_within_window(self) -> None:
        now = db.now_ts()
        fresh = self._unit("global", "public", type="state", content="这周很累", importance=0.5, last_confirmed=now)
        old = self._unit("global", "public", type="state", content="上个月忙", importance=0.5, last_confirmed=now - 30 * 86400)
        block = memory_read.recent_state_block("public_post", "gotoh", now=now)
        ids = [i.unit_id for i in block]
        self.assertIn(fresh, ids)
        self.assertNotIn(old, ids)  # outside 7-day window

    def test_state_block_capped_and_ranked(self) -> None:
        now = db.now_ts()
        for i in range(8):
            self._unit("global", "public", type="state", content=f"状态{i}", importance=0.4 + i * 0.05, last_confirmed=now)
        block = memory_read.recent_state_block("public_post", "gotoh", now=now)
        self.assertLessEqual(len(block), memory_read.STATE_BLOCK_LIMIT)
        # highest importance first
        self.assertEqual(block[0].content, "状态7")

    def test_state_block_excludes_other_souls_private(self) -> None:
        now = db.now_ts()
        self._unit("soul:kita", "private:soul:kita", type="state", content="kita私聊状态", importance=0.6, last_confirmed=now)
        block = memory_read.recent_state_block("public_post", "gotoh", now=now)
        self.assertEqual(block, [])  # gotoh cannot see kita's private state

    def test_state_block_own_private_flagged_discretion_in_public(self) -> None:
        now = db.now_ts()
        self._unit("soul:gotoh", "private:soul:gotoh", type="state", content="只跟gotoh说的状态", importance=0.6, last_confirmed=now)
        pub = memory_read.recent_state_block("public_post", "gotoh", now=now)
        self.assertEqual(len(pub), 1)
        self.assertTrue(pub[0].needs_discretion)
        prv = memory_read.recent_state_block("chat", "gotoh", now=now)
        self.assertFalse(prv[0].needs_discretion)

    # --- retrieve_units ----------------------------------------------------

    def test_retrieve_matches_keywords_and_excludes_state_and_portrait(self) -> None:
        self._unit("global", "public", type="preference", content="喜欢安静的咖啡馆看书", importance=0.5)
        self._unit("global", "public", type="preference", content="讨厌早起", importance=0.5)
        self._unit("global", "public", type="state", content="咖啡喝多了睡不着", importance=0.5)  # state excluded
        self._unit("global", "public", type="identity", content="咖啡相关核心身份", importance=0.8, in_portrait=1)  # portrait excluded
        hits = memory_read.retrieve_units("咖啡馆 看书", "public_post", "gotoh")
        contents = [h.content for h in hits]
        self.assertIn("喜欢安静的咖啡馆看书", contents)
        self.assertNotIn("咖啡喝多了睡不着", contents)
        self.assertNotIn("咖啡相关核心身份", contents)
        # best match ranked first
        self.assertEqual(hits[0].content, "喜欢安静的咖啡馆看书")

    def test_retrieve_public_sees_other_souls_public_memory(self) -> None:
        # user's public comment-conversation belief lands in global/public, shared across souls
        with db.transaction() as conn:
            ev = mes.record_comment_mutation(
                conn, comment_id=901, post_id="20260616-001", soul_name="kita",
                role="user", op="create", content="我自学吉他", occurred_at=1.0,
            ).id
        uid = mus.add_unit(
            owner_scope=mes.GLOBAL_SCOPE, visibility_scope=mes.PUBLIC_VISIBILITY, source_channel="comment",
            type="preference", content="用户喜欢弹吉他自学", importance=0.5, evidence_event_ids=[ev],
        )
        hits = memory_read.retrieve_units("弹吉他", "public_post", "gotoh")
        self.assertIn(uid, [h.unit_id for h in hits])  # gotoh can retrieve user's public convo with kita

    def test_retrieve_excludes_other_souls_private(self) -> None:
        ev = self._ev("soul:kita", "private:soul:kita", "chat")
        mus.add_unit(
            owner_scope="soul:kita", visibility_scope="private:soul:kita", source_channel="chat",
            type="insight", content="只对kita私聊的事", importance=0.6, evidence_event_ids=[ev],
        )
        hits = memory_read.retrieve_units("私聊", "public_post", "gotoh")
        self.assertEqual(hits, [])

    def test_retrieve_own_private_flagged_in_public(self) -> None:
        ev = self._ev("soul:gotoh", "private:soul:gotoh", "chat")
        uid = mus.add_unit(
            owner_scope="soul:gotoh", visibility_scope="private:soul:gotoh", source_channel="chat",
            type="insight", content="用户私下告诉gotoh的秘密爱好", importance=0.6, evidence_event_ids=[ev],
        )
        hits = memory_read.retrieve_units("爱好", "public_post", "gotoh")
        match = [h for h in hits if h.unit_id == uid]
        self.assertEqual(len(match), 1)
        self.assertTrue(match[0].needs_discretion)


    # --- build_memory_section ---------------------------------------------

    def test_build_section_assembles_layers(self) -> None:
        now = db.now_ts()
        # portrait
        core = self._unit("global", "public", type="identity", content="南大法语生，自学计算机", importance=0.8)
        mus.confirm_unit(core, evidence_event_ids=[self._ev("global", "public")], confidence=0.9)
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET tier='core', confidence=0.9 WHERE id=?", (core,))
        mvs.synthesize_view("global", "public", mvs.VIEW_USER_PORTRAIT)
        # state + relevant
        self._unit("global", "public", type="state", content="这周备考很累", importance=0.5, last_confirmed=now)
        self._unit("global", "public", type="preference", content="喜欢安静咖啡馆", importance=0.5)

        prompt = memory_read.build_memory_section("public_post", "gotoh", "咖啡馆")
        self.assertIn("[基线认知]", prompt.text)
        self.assertIn("[当前状态]", prompt.text)
        self.assertIn("这周备考很累", prompt.text)
        self.assertIn("[相关记忆]", prompt.text)
        self.assertIn("咖啡馆", prompt.text)
        self.assertIn("[记忆使用规则]", prompt.text)
        self.assertFalse(prompt.has_discretion_items)

    def test_build_section_marks_discretion_and_adds_rule(self) -> None:
        now = db.now_ts()
        ev = self._ev("soul:gotoh", "private:soul:gotoh", "chat")
        mus.add_unit(
            owner_scope="soul:gotoh", visibility_scope="private:soul:gotoh", source_channel="chat",
            type="insight", content="用户私下说的烦心事", importance=0.6, evidence_event_ids=[ev],
        )
        prompt = memory_read.build_memory_section("public_post", "gotoh", "烦心事")
        self.assertIn(memory_read._DISCRETION_TAG, prompt.text)
        self.assertTrue(prompt.has_discretion_items)
        self.assertIn("默认不要主动透露", prompt.text)

    def test_build_section_empty_when_no_memory(self) -> None:
        prompt = memory_read.build_memory_section("public_post", "gotoh", "随便")
        self.assertEqual(prompt.text, "")
        self.assertEqual(prompt.used_unit_ids, [])

    def test_relevant_memory_deduplicates_active_goal_topic(self) -> None:
        goal_service.create_goal("跨专业考研", None, "long")
        self._unit(
            "global",
            "public",
            type="preference",
            content="用户想跨专业考研",
            importance=0.6,
        )
        hits = memory_read.retrieve_units("跨专业考研", "public_post", "gotoh")
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()
