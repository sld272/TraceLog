from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, fts_query, retrieval


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

    def tearDown(self) -> None:
        retrieval.fts_search_scored = self.old_fts_search_scored
        retrieval.vector_search_scored = self.old_vector_search_scored
        retrieval._read_candidate_contents = self.old_read_candidate_contents

    def stub_search(
        self,
        *,
        fts_hits: list[retrieval.RetrievalHit],
        vector_hits: list[retrieval.RetrievalHit],
        contents: dict[str, str] | None = None,
    ) -> None:
        retrieval.fts_search_scored = lambda query, k=20: fts_hits
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

    def tearDown(self) -> None:
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

    def test_empty_or_symbol_only_fts_query_returns_empty(self) -> None:
        self.insert_post("p-1", "ChromaDB 和 FTS5。", 1.0)

        self.assertEqual([], retrieval.fts_search_scored('"""()^{}[]', k=5))

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


if __name__ == "__main__":
    unittest.main()
