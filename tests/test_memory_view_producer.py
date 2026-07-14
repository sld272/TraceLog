from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_unit_service as mus,
    memory_view_producer as vproducer,
    memory_view_service as mvs,
    soul_relationship_memory as srm,
)
from core.llm import memory_router


class ViewSynthParserTest(unittest.TestCase):
    """The model may group ids, but only exact source unit text reaches users."""

    def _parse(self, obj, *, unit_contents, char_budget=1000):
        raw = obj if isinstance(obj, str) else json.dumps(obj)
        return memory_router._parse_view_synthesis_content(
            raw, unit_contents=unit_contents, char_budget=char_budget
        )

    def test_renders_selected_units_blank_line_joined(self) -> None:
        out = self._parse(
            {"paragraphs": [
                {"unit_ids": ["mu_a"]},
                {"unit_ids": ["mu_b"]},
            ]},
            unit_contents={"mu_a": "用户在准备考研", "mu_b": "偏好安静的学习环境。"},
        )
        self.assertEqual(out, "用户在准备考研。\n\n偏好安静的学习环境。")

    def test_model_text_is_ignored_even_with_legal_id(self) -> None:
        out = self._parse(
            {"paragraphs": [
                {"text": "用户每天清晨跑十公里。", "unit_ids": ["mu_a"]},
            ]},
            unit_contents={"mu_a": "用户正在准备考研"},
        )
        self.assertEqual(out, "用户正在准备考研。")
        self.assertNotIn("跑十公里", out)

    def test_unknown_empty_and_repeated_ids_are_ignored(self) -> None:
        out = self._parse(
            {"paragraphs": [
                {"unit_ids": ["mu_x"]},
                {"unit_ids": []},
                {"unit_ids": ["mu_a", "mu_a"]},
                {"unit_ids": ["mu_a", "mu_b"]},
            ]},
            unit_contents={"mu_a": "事实 A", "mu_b": "事实 B"},
        )
        self.assertEqual(out, "事实 A。\n\n事实 B。")

    def test_all_dropped_returns_none(self) -> None:
        self.assertIsNone(self._parse(
            {"paragraphs": [
                {"unit_ids": ["mu_x"]},
                {"unit_ids": []},
            ]},
            unit_contents={"mu_a": "事实 A"},
        ))

    def test_over_budget_drops_whole_trailing_paragraph(self) -> None:
        out = self._parse(
            {"paragraphs": [
                {"unit_ids": ["mu_a"]},
                {"unit_ids": ["mu_b"]},
                {"unit_ids": ["mu_c"]},
            ]},
            unit_contents={"mu_a": "AAAA", "mu_b": "BBBB", "mu_c": "CCCC"},
            char_budget=14,  # "AAAA。\n\nBBBB。" = 12 fits; next paragraph overflows
        )
        self.assertEqual(out, "AAAA。\n\nBBBB。")

    def test_rejects_non_paragraph_shapes(self) -> None:
        units = {"mu_a": "事实 A"}
        self.assertIsNone(self._parse("not json", unit_contents=units))
        self.assertIsNone(self._parse({"profile_md": "旧格式"}, unit_contents=units))
        self.assertIsNone(self._parse({"paragraphs": "x"}, unit_contents=units))
        self.assertIsNone(self._parse({"paragraphs": []}, unit_contents=units))


