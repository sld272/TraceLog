from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

    # --- P3 time labels ----------------------------------------------------

    def test_relative_time_tag_buckets(self) -> None:
        now = db.now_ts()
        day = 86400.0
        self.assertEqual(memory_read.relative_time_tag(0, now), "")
        self.assertEqual(memory_read.relative_time_tag(now - 60, now), "刚刚")
        self.assertEqual(memory_read.relative_time_tag(now - 5 * 3600, now), "5 小时前")
        self.assertEqual(memory_read.relative_time_tag(now - 1.5 * day, now), "昨天")
        self.assertEqual(memory_read.relative_time_tag(now - 3 * day, now), "3 天前")
        self.assertEqual(memory_read.relative_time_tag(now - 10 * day, now), "上周")
        self.assertEqual(memory_read.relative_time_tag(now - 40 * day, now), "上个月")
        self.assertEqual(memory_read.relative_time_tag(now - 400 * day, now), "一年多前")

    def test_injected_lines_carry_time_labels_and_rule(self) -> None:
        now = db.now_ts()
        day = 86400.0
        self._unit("global", "public", type="state", content="在准备答辩", importance=0.5, last_confirmed=now - 3 * day)
        self._unit("global", "public", type="preference", content="喜欢安静咖啡馆", importance=0.5, last_confirmed=now - 10 * day)
        prompt = memory_read.build_memory_section("public_post", "gotoh", "咖啡馆")
        self.assertIn("- 近期（3 天前）：在准备答辩", prompt.text)
        self.assertIn("|上周] 喜欢安静咖啡馆", prompt.text)
        self.assertIn("时间标注", prompt.text)  # interpretation rule ships with the labels

    # --- P1 read-time folding & contested hedge ----------------------------

    def test_same_fact_pair_folds_to_more_private_in_chat(self) -> None:
        public = self._unit("global", "public", type="preference", content="喜欢安静咖啡馆")
        ev = self._ev("soul:gotoh", "private:soul:gotoh", "chat")
        private = mus.add_unit(
            owner_scope="soul:gotoh", visibility_scope="private:soul:gotoh",
            source_channel="chat", type="preference", content="喜欢清静的咖啡馆",
            importance=0.5, evidence_event_ids=[ev],
        )
        mus.add_unit_link(public, private, "same_fact")
        prompt = memory_read.build_memory_section("chat", "gotoh", "咖啡馆")
        self.assertIn("喜欢清静的咖啡馆", prompt.text)   # the more-private copy
        self.assertNotIn("喜欢安静咖啡馆", prompt.text)  # public twin folded away

    def test_context_variant_pair_is_not_folded(self) -> None:
        public = self._unit("global", "public", type="preference", content="喜欢热闹的咖啡馆")
        ev = self._ev("soul:gotoh", "private:soul:gotoh", "chat")
        private = mus.add_unit(
            owner_scope="soul:gotoh", visibility_scope="private:soul:gotoh",
            source_channel="chat", type="preference", content="其实喜欢安静的咖啡馆",
            importance=0.5, evidence_event_ids=[ev],
        )
        mus.add_unit_link(public, private, "context_variant")
        prompt = memory_read.build_memory_section("chat", "gotoh", "咖啡馆")
        self.assertIn("喜欢热闹的咖啡馆", prompt.text)
        self.assertIn("其实喜欢安静的咖啡馆", prompt.text)

    def test_contested_unit_is_hedged_without_attribution(self) -> None:
        contested = self._unit("global", "public", type="preference", content="在准备考研")
        mus.mark_contested(contested)
        prompt = memory_read.build_memory_section("public_post", "gotoh", "考研")
        self.assertIn("「不太确定」", prompt.text)
        self.assertIn("不要解释或猜测它为什么不确定", prompt.text)
        self.assertNotIn("私聊", prompt.text.split("[记忆使用规则]")[0])  # no attribution on the line

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

    # --- FTS keyword channel for units ------------------------------------

    def test_fts_unit_ranks_matches_long_cjk_via_trigram(self) -> None:
        u = self._unit("global", "public", type="preference", content="喜欢在深夜的图书馆学习")
        self.assertIn(u, memory_read._fts_unit_ranks("图书馆"))  # ≥3-char CJK -> trigram

    def test_fts_unit_ranks_matches_short_cjk_via_like(self) -> None:
        # 2-char CJK words can't be tokenized by trigram; LIKE fallback must find them.
        u = self._unit("global", "public", type="preference", content="正在准备考研")
        self.assertIn(u, memory_read._fts_unit_ranks("考研"))

    def test_fts_unit_ranks_uses_rewrite_keywords_including_short_cjk(self) -> None:
        long_hit = self._unit("global", "public", type="preference", content="喜欢去图书馆")
        short_hit = self._unit("global", "public", type="preference", content="在准备考研")
        ranks = memory_read._fts_unit_ranks("随便问问", keywords=["图书馆", "考研"])
        self.assertIn(long_hit, ranks)   # trigram
        self.assertIn(short_hit, ranks)  # LIKE (2-char keyword)

    def test_fts_raw_query_recovers_embedded_short_cjk_words(self) -> None:
        # raw fallback (no rewrite): a unit whose only overlap is a 2-char word must
        # still be found even though the whole query is >=3 chars — trigram can't
        # tokenize the 2-char parts, so they must be routed to LIKE.
        u = self._unit("global", "public", type="preference", content="用户在准备考研")
        self.assertIn(u, memory_read._fts_unit_ranks("考研 规划"))  # space-separated words
        self.assertIn(u, memory_read._fts_unit_ranks("考研进展"))   # spaceless, same gap

    def test_fts_raw_query_single_cjk_char_via_like(self) -> None:
        u = self._unit("global", "public", type="preference", content="喜欢画画")
        self.assertIn(u, memory_read._fts_unit_ranks("画"))  # no candidates -> LIKE whole query

    def test_fts_keywords_split_multiword_cjk_phrase(self) -> None:
        # rewriter packed two 2-char words into one keyword string; it must still
        # recall what the raw query "考研 规划" would — the embedded 考研 via LIKE.
        u = self._unit("global", "public", type="preference", content="用户在准备考研")
        self.assertIn(u, memory_read._fts_unit_ranks("", ["考研 规划"]))

    def test_fts_keywords_separate_short_cjk_no_regression(self) -> None:
        u = self._unit("global", "public", type="preference", content="用户在准备考研")
        self.assertIn(u, memory_read._fts_unit_ranks("", ["考研", "规划"]))

    def test_fts_keywords_ascii_term_no_regression(self) -> None:
        u = self._unit("global", "public", type="preference", content="正在学习 ChromaDB 向量库")
        self.assertIn(u, memory_read._fts_unit_ranks("", ["ChromaDB"]))

    def test_retrieve_units_keeps_only_fts_or_semantic_matches(self) -> None:
        hit = self._unit("global", "public", type="preference", content="周末喜欢去爬山")
        self._unit("global", "public", type="preference", content="讨厌喝咖啡")
        ids = [h.unit_id for h in memory_read.retrieve_units("爬山", "public_post", "gotoh")]
        self.assertIn(hit, ids)
        self.assertEqual(1, len(ids))  # the unrelated unit is gated out

    # --- adaptive semantic gate (P4) ---------------------------------------

    def test_adaptive_cutoff_rescues_subfloor_head_cluster(self) -> None:
        # coherent head cluster below the legacy 0.30 floor, clearly separated
        # from the tail -> the gap wins and the cluster passes
        cutoff = memory_read.adaptive_sim_cutoff([0.29, 0.28, 0.20])
        self.assertAlmostEqual(cutoff, 0.24)
        self.assertLess(cutoff, 0.28)

    def test_adaptive_cutoff_flat_sequence_falls_back_to_legacy_floor(self) -> None:
        self.assertEqual(
            memory_read.adaptive_sim_cutoff([0.60, 0.58, 0.57]),
            memory_read.SEMANTIC_SIM_FALLBACK_FLOOR,
        )

    def test_adaptive_cutoff_strong_winner_tightens_gate(self) -> None:
        # a dominant top hit pushes the cutoff above the legacy floor, cutting
        # the noise tail the fixed 0.30 used to admit
        cutoff = memory_read.adaptive_sim_cutoff([0.80, 0.40])
        self.assertAlmostEqual(cutoff, 0.60)

    def test_adaptive_cutoff_single_hit_uses_legacy_floor(self) -> None:
        self.assertEqual(
            memory_read.adaptive_sim_cutoff([0.25]),
            memory_read.SEMANTIC_SIM_FALLBACK_FLOOR,
        )

    # --- semantic distance floor (R3) -------------------------------------

    def test_semantic_sims_apply_distance_floor(self) -> None:
        hits = [
            SimpleNamespace(doc_id="unit-near", metadata={"unit_id": "near"}, rank=1, distance=0.2),
            SimpleNamespace(doc_id="unit-far", metadata={"unit_id": "far"}, rank=2, distance=0.95),
        ]
        with patch("core.vectorstore.query_documents", return_value=hits):
            sims = memory_read._semantic_unit_sims("随便问问")
        self.assertIn("near", sims)          # similarity 0.8 >= floor
        self.assertNotIn("far", sims)        # similarity 0.05 gated out
        self.assertAlmostEqual(0.8, sims["near"])

    def test_semantic_sims_keep_missing_distance_fail_open(self) -> None:
        hits = [SimpleNamespace(doc_id="unit-x", metadata={"unit_id": "x"}, rank=1, distance=None)]
        with patch("core.vectorstore.query_documents", return_value=hits):
            sims = memory_read._semantic_unit_sims("随便问问")
        self.assertIn("x", sims)  # unknown distance kept (fail-open), not floored

    def test_retrieve_units_gates_out_far_semantic_only_match(self) -> None:
        u = self._unit("global", "public", type="preference", content="完全无关的内容")
        far = [SimpleNamespace(doc_id=f"unit-{u}", metadata={"unit_id": u}, rank=1, distance=0.95)]
        with patch("core.vectorstore.query_documents", return_value=far):
            hits = memory_read.retrieve_units("zzz", "public_post", "gotoh")
        self.assertEqual([], [h.unit_id for h in hits])  # far semantic + no FTS -> dropped

    # --- retrieval debug log (memory_retrieval) ---------------------------

    def test_semantic_unit_hits_retains_sub_floor_neighbors(self) -> None:
        hits = [
            SimpleNamespace(doc_id="unit-near", metadata={"unit_id": "near"}, rank=1, distance=0.2),
            SimpleNamespace(doc_id="unit-far", metadata={"unit_id": "far"}, rank=2, distance=0.95),
        ]
        with patch("core.vectorstore.query_documents", return_value=hits):
            out = memory_read._semantic_unit_hits("随便问问")
        by_id = {h.unit_id: h for h in out}
        self.assertEqual(2, len(out))            # the sub-floor neighbour is retained
        self.assertTrue(by_id["near"].passed)
        self.assertFalse(by_id["far"].passed)    # below the floor but still present
        self.assertAlmostEqual(0.05, by_id["far"].sim)

    def test_memory_retrieval_log_emitted_at_debug(self) -> None:
        hit = self._unit("global", "public", type="preference", content="周末喜欢去爬山")
        far_uid = self._unit("global", "public", type="preference", content="完全无关")
        sem = [
            SimpleNamespace(doc_id=f"unit-{hit}", metadata={"unit_id": hit}, rank=1, distance=0.2),
            SimpleNamespace(doc_id=f"unit-{far_uid}", metadata={"unit_id": far_uid}, rank=2, distance=0.95),
        ]
        events = []

        def capture(event, level="INFO", **fields):
            events.append((event, level, fields))

        with patch("core.vectorstore.query_documents", return_value=sem), \
             patch.object(memory_read.logging_service, "is_enabled_for", return_value=True), \
             patch.object(memory_read.logging_service, "log_event", side_effect=capture):
            memory_read.retrieve_units("爬山", "public_post", "gotoh")

        logged = [(lvl, f) for (e, lvl, f) in events if e == "memory_retrieval"]
        self.assertEqual(1, len(logged))
        level, payload = logged[0]
        self.assertEqual("DEBUG", level)                 # silent under the default INFO
        near = next(u for u in payload["units"] if u["unit_id"] == hit)
        self.assertAlmostEqual(0.8, near["sem_sim"])     # distance surfaced as similarity
        self.assertTrue(near["in_top_k"])
        floored_ids = {n["unit_id"] for n in payload["floored_neighbors"]}
        self.assertIn(far_uid, floored_ids)              # rejected-but-near kept for tuning

    def test_memory_retrieval_silent_when_logging_disabled(self) -> None:
        self._unit("global", "public", type="preference", content="周末喜欢去爬山")
        with patch("core.vectorstore.query_documents", return_value=[]), \
             patch.object(memory_read.logging_service, "_enabled", False), \
             patch.object(memory_read.logging_service, "log_event") as mock_log:
            memory_read.retrieve_units("爬山", "public_post", "gotoh")
        mock_log.assert_not_called()  # no debug payload built when logging is off

    # --- evidence retrieval channel (raw docs -> owning unit) ---------------

    @staticmethod
    def _vector_router(unit_hits, evidence_hits):
        """Dispatch a patched query_documents on the `where` filter: the unit
        channel queries type='unit', the evidence channel type $in raw docs."""
        def fake(query, n_results=20, where=None):
            if (where or {}).get("type") == "unit":
                return unit_hits
            return evidence_hits
        return fake

    def _score_post_unit(self, *, content="用户在攻读大学学业"):
        """A broad unit whose only mention of the exam score lives in its
        evidence text — the retrieval-key gap the evidence channel closes."""
        with db.transaction() as conn:
            ev = mes.record_post_mutation(
                conn, post_id="p-score", op="create",
                content="期末考了92分", occurred_at=1000.0,
            ).id
        return mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="insight", content=content, confidence=0.8, importance=0.5,
            evidence_event_ids=[ev],
        )

    @staticmethod
    def _post_doc_hit(post_id="p-score", distance=0.2):
        return SimpleNamespace(
            doc_id=f"post-{post_id}", type="post", source_id=post_id,
            rank=1, distance=distance, metadata={"type": "post", "post_id": post_id},
            document=None,
        )

    def test_retrieve_admits_unit_via_evidence_text_hit(self) -> None:
        # Query matches only the raw post text, not the broad unit content —
        # without the evidence channel this unit is unreachable.
        uid = self._score_post_unit()
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            hits = memory_read.retrieve_units("我期末考了多少来着", "public_post", "gotoh")
        self.assertIn(uid, [h.unit_id for h in hits])

    def test_evidence_channel_respects_excluded_sources(self) -> None:
        # Replying to the score post itself: its evidence must not self-retrieve.
        uid = self._score_post_unit()
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            hits = memory_read.retrieve_units(
                "我期末考了多少来着", "public_post", "gotoh",
                excluded_sources={("post", "p-score")},
            )
        self.assertNotIn(uid, [h.unit_id for h in hits])

    def test_evidence_channel_does_not_resurface_retracted_unit(self) -> None:
        # User "forgot" the unit: its evidence must not bring it back.
        uid = self._score_post_unit()
        mus.retract_unit(uid, by="user")
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            hits = memory_read.retrieve_units("我期末考了多少来着", "public_post", "gotoh")
        self.assertEqual([], [h.unit_id for h in hits])

    def test_evidence_channel_skips_deleted_source(self) -> None:
        uid = self._score_post_unit()
        with db.transaction() as conn:
            mes.record_post_mutation(
                conn, post_id="p-score", op="delete", content=None, occurred_at=1001.0,
            )
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            hits = memory_read.retrieve_units("我期末考了多少来着", "public_post", "gotoh")
        self.assertEqual([], [h.unit_id for h in hits])

    def test_evidence_channel_skips_assistant_lines(self) -> None:
        # An AI comment matching the query is not user evidence.
        uid = self._score_post_unit()
        ai_doc = SimpleNamespace(
            doc_id="comment-77", type="comment", source_id="77", rank=1, distance=0.1,
            metadata={"type": "comment", "role": "assistant"}, document=None,
        )
        router = self._vector_router([], [ai_doc])
        with patch("core.vectorstore.query_documents", side_effect=router):
            hits = memory_read.retrieve_units("我期末考了多少来着", "public_post", "gotoh")
        self.assertEqual([], [h.unit_id for h in hits])

    def test_evidence_channel_cannot_leak_other_souls_private_chat(self) -> None:
        # kita's private-chat evidence hit resolves to a private unit; the scoped
        # candidate set must still keep it away from gotoh's public reply.
        with db.transaction() as conn:
            ev = mes.record_chat_mutation(
                conn, message_id=501, soul_name="kita", op="create",
                content="悄悄说：期末考了92分", occurred_at=1000.0, role="user",
            ).id
        mus.add_unit(
            owner_scope="soul:kita", visibility_scope="private:soul:kita",
            source_channel="chat", type="insight", content="用户在攻读大学学业",
            confidence=0.8, importance=0.5, evidence_event_ids=[ev],
        )
        chat_doc = SimpleNamespace(
            doc_id="chat-501", type="chat", source_id="501", rank=1, distance=0.1,
            metadata={"type": "chat", "role": "user", "soul_name": "kita"}, document=None,
        )
        router = self._vector_router([], [chat_doc])
        with patch("core.vectorstore.query_documents", side_effect=router):
            hits = memory_read.retrieve_units("我期末考了多少来着", "public_post", "gotoh")
        self.assertEqual([], hits)

    # --- orphan evidence injection (未沉淀原文直接进上下文) ------------------

    def _orphan_post(self, post_id="p-score", content="期末考了92分", occurred_at=1000.0):
        """A raw post reconcile never condensed into any unit."""
        with db.transaction() as conn:
            mes.record_post_mutation(
                conn, post_id=post_id, op="create",
                content=content, occurred_at=occurred_at,
            )

    def test_orphan_evidence_injected_as_raw_context(self) -> None:
        # No unit exists for the fact at all — unit retrieval can't reach it by
        # construction, so the evidence hit must inject the raw line itself.
        self._orphan_post()
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            prompt = memory_read.build_memory_section(
                "public_post", "gotoh", "我期末考了多少来着",
            )
        self.assertIn("[未整理成记忆的相关原文]", prompt.text)
        self.assertIn("期末考了92分", prompt.text)
        # cited like freshness so the 引用记忆 panel shows what the reply leaned on
        self.assertIn("期末考了92分", [f.content for f in prompt.used_freshness])

    def test_orphan_evidence_respects_excluded_sources(self) -> None:
        # Replying to the orphan post itself: its own text must not self-inject.
        self._orphan_post()
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            prompt = memory_read.build_memory_section(
                "public_post", "gotoh", "我期末考了多少来着",
                excluded_sources={("post", "p-score")},
            )
        self.assertNotIn("[未整理成记忆的相关原文]", prompt.text)

    def test_orphan_private_chat_not_leaked_to_public_reply(self) -> None:
        # kita's private-chat line has no unit; direct injection must still be
        # scope-filtered, never reaching gotoh's public reply.
        with db.transaction() as conn:
            mes.record_chat_mutation(
                conn, message_id=601, soul_name="kita", op="create",
                content="悄悄说：期末考了92分", occurred_at=1000.0, role="user",
            )
        chat_doc = SimpleNamespace(
            doc_id="chat-601", type="chat", source_id="601", rank=1, distance=0.1,
            metadata={"type": "chat", "role": "user", "soul_name": "kita"}, document=None,
        )
        router = self._vector_router([], [chat_doc])
        with patch("core.vectorstore.query_documents", side_effect=router):
            prompt = memory_read.build_memory_section(
                "public_post", "gotoh", "我期末考了多少来着",
            )
        self.assertNotIn("92分", prompt.text)

    def test_retracted_unit_evidence_is_not_orphan(self) -> None:
        # Retraction keeps the memory_unit_evidence rows, so a retracted unit's
        # evidence must not resurface as raw orphan text either.
        uid = self._score_post_unit()
        mus.retract_unit(uid, by="user")
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            prompt = memory_read.build_memory_section(
                "public_post", "gotoh", "我期末考了多少来着",
            )
        self.assertNotIn("期末考了92分", prompt.text)
        self.assertNotIn("[未整理成记忆的相关原文]", prompt.text)

    def test_pending_review_link_is_not_orphan(self) -> None:
        # Evidence mid-relink (review_pending=1) was condensed once: not an
        # orphan, and not a confirmed unit vouch either — the freshness seam's
        # review path owns surfacing it.
        uid = self._score_post_unit()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memory_unit_evidence SET review_pending = 1 WHERE unit_id = ?",
                (uid,),
            )
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            hits, orphans = memory_read._evidence_unit_hits("我期末考了多少来着")
        self.assertEqual([], hits)
        self.assertEqual([], orphans)

    def test_orphan_evidence_capped_and_best_sim_first(self) -> None:
        docs = []
        for i in range(5):
            pid = f"p-orph{i}"
            self._orphan_post(post_id=pid, content=f"孤儿事实{i}", occurred_at=1000.0 + i)
            docs.append(self._post_doc_hit(post_id=pid, distance=0.2 + i * 0.01))
        router = self._vector_router([], docs)
        with patch("core.vectorstore.query_documents", side_effect=router):
            _, _, orphan_items = memory_read.retrieve_units_with_anchors(
                "孤儿事实", "public_post", "gotoh",
            )
        self.assertEqual(memory_read.EVIDENCE_ORPHAN_MAX, len(orphan_items))
        self.assertEqual("孤儿事实0", orphan_items[0].content)

    def test_orphan_not_duplicated_in_freshness(self) -> None:
        # A recent un-condensed post matched by the query would qualify for BOTH
        # the orphan section and the freshness seam — it must appear only once.
        self._orphan_post(occurred_at=db.now_ts())
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            prompt = memory_read.build_memory_section(
                "public_post", "gotoh", "我期末考了多少来着",
            )
        self.assertIn("[未整理成记忆的相关原文]", prompt.text)
        self.assertEqual(1, prompt.text.count("期末考了92分"))

    # --- evidence anchors steer recall (相关对话原文锚点) --------------------

    def test_recall_prefers_evidence_anchor_over_keyword_guess(self) -> None:
        # A unit backed by two posts; the query terms favor the WRONG one by
        # keyword overlap. The anchor from the evidence channel must win.
        with db.transaction() as conn:
            mes.record_post_mutation(
                conn, post_id="p-guitar", op="create",
                content="今天练吉他很开心", occurred_at=1000.0,
            )
            ev_score = mes.record_post_mutation(
                conn, post_id="p-score", op="create",
                content="期末考了92分", occurred_at=999.0,
            )
        ev_guitar = mes.latest_source_event("post", "p-guitar")
        uid = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="insight", content="用户的大学生活丰富", confidence=0.8, importance=0.5,
            evidence_event_ids=[int(ev_guitar["id"]), ev_score.id],
        )
        hit = memory_read.MemoryItem(
            unit_id=uid, type="insight", content="用户的大学生活丰富",
            confidence=0.8, importance=0.5, owner_scope="global",
            visibility_scope="public", needs_discretion=False,
        )
        anchor = mes.current_effective_event("post", "p-score")

        # terms overlap only the guitar post; without the anchor it would recall it
        unanchored = memory_read._recall_conversations([hit], "public_post", "gotoh", ["吉他"])
        self.assertIn("今天练吉他很开心", unanchored)

        anchored = memory_read._recall_conversations(
            [hit], "public_post", "gotoh", ["吉他"], anchors={uid: anchor},
        )
        self.assertIn("期末考了92分", anchored)
        self.assertNotIn("今天练吉他很开心", anchored)

    def test_build_section_surfaces_fact_reachable_only_via_evidence(self) -> None:
        # End-to-end acceptance for the exam-score bug: broad unit, fact only in
        # the raw post; query matches neither unit FTS nor unit semantics. The
        # evidence channel must admit the unit AND recall the matching post.
        uid = self._score_post_unit()
        router = self._vector_router([], [self._post_doc_hit()])
        with patch("core.vectorstore.query_documents", side_effect=router):
            prompt = memory_read.build_memory_section(
                "public_post", "gotoh", "我期末考了多少来着",
            )
        self.assertIn("[相关记忆]", prompt.text)
        self.assertIn("用户在攻读大学学业", prompt.text)   # the admitted unit
        self.assertIn("[相关对话原文]", prompt.text)
        self.assertIn("期末考了92分", prompt.text)          # the fact itself
        self.assertIn(uid, prompt.used_unit_ids)

    def test_recall_log_marks_anchored_links(self) -> None:
        uid = self._score_post_unit()
        anchor = mes.current_effective_event("post", "p-score")
        hit = memory_read.MemoryItem(
            unit_id=uid, type="insight", content="用户在攻读大学学业",
            confidence=0.8, importance=0.5, owner_scope="global",
            visibility_scope="public", needs_discretion=False,
        )
        events = []

        def capture(event, level="INFO", **fields):
            events.append((event, fields))

        with patch.object(memory_read.logging_service, "is_enabled_for", return_value=True), \
             patch.object(memory_read.logging_service, "log_event", side_effect=capture):
            memory_read._recall_conversations(
                [hit], "public_post", "gotoh", [], anchors={uid: anchor},
            )
        payload = next(f for (e, f) in events if e == "memory_recall")
        link = next(item for item in payload["links"] if item["unit_id"] == uid)
        self.assertTrue(link["anchored"])
        self.assertEqual("p-score", link["post_id"])

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

    def test_recall_log_records_comment_to_post_link(self) -> None:
        now = 1000.0
        db.execute(
            "INSERT INTO souls(name, file_path, created_at, updated_at) VALUES(?, ?, ?, ?)",
            ("kita", "souls/kita.md", now, now),
        )
        db.execute(
            "INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            ("20260616-001", "2026-06-16", "今天练吉他", now, now),
        )
        db.execute(
            "INSERT INTO comments(id, post_id, soul_name, role, content, seq, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (901, "20260616-001", "kita", "user", "我自学吉他三个月了", 0, now),
        )
        with db.transaction() as conn:
            mes.record_post_mutation(
                conn, post_id="20260616-001", op="create", content="今天练吉他", occurred_at=now,
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

        events = []

        def capture(event, level="INFO", **fields):
            events.append((event, fields))

        with patch.object(memory_read.logging_service, "is_enabled_for", return_value=True), \
             patch.object(memory_read.logging_service, "log_event", side_effect=capture):
            memory_read._recall_conversations(
                [hit], "public_post", "kita", ["吉他"], trace_context={"post_id": "20260616-001"}
            )

        logged = [f for (e, f) in events if e == "memory_recall"]
        self.assertEqual(1, len(logged))
        payload = logged[0]
        link = next(item for item in payload["links"] if item["unit_id"] == uid)
        self.assertEqual("comment", link["via"])           # comment evidence resolved
        self.assertEqual("901", link["comment_id"])        # the comment id, and
        self.assertEqual("20260616-001", link["post_id"])  # the post it hangs under
        self.assertEqual("kita", link["soul"])
        self.assertEqual({"post_id": "20260616-001"}, payload["trace"])

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
