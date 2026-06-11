from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import db, fts_query, logging_service, retrieval, vectorstore


class FtsQueryTest(unittest.TestCase):
    def test_long_cjk_query_builds_phrase_and_window_candidates(self) -> None:
        query = "我之前是不是说过晚上图书馆学习效率更高"

        candidates = fts_query.match_candidates(query)
        match = fts_query.build_match_query(query)

        self.assertGreater(len(candidates), 1)
        self.assertEqual("我之前是不是说过晚上图书馆学习效率更高", candidates[0])
        self.assertIn("晚上", candidates)
        self.assertIn("图书馆", candidates)
        self.assertIn("学习", candidates)
        self.assertIn("效率", candidates)
        self.assertIn("更高", candidates)
        self.assertNotIn("我之", candidates)
        self.assertNotIn("之前", candidates)
        self.assertNotIn("是不是", candidates)
        self.assertNotIn("说过", candidates)
        self.assertLessEqual(len(candidates), 16)
        self.assertIn('"图书馆"', match)
        self.assertIn(" OR ", match)

    def test_english_and_numeric_terms_are_preserved(self) -> None:
        candidates = fts_query.match_candidates("ChromaDB fts5 2026")

        self.assertIn("ChromaDB", candidates)
        self.assertIn("fts5", candidates)
        self.assertIn("2026", candidates)

    def test_special_symbols_do_not_create_unquoted_match_syntax(self) -> None:
        match = fts_query.build_match_query('"ChromaDB" (fts5) ^ {2026}')

        self.assertIn('"ChromaDB"', match)
        self.assertIn('"fts5"', match)
        self.assertIn('"2026"', match)
        self.assertNotIn("^", match)

    def test_empty_or_symbol_only_query_returns_empty_match(self) -> None:
        self.assertEqual("", fts_query.build_match_query(""))
        self.assertEqual("", fts_query.build_match_query('"""()^{}[]'))


class RetrievalFusionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_fts_search_scored = retrieval.fts_search_scored
        self.old_vector_search_scored = retrieval.vector_search_scored
        self.old_read_candidate_contents = retrieval._read_candidate_contents
        self.old_log_event = retrieval.logging_service.log_event
        self.logged_events: list[dict] = []
        retrieval.logging_service.log_event = lambda event, **fields: self.logged_events.append({"event": event, **fields})

    def tearDown(self) -> None:
        retrieval.fts_search_scored = self.old_fts_search_scored
        retrieval.vector_search_scored = self.old_vector_search_scored
        retrieval._read_candidate_contents = self.old_read_candidate_contents
        retrieval.logging_service.log_event = self.old_log_event

    def stub_search(
        self,
        *,
        fts_hits: list[retrieval.RetrievalHit],
        vector_hits: list[retrieval.RetrievalHit],
        contents: dict[str, str] | None = None,
    ) -> None:
        retrieval.fts_search_scored = lambda query, k=20, **kwargs: fts_hits
        retrieval.vector_search_scored = lambda query, k=20: vector_hits
        retrieval._read_candidate_contents = lambda post_ids: contents or {}

    def test_exact_keyword_fts_hit_beats_weak_vector_hit(self) -> None:
        self.stub_search(
            fts_hits=[retrieval.RetrievalHit("p-fts", "fts", 1, -2.0)],
            vector_hits=[
                retrieval.RetrievalHit("p-vector-strong", "vector", 1, 0.2),
                retrieval.RetrievalHit("p-vector-weak", "vector", 2, 0.8),
            ],
            contents={"p-fts": "今天复习 408 数据结构"},
        )

        hits = retrieval.hybrid_search_scored("408", k=3)

        self.assertEqual("p-fts", hits[0].post_id)
        self.assertIn("fts:rank=1", hits[0].reasons)
        self.assertIn("exact_phrase", hits[0].reasons)

    def test_descriptive_query_boosts_vector_weight(self) -> None:
        self.stub_search(
            fts_hits=[retrieval.RetrievalHit("p-fts", "fts_trigram", 1, -1.0)],
            vector_hits=[retrieval.RetrievalHit("p-vector", "vector", 1, 0.1)],
        )

        hits = retrieval.hybrid_search_scored("最近那种焦虑状态怎么办", k=2)

        vector_hit = next(hit for hit in hits if hit.post_id == "p-vector")
        fts_hit = next(hit for hit in hits if hit.post_id == "p-fts")
        self.assertGreater(vector_hit.score, fts_hit.score)

    def test_agreement_bonus_is_reported(self) -> None:
        self.stub_search(
            fts_hits=[retrieval.RetrievalHit("p-both", "fts", 1, -1.0)],
            vector_hits=[retrieval.RetrievalHit("p-both", "vector", 1, 0.1)],
        )

        hits = retrieval.hybrid_search_scored("考试压力", k=1)

        self.assertEqual("p-both", hits[0].post_id)
        self.assertIn("agreement", hits[0].reasons)
        self.assertGreater(hits[0].score, hits[0].fts_score * 0.5)

    def test_single_strong_source_is_not_averaged_down(self) -> None:
        self.stub_search(
            fts_hits=[retrieval.RetrievalHit("p-fts", "fts", 1, -1.0)],
            vector_hits=[
                retrieval.RetrievalHit("p-other", "vector", 1, 0.1),
                retrieval.RetrievalHit("p-fts", "vector", 2, 0.9),
            ],
        )

        hits = retrieval.hybrid_search_scored("408", k=2)
        fts_hit = next(hit for hit in hits if hit.post_id == "p-fts")

        self.assertGreaterEqual(fts_hit.score, 0.55)

    def test_min_score_filters_and_fallback_returns_one(self) -> None:
        self.stub_search(
            fts_hits=[
                retrieval.RetrievalHit("p-1", "fts", 1, -1.0),
                retrieval.RetrievalHit("p-2", "fts", 2, -0.5),
            ],
            vector_hits=[],
        )

        strict_hits = retrieval.hybrid_search_scored("普通查询", k=3, min_score=0.9, allow_fallback=False)
        fallback_hits = retrieval.hybrid_search_scored("普通查询", k=3, min_score=0.9, allow_fallback=True)

        self.assertEqual([], strict_hits)
        self.assertEqual(["p-1"], [hit.post_id for hit in fallback_hits])

    def test_stable_tie_break_ordering(self) -> None:
        self.stub_search(
            fts_hits=[
                retrieval.RetrievalHit("p-001", "fts", 1, -1.0),
                retrieval.RetrievalHit("p-003", "fts", 1, -1.0),
                retrieval.RetrievalHit("p-002", "fts", 1, -1.0),
            ],
            vector_hits=[],
        )

        hits = retrieval.hybrid_search_scored("same", k=3)

        self.assertEqual(["p-003", "p-002", "p-001"], [hit.post_id for hit in hits])

    def test_empty_sources_return_empty(self) -> None:
        self.stub_search(fts_hits=[], vector_hits=[])

        self.assertEqual([], retrieval.hybrid_search_scored("nothing", k=3))

    def test_exclusion_filters_self_before_truncation_and_logs_event(self) -> None:
        self.stub_search(
            fts_hits=[
                retrieval.RetrievalHit("p-self", "fts", 1, -2.0),
                retrieval.RetrievalHit("p-fts", "fts", 2, -1.0),
            ],
            vector_hits=[
                retrieval.RetrievalHit("p-self", "vector", 1, 0.1),
                retrieval.RetrievalHit("p-vector", "vector", 2, 0.2),
            ],
        )

        hits = retrieval.hybrid_search_scored(
            "自引用查询",
            k=2,
            trace_context={"channel": "public_post", "post_id": "p-self"},
            exclusion=retrieval.RetrievalExclusion(post_ids=frozenset({"p-self"})),
        )

        self.assertEqual({"p-fts", "p-vector"}, {hit.post_id for hit in hits})
        self.assertNotIn("p-self", [hit.post_id for hit in hits])
        event = next(event for event in self.logged_events if event["event"] == "retrieval_self_excluded")
        self.assertEqual("public_post", event["channel"])
        self.assertEqual("p-self", event["post_id"])
        self.assertEqual(["post-p-self"], event["excluded_doc_ids"])


class VectorDistanceFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_query_post_hits = vectorstore.query_post_hits
        self.old_query_documents = vectorstore.query_documents
        self.old_fts_search_scored = retrieval.fts_search_scored
        self.old_read_post_content = retrieval._read_post_content
        self.old_log_event = retrieval.logging_service.log_event
        self.logged_events: list[dict] = []

        def capture_log_event(event: str, **fields) -> None:
            self.logged_events.append({"event": event, **fields})

        retrieval.logging_service.log_event = capture_log_event
        retrieval.fts_search_scored = lambda *args, **kwargs: []
        retrieval._read_post_content = lambda post_id: ""

    def tearDown(self) -> None:
        vectorstore.query_post_hits = self.old_query_post_hits
        vectorstore.query_documents = self.old_query_documents
        retrieval.fts_search_scored = self.old_fts_search_scored
        retrieval._read_post_content = self.old_read_post_content
        retrieval.logging_service.log_event = self.old_log_event

    def test_vector_hit_beyond_threshold_dropped(self) -> None:
        vectorstore.query_post_hits = lambda query, n_results=20: [
            vectorstore.VectorHit("p-near", 1, 0.3),
            vectorstore.VectorHit("p-far", 2, 0.9),
        ]

        hits = retrieval.vector_search_scored("焦虑", k=20)

        self.assertEqual(["p-near"], [hit.post_id for hit in hits])

    def test_all_vector_hits_filtered_returns_empty(self) -> None:
        vectorstore.query_documents = lambda query, n_results=20, where=None: [
            vectorstore.VectorDocHit("post-p-far", "post", "p-far", 1, 0.9, {"type": "post", "post_id": "p-far"})
        ]

        hits = retrieval.hybrid_search_documents("完全无关", k=3)

        self.assertEqual([], hits)

    def test_fts_hits_unaffected_by_vector_filter(self) -> None:
        retrieval.fts_search_scored = lambda *args, **kwargs: [
            retrieval.RetrievalHit("p-keyword", "fts", 1, -1.0)
        ]
        vectorstore.query_documents = lambda query, n_results=20, where=None: [
            vectorstore.VectorDocHit("post-p-far", "post", "p-far", 1, 0.9, {"type": "post", "post_id": "p-far"})
        ]

        hits = retrieval.hybrid_search_documents("关键词", k=3)

        self.assertEqual(["post-p-keyword"], [hit.doc_id for hit in hits])
        self.assertEqual(["fts"], hits[0].sources)

    def test_none_distance_kept(self) -> None:
        vectorstore.query_post_hits = lambda query, n_results=20: [
            vectorstore.VectorHit("p-unknown-distance", 1, None)
        ]

        hits = retrieval.vector_search_scored("焦虑", k=20)

        self.assertEqual(["p-unknown-distance"], [hit.post_id for hit in hits])

    def test_wide_candidate_pool_rescues_filtered_top_ranks(self) -> None:
        captured: dict[str, int] = {}

        def fake_query_documents(query, n_results=20, where=None):
            captured["n_results"] = n_results
            return [
                vectorstore.VectorDocHit("chat-far-1", "chat", "1", 1, 0.91, {"type": "chat"}),
                vectorstore.VectorDocHit("comment-far-2", "comment", "2", 2, 0.82, {"type": "comment"}),
                vectorstore.VectorDocHit("post-p-far", "post", "p-far", 3, 0.77, {"type": "post", "post_id": "p-far"}),
                vectorstore.VectorDocHit(
                    "post-p-relevant",
                    "post",
                    "p-relevant",
                    4,
                    0.40,
                    {"type": "post", "post_id": "p-relevant"},
                ),
                vectorstore.VectorDocHit(
                    "chat-12",
                    "chat",
                    "12",
                    5,
                    0.38,
                    {"type": "chat", "thread_id": 7, "message_id": 12, "soul_name": "小黑"},
                ),
            ]

        vectorstore.query_documents = fake_query_documents

        hits = retrieval.hybrid_search_documents("上次聊的比赛", k=3)

        self.assertEqual(20, captured["n_results"])
        self.assertEqual(["chat-12", "post-p-relevant"], [hit.doc_id for hit in hits])
        self.assertEqual(["chat", "post"], [hit.type for hit in hits])
        self.assertEqual([2, 1], [hit.rank for hit in hits])

    def test_filtered_event_logged(self) -> None:
        vectorstore.query_post_hits = lambda query, n_results=20: [
            vectorstore.VectorHit("p-near", 1, 0.3),
            vectorstore.VectorHit("p-far", 2, 0.9),
        ]

        retrieval.vector_search_scored("焦虑", k=20)

        events = [event for event in self.logged_events if event["event"] == "vector_hits_filtered"]
        self.assertTrue(events)
        self.assertEqual("posts", events[-1]["target"])
        self.assertEqual(1, events[-1]["dropped_count"])
        self.assertEqual([0.9], events[-1]["dropped_distances"])

    def test_doc_retrieval_result_logged_with_hits(self) -> None:
        vectorstore.query_documents = lambda query, n_results=20, where=None: [
            vectorstore.VectorDocHit(
                "chat-12",
                "chat",
                "12",
                1,
                0.25,
                {"type": "chat", "thread_id": 7, "message_id": 12, "soul_name": "小黑"},
            )
        ]

        hits = retrieval.hybrid_search_documents(
            "上次聊的比赛", k=3, trace_context={"channel": "chat", "thread_id": 7}
        )

        self.assertEqual(["chat-12"], [hit.doc_id for hit in hits])
        events = [event for event in self.logged_events if event["event"] == "hybrid_doc_retrieval_result"]
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("chat", event["channel"])
        self.assertEqual(7, event["thread_id"])
        self.assertEqual("上次聊的比赛", event["raw_query"])
        self.assertEqual([], event["fts_hits"])
        self.assertEqual(1, len(event["vector_hits"]))
        self.assertEqual(0.25, event["vector_hits"][0]["distance"])
        final = event["final_hits"]
        self.assertEqual(1, len(final))
        self.assertEqual("chat-12", final[0]["doc_id"])
        self.assertEqual("chat", final[0]["type"])
        self.assertIn("score", final[0])
        self.assertIn("vector:rank=1", final[0]["reasons"])

    def test_doc_retrieval_result_logged_when_empty(self) -> None:
        vectorstore.query_documents = lambda query, n_results=20, where=None: [
            vectorstore.VectorDocHit("post-p-far", "post", "p-far", 1, 0.9, {"type": "post", "post_id": "p-far"})
        ]

        hits = retrieval.hybrid_search_documents("完全无关", k=3, trace_context={"channel": "comment"})

        self.assertEqual([], hits)
        events = [event for event in self.logged_events if event["event"] == "hybrid_doc_retrieval_result"]
        self.assertEqual(1, len(events))
        self.assertEqual([], events[0]["final_hits"])
        self.assertEqual([], events[0]["vector_hits"])

    def test_document_exclusion_filters_current_post_and_all_comments_before_truncation(self) -> None:
        retrieval.fts_search_scored = lambda *args, **kwargs: [
            retrieval.RetrievalHit("p-current", "fts", 1, -2.0),
            retrieval.RetrievalHit("p-history", "fts", 2, -1.0),
        ]
        vectorstore.query_documents = lambda query, n_results=20, where=None: [
            vectorstore.VectorDocHit(
                "post-vision-p-current",
                "post_vision",
                "p-current",
                1,
                0.1,
                {"type": "post_vision", "post_id": "p-current"},
            ),
            vectorstore.VectorDocHit(
                "comment-1",
                "comment",
                "1",
                2,
                0.2,
                {"type": "comment", "post_id": "p-current", "soul_name": "默认"},
            ),
            vectorstore.VectorDocHit(
                "comment-2",
                "comment",
                "2",
                3,
                0.3,
                {"type": "comment", "post_id": "p-current", "soul_name": "毒舌好友"},
            ),
            vectorstore.VectorDocHit(
                "comment-3",
                "comment",
                "3",
                4,
                0.4,
                {"type": "comment", "post_id": "p-other", "soul_name": "默认"},
            ),
        ]

        hits = retrieval.hybrid_search_documents(
            "当前帖内容",
            k=2,
            trace_context={"channel": "comment", "post_id": "p-current"},
            exclusion=retrieval.RetrievalExclusion(
                post_ids=frozenset({"p-current"}),
                comment_post_ids=frozenset({"p-current"}),
            ),
        )

        self.assertEqual(["post-p-history", "comment-3"], [hit.doc_id for hit in hits])
        events = [event for event in self.logged_events if event["event"] == "retrieval_self_excluded"]
        self.assertEqual(1, len(events))
        self.assertEqual("comment", events[0]["channel"])
        self.assertEqual(
            ["post-p-current", "post-vision-p-current", "comment-1", "comment-2"],
            events[0]["excluded_doc_ids"],
        )

    def test_document_exclusion_filters_chat_window_but_keeps_older_chat_messages(self) -> None:
        vectorstore.query_documents = lambda query, n_results=20, where=None: [
            vectorstore.VectorDocHit(
                "chat-1",
                "chat",
                "1",
                1,
                0.1,
                {"type": "chat", "thread_id": 7, "message_id": 1, "soul_name": "默认"},
            ),
            vectorstore.VectorDocHit(
                "chat-21",
                "chat",
                "21",
                2,
                0.2,
                {"type": "chat", "thread_id": 7, "message_id": 21, "soul_name": "默认"},
            ),
            vectorstore.VectorDocHit(
                "post-p-history",
                "post",
                "p-history",
                3,
                0.3,
                {"type": "post", "post_id": "p-history"},
            ),
        ]

        hits = retrieval.hybrid_search_documents(
            "你好",
            k=2,
            trace_context={"channel": "chat", "thread_id": 7},
            exclusion=retrieval.RetrievalExclusion(chat_message_ids=frozenset(range(2, 22))),
        )

        self.assertEqual(["chat-1", "post-p-history"], [hit.doc_id for hit in hits])
        events = [event for event in self.logged_events if event["event"] == "retrieval_self_excluded"]
        self.assertEqual(["chat-21"], events[-1]["excluded_doc_ids"])


class RetrievalDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_vector_search_scored = retrieval.vector_search_scored
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        logging_service.init_logging({"enabled": True})

    def tearDown(self) -> None:
        logging_service.init_logging({"enabled": False})
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        retrieval.vector_search_scored = self.old_vector_search_scored
        self.tmp.cleanup()

    def insert_post(self, post_id: str, content: str, created_at: float) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-25T00:00:00+08:00", content, created_at, created_at),
        )

    def test_short_cjk_like_fallback_is_low_trust(self) -> None:
        self.insert_post("p-new-like", "我买了电脑", 3.0)
        self.insert_post("p-old-like", "我今天很累", 2.0)
        self.insert_post("p-vector", "长期疲惫和压力让我焦虑", 1.0)
        retrieval.vector_search_scored = lambda query, k=20: [
            retrieval.RetrievalHit("p-vector", "vector", 1, 0.1)
        ]

        hits = retrieval.hybrid_search_scored("我", k=3)

        self.assertEqual("p-vector", hits[0].post_id)
        like_hit = next(hit for hit in hits if hit.post_id == "p-new-like")
        self.assertIn("like_fallback", like_hit.reasons)
        self.assertIn("low_trust", like_hit.reasons)

    def test_long_cjk_fts_uses_window_candidates_to_hit_related_post(self) -> None:
        self.insert_post("p-library", "晚上在图书馆学习时，我的效率确实更高。", 2.0)
        self.insert_post("p-other", "今天只是随便散步。", 1.0)
        retrieval.vector_search_scored = lambda query, k=20: []

        hits = retrieval.fts_search_scored("我之前是不是说过晚上图书馆学习效率更高", k=5)

        self.assertIn("p-library", [hit.post_id for hit in hits])
        event = self._last_event("fts_query_built")
        self.assertEqual("posts", event["target"])
        self.assertEqual("posts_fts_trigram", event["table"])
        self.assertIn("图书馆", event["deterministic_candidates"])
        self.assertIn('"图书馆"', event["match"])

    def test_empty_or_symbol_only_fts_query_returns_empty(self) -> None:
        self.insert_post("p-1", "ChromaDB 和 FTS5。", 1.0)

        self.assertEqual([], retrieval.fts_search_scored('"""()^{}[]', k=5))

    def test_keyword_search_posts_uses_fts_order(self) -> None:
        self.insert_post("p-alpha", "alpha alpha alpha", 1.0)
        self.insert_post("p-beta", "alpha", 2.0)

        hits = retrieval.keyword_search_posts("alpha", k=2)

        self.assertEqual(["p-alpha", "p-beta"], hits)

    def test_keyword_search_posts_returns_empty_when_no_hits(self) -> None:
        self.insert_post("p-1", "ChromaDB 和 FTS5。", 1.0)

        self.assertEqual([], retrieval.keyword_search_posts("不存在的内容", k=5))

    def test_keyword_search_posts_short_cjk_uses_like_fallback(self) -> None:
        self.insert_post("p-new-like", "我买了电脑", 3.0)
        self.insert_post("p-old-like", "我今天很累", 2.0)

        hits = retrieval.keyword_search_posts("我", k=5)

        self.assertEqual(["p-new-like", "p-old-like"], hits)

    def test_fts_keywords_can_drive_fts_search(self) -> None:
        self.insert_post("p-library", "晚上在图书馆学习时，我的效率确实更高。", 2.0)
        self.insert_post("p-other", "今天只是随便散步。", 1.0)

        hits = retrieval.fts_search_scored(
            "完全不相关的原始查询",
            k=5,
            fts_keywords=["图书馆", "学习效率"],
        )

        self.assertIn("p-library", [hit.post_id for hit in hits])
        self.assertEqual("fts_rewrite", hits[0].source)
        event = self._last_event("fts_query_built")
        self.assertEqual(["图书馆", "学习效率"], event["fts_keywords"])
        self.assertEqual(["图书馆", "学习效率"], event["keyword_candidates"])
        self.assertEqual("fts_rewrite", event["source"])

    def test_semantic_query_is_used_for_vector_search(self) -> None:
        captured: dict[str, str] = {}
        retrieval.vector_search_scored = lambda query, k=20: captured.setdefault("query", query) and []

        retrieval.hybrid_search_scored(
            "原始查询",
            k=3,
            semantic_query="改写后的语义查询",
            fts_keywords=[],
        )

        self.assertEqual("改写后的语义查询", captured["query"])
        event = self._last_event("hybrid_retrieval_result")
        self.assertEqual("原始查询", event["raw_query"])
        self.assertEqual("改写后的语义查询", event["semantic_query"])
        self.assertEqual([], event["fts_keywords"])

    def _last_event(self, event_name: str) -> dict:
        current = self.workspace / "logs" / "current.jsonl"
        records = [
            json.loads(line)
            for line in current.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matches = [record for record in records if record.get("event") == event_name]
        self.assertTrue(matches)
        return matches[-1]


if __name__ == "__main__":
    unittest.main()
