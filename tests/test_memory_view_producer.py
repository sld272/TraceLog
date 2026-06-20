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
from core.llm import reflection_router


class ViewSynthParserTest(unittest.TestCase):
    def test_parser_extracts_profile_md(self) -> None:
        raw = json.dumps({"profile_md": "  这是画像。  "})
        self.assertEqual(reflection_router._parse_view_synthesis_content(raw), "这是画像。")

    def test_parser_rejects_empty_or_bad(self) -> None:
        self.assertIsNone(reflection_router._parse_view_synthesis_content(json.dumps({"profile_md": "  "})))
        self.assertIsNone(reflection_router._parse_view_synthesis_content("not json"))
        self.assertIsNone(reflection_router._parse_view_synthesis_content(json.dumps({"x": 1})))


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

    def test_synthesizer_formats_units_and_returns_prose(self) -> None:
        self._core_unit("我是一名考研学生")
        captured = {}

        def fake_call(client, model, *, units_text, char_budget, view_type, trace_context=None):
            captured["units_text"] = units_text
            captured["budget"] = char_budget
            return "综合：用户是一名专注备考的研究生考生。"

        with patch.object(reflection_router, "call_view_synthesis", fake_call):
            synthesizer = vproducer.make_llm_synthesizer(object(), "m", mvs.VIEW_USER_MD)
            view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD, synthesizer=synthesizer)

        self.assertIn("我是一名考研学生", captured["units_text"])
        self.assertEqual(captured["budget"], mvs.USER_MD_CHAR_BUDGET)
        self.assertFalse(view.used_fallback)
        self.assertIn("专注备考", view.content_md)

    def test_falls_back_to_template_when_llm_returns_none(self) -> None:
        self._core_unit("我是研究生")
        with patch.object(reflection_router, "call_view_synthesis", lambda *a, **k: None):
            synthesizer = vproducer.make_llm_synthesizer(object(), "m", mvs.VIEW_USER_MD)
            view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD, synthesizer=synthesizer)
        self.assertTrue(view.used_fallback)
        self.assertIn("## 身份", view.content_md)

    def test_refresh_builds_cross_bucket_relationship_view(self) -> None:
        with db.transaction() as conn:
            event_id = mes.record_chat_mutation(
                conn,
                message_id=99,
                soul_name="luna",
                role="user",
                op="create",
                content="难过时先陪我",
                occurred_at=1.0,
            ).id
        mus.add_unit(
            owner_scope="soul:luna",
            visibility_scope="private:soul:luna",
            source_channel="chat",
            type="relationship",
            content="用户难过时希望 luna 先陪伴",
            confidence=0.9,
            tier="core",
            importance=0.9,
            evidence_event_ids=[event_id],
        )
        captured = {}

        def fake_call(client, model, *, units_text, char_budget, view_type, trace_context=None):
            captured["units_text"] = units_text
            captured["view_type"] = view_type
            return "难过时，我们先安静陪伴。"

        with patch.object(reflection_router, "call_view_synthesis", fake_call):
            results = vproducer.refresh_views_after_reconcile(object(), "m")

        self.assertEqual(1, len(results))
        self.assertEqual(mvs.VIEW_SOUL_RELATIONSHIP, captured["view_type"])
        self.assertIn("场景=私聊", captured["units_text"])
        self.assertEqual("难过时，我们先安静陪伴。", srm.read_relationship_memory("luna"))


if __name__ == "__main__":
    unittest.main()
