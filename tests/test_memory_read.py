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

    # --- cited units (引用记忆) -------------------------------------------

    def test_cited_units_hydrates_dedupes_and_drops_missing(self) -> None:
        u1 = self._unit("global", "public", type="preference", content="喜欢安静")
        u2 = self._unit("global", "public", type="state", content="最近很累")
        items = memory_read.cited_units([u1, u2, u1, "mu_missing"])
        self.assertEqual([i["unit_id"] for i in items], [u1, u2])
        self.assertEqual(items[0]["kind"], "unit")
        self.assertEqual(items[0]["type"], "preference")
        self.assertEqual(items[0]["content"], "喜欢安静")
        self.assertIn("confidence", items[0])

    def test_cited_units_drops_inactive(self) -> None:
        u = self._unit("global", "public", type="preference", content="会被撤回")
        mus.retract_unit(u, by="user")
        self.assertEqual(memory_read.cited_units([u]), [])

    def test_cited_memory_includes_units_and_freshness(self) -> None:
        # a reply can lean on raw freshness evidence that isn't yet a unit; it must
        # show up in 引用记忆 alongside the belief units (kind='fresh')
        u = self._unit("global", "public", type="preference", content="喜欢安静")
        fresh = memory_read.FreshnessItem(
            content="刚提到在搬家", source_channel="post", occurred_at=0.0,
            owner_scope="global", visibility_scope="public", needs_discretion=False,
        )
        items = memory_read.cited_memory([u], [fresh])
        self.assertEqual(items[0]["kind"], "unit")
        self.assertEqual(items[0]["content"], "喜欢安静")
        self.assertEqual(
            items[1], {"kind": "fresh", "content": "刚提到在搬家", "channel": "post"}
        )
        meta = memory_read.cited_memory_metadata_from(items)
        self.assertEqual(meta, {"version": 1, "items": items})

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

    # --- recall around comment-derived units (相关对话原文) ----------------

    def test_recall_resolves_comment_unit_to_its_post_and_thread(self) -> None:
        # A belief distilled from a public comment: its evidence source_id is the
        # comment id, NOT the post id. Recall must resolve comment -> post/soul via
        # the comments table, then surface the original post and the comment thread.
        now = 1000.0
        db.execute(
            "INSERT INTO souls(name, file_path, created_at, updated_at) VALUES(?, ?, ?, ?)",
            ("kita", "souls/kita.md", now, now),
        )
        db.execute(
            "INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            ("20260616-001", "2026-06-16", "今天终于把吉他练到能弹完一整首", now, now),
        )
        db.execute(
            "INSERT INTO comments(id, post_id, soul_name, role, content, seq, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (901, "20260616-001", "kita", "user", "我自学吉他三个月了", 0, now),
        )
        db.execute(
            "INSERT INTO comments(id, post_id, soul_name, role, content, seq, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (902, "20260616-001", "kita", "assistant", "三个月能弹完整首很厉害", 1, now),
        )
        with db.transaction() as conn:
            mes.record_post_mutation(
                conn, post_id="20260616-001", op="create",
                content="今天终于把吉他练到能弹完一整首", occurred_at=now,
            )
            ev = mes.record_comment_mutation(
                conn, comment_id=901, post_id="20260616-001", soul_name="kita",
                role="user", op="create", content="我自学吉他三个月了", occurred_at=now,
            )
        uid = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="comment",
            type="preference", content="用户在自学吉他", confidence=0.8, importance=0.5,
            evidence_event_ids=[ev.id],
        )
        hit = memory_read.MemoryItem(
            unit_id=uid, type="preference", content="用户在自学吉他",
            confidence=0.8, importance=0.5, owner_scope="global",
            visibility_scope="public", needs_discretion=False,
        )

        recall = memory_read._recall_conversations([hit], "public_post", "kita", ["吉他"])

        self.assertIn("[相关对话原文]", recall)
        self.assertIn("今天终于把吉他练到能弹完一整首", recall)  # original post
        self.assertIn("与kita", recall)
        self.assertIn("我自学吉他三个月了", recall)  # user's comment
        self.assertIn("三个月能弹完整首很厉害", recall)  # soul's reply

    # --- provenance attribution keyed on source_type (post vs comment) -------

    def test_relevant_memory_attributes_comment_unit_to_its_soul_area(self) -> None:
        # Comment user-facts and posts both live in (…, public) after bucketing, so
        # attribution must key on source_type, not visibility. A belief from kita's
        # comment area, surfaced for luna, must read "用户在 kita 的评论区" — not the
        # post label — so luna never reads it as said to herself. Old occurred_at
        # keeps these out of the freshness seam, isolating the [相关记忆] tags.
        old = 1000.0
        db.execute(
            "INSERT INTO souls(name, file_path, created_at, updated_at) VALUES(?, ?, ?, ?)",
            ("kita", "souls/kita.md", old, old),
        )
        db.execute(
            "INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            ("p-comment", "2026-06-16", "练吉他", old, old),
        )
        db.execute(
            "INSERT INTO comments(id, post_id, soul_name, role, content, seq, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (901, "p-comment", "kita", "user", "我自学吉他三个月了", 0, old),
        )
        with db.transaction() as conn:
            cev = mes.record_comment_mutation(
                conn, comment_id=901, post_id="p-comment", soul_name="kita",
                role="user", op="create", content="我自学吉他三个月了", occurred_at=old,
            )
            pev = mes.record_post_mutation(
                conn, post_id="p-post", op="create", content="想换把新吉他", occurred_at=old,
            )
        mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="comment",
            type="preference", content="用户在自学吉他", confidence=0.8, importance=0.5,
            evidence_event_ids=[cev.id],
        )
        mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content="用户想换新吉他", confidence=0.8, importance=0.5,
            evidence_event_ids=[pev.id],
        )

        text = memory_read.build_memory_section("public_post", "luna", "吉他").text

        self.assertIn("用户在自学吉他 （用户在 kita 的评论区）", text)
        self.assertIn("用户想换新吉他 （公开帖子）", text)

    def test_freshness_attributes_cross_soul_comment_line(self) -> None:
        # The higher-severity case: a raw comment line the user said in kita's area
        # surfaces verbatim in luna's freshness (the comment_message lens is global/
        # public, cross-soul readable). Without source-keyed attribution it would be
        # mislabeled "公开帖子"; luna could read "你说得对" as a public broadcast.
        now = db.now_ts()
        db.execute(
            "INSERT INTO souls(name, file_path, created_at, updated_at) VALUES(?, ?, ?, ?)",
            ("kita", "souls/kita.md", now, now),
        )
        db.execute(
            "INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            ("p1", "2026-06-16", "练吉他", now, now),
        )
        db.execute(
            "INSERT INTO comments(id, post_id, soul_name, role, content, seq, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (901, "p1", "kita", "user", "我自学吉他三个月了", 0, now),
        )
        with db.transaction() as conn:
            mes.record_comment_mutation(
                conn, comment_id=901, post_id="p1", soul_name="kita",
                role="user", op="create", content="我自学吉他三个月了", occurred_at=now,
            )

        seen_by_luna = memory_read.build_memory_section("public_post", "luna", "吉他").text
        self.assertIn("[尚未稳定沉淀的原始证据]", seen_by_luna)
        self.assertIn("（用户在 kita 的评论区） 我自学吉他三个月了", seen_by_luna)

        # For kita herself it is her own comment area — no cross-soul "用户在 X" tag.
        seen_by_kita = memory_read.build_memory_section("public_post", "kita", "吉他").text
        self.assertIn("（评论区） 我自学吉他三个月了", seen_by_kita)
        self.assertNotIn("用户在 kita 的评论区", seen_by_kita)


if __name__ == "__main__":
    unittest.main()