class ViewSynthProducerTest(unittest.TestCase):
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

    def _event(self) -> int:
        self._seq += 1
        with db.transaction() as conn:
            return mes.record_post_mutation(conn, post_id=f"p{self._seq}", op="create", content="e", occurred_at=float(self._seq)).id

    def _core_unit(self, content: str) -> str:
        uid = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="identity", content=content, confidence=0.9, tier="core",
            importance=0.85, evidence_event_ids=[self._event()],
        )
        mus.confirm_unit(uid, evidence_event_ids=[self._event()], confidence=0.9)
        return uid

    def _seed_core_units(self, n: int) -> None:
        for i in range(n):
            self._core_unit(f"用户核心事实{i}")

    def test_below_threshold_returns_none_without_llm(self) -> None:
        # Gate 1: a single core unit is too thin for synthesis; the LLM must not
        # be called and the deterministic template takes over.
        self._core_unit("我是一名考研学生")
        calls = {"n": 0}

        def fake_call(*a, **k):
            calls["n"] += 1
            return "不应被调用"

        with patch.object(memory_router, "call_view_synthesis", fake_call):
            synthesizer = vproducer.make_llm_synthesizer(object(), "m", mvs.VIEW_USER_PORTRAIT)
            view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_PORTRAIT, synthesizer=synthesizer)

        self.assertEqual(calls["n"], 0)
        self.assertTrue(view.used_fallback)
        self.assertIn("## 身份", view.content_md)

    def test_at_threshold_calls_llm_with_id_anchors(self) -> None:
        self._seed_core_units(vproducer.MIN_UNITS_FOR_LLM)
        captured = {}

        def fake_call(client, model, *, units_text, char_budget, view_type, unit_contents, trace_context=None):
            captured["units_text"] = units_text
            captured["unit_contents"] = unit_contents
            captured["budget"] = char_budget
            return "综合画像：用户专注考研。"

        with patch.object(memory_router, "call_view_synthesis", fake_call):
            synthesizer = vproducer.make_llm_synthesizer(object(), "m", mvs.VIEW_USER_PORTRAIT)
            view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_PORTRAIT, synthesizer=synthesizer)

        self.assertFalse(view.used_fallback)
        self.assertIn("综合画像", view.content_md)
        self.assertEqual(len(captured["unit_contents"]), vproducer.MIN_UNITS_FOR_LLM)
        self.assertIn("[id=mu_", captured["units_text"])
        self.assertEqual(captured["budget"], mvs.USER_PORTRAIT_CHAR_BUDGET)

    def test_falls_back_to_template_when_llm_returns_none(self) -> None:
        self._seed_core_units(vproducer.MIN_UNITS_FOR_LLM)
        with patch.object(memory_router, "call_view_synthesis", lambda *a, **k: None):
            synthesizer = vproducer.make_llm_synthesizer(object(), "m", mvs.VIEW_USER_PORTRAIT)
            view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_PORTRAIT, synthesizer=synthesizer)
        self.assertTrue(view.used_fallback)
        self.assertIn("## 身份", view.content_md)

    def test_refresh_builds_cross_bucket_relationship_view(self) -> None:
        # >= MIN_UNITS_FOR_LLM relationship units so gate 1 lets the LLM path run.
        for i in range(vproducer.MIN_UNITS_FOR_LLM):
            with db.transaction() as conn:
                event_id = mes.record_chat_mutation(
                    conn, message_id=90 + i, soul_name="luna", role="user",
                    op="create", content=f"互动{i}", occurred_at=float(i + 1),
                ).id
            mus.add_unit(
                owner_scope="soul:luna",
                visibility_scope="private:soul:luna",
                source_channel="chat",
                type="relationship",
                content=f"用户与 luna 的相处约定{i}",
                confidence=0.9,
                tier="core",
                importance=0.9,
                evidence_event_ids=[event_id],
            )
        captured = {}

        def fake_call(client, model, *, units_text, char_budget, view_type, unit_contents, trace_context=None):
            captured["units_text"] = units_text
            captured["view_type"] = view_type
            captured["unit_contents"] = unit_contents
            return "难过时，我们先安静陪伴。"

        with patch.object(memory_router, "call_view_synthesis", fake_call):
            results = vproducer.refresh_views_after_reconcile(object(), "m")

        self.assertEqual(1, len(results))
        self.assertEqual(mvs.VIEW_SOUL_RELATIONSHIP, captured["view_type"])
        self.assertIn("场景=私聊", captured["units_text"])
        self.assertEqual(len(captured["unit_contents"]), vproducer.MIN_UNITS_FOR_LLM)
        self.assertEqual("难过时，我们先安静陪伴。", srm.read_relationship_memory("luna"))


if __name__ == "__main__":
    unittest.main()
