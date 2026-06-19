from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_reconcile_producer as producer_mod,
    memory_reconciler as recon,
    memory_unit_service as mus,
)
from core.llm import reflection_router


class ReconcileParserTest(unittest.TestCase):
    def test_parser_normalizes_ops(self) -> None:
        raw = json.dumps({
            "summary": "本轮发现一个目标",
            "ops": [
                {"op": "add", "type": "goal", "content": " 考研 ", "confidence": 1.5,
                 "tier": "core", "importance": 0.8, "evidence_event_ids": [1, "2", "x"]},
                {"op": "confirm", "target_id": "mu_a", "evidence_event_ids": [3]},
                {"op": "revise", "target_id": "mu_b", "content": "改了"},
                {"op": "retract", "target_id": "mu_c", "reason": "outdated"},
                {"op": "bogus", "content": "应被丢弃"},
                "not a dict",
            ],
        })
        parsed = reflection_router._parse_memory_reconcile_content(raw)
        ops = parsed["ops"]
        self.assertEqual(parsed["summary"], "本轮发现一个目标")
        self.assertEqual(len(ops), 4)  # bogus + non-dict dropped

        add = ops[0]
        self.assertEqual(add["type"], "goal")
        self.assertEqual(add["content"], "考研")  # trimmed
        self.assertEqual(add["confidence"], 1.0)  # clamped
        self.assertEqual(add["evidence_event_ids"], [1, 2])  # "x" dropped

        self.assertEqual(ops[3]["reason"], "outdated")

    def test_parser_coerces_unknown_type_to_insight(self) -> None:
        raw = json.dumps({"ops": [
            {"op": "add", "type": "long_term_goal", "content": "x", "evidence_event_ids": [1]}
        ]})
        parsed = reflection_router._parse_memory_reconcile_content(raw)
        self.assertEqual(parsed["ops"][0]["type"], "insight")

    def test_parser_rejects_non_json(self) -> None:
        self.assertIsNone(reflection_router._parse_memory_reconcile_content("not json"))

    def test_parser_handles_missing_ops(self) -> None:
        parsed = reflection_router._parse_memory_reconcile_content(json.dumps({"summary": "无"}))
        self.assertEqual(parsed["ops"], [])


class ReconcileProducerTest(unittest.TestCase):
    def test_producer_formats_inputs_and_passes_through(self) -> None:
        captured = {}

        def fake_call(client, model, *, boundary_text, events_text, active_units_text, tombstones_text, trace_context=None):
            captured["boundary_text"] = boundary_text
            captured["events_text"] = events_text
            captured["units_text"] = active_units_text
            captured["tombstones_text"] = tombstones_text
            return {"ops": [{"op": "add"}], "summary": "s"}

        with patch.object(reflection_router, "call_memory_reconcile", fake_call):
            producer = producer_mod.make_llm_op_producer(client=object(), model="m")
            result = producer(
                boundary={"owner_scope": "global", "visibility_scope": "public"},
                events=[{"id": 7, "source_channel": "post", "op": "create",
                         "source_type": "post", "source_id": "p1", "content_snapshot": "考研倒计时"}],
                active_units=[{"id": "mu_x", "type": "goal", "content": "已有目标", "confidence": 0.6, "tier": "core"}],
                tombstones=[{"retraction_reason": "false", "content": "错误信念"}],
            )

        self.assertIn("event_id=7", captured["events_text"])
        self.assertIn("考研倒计时", captured["events_text"])
        self.assertIn("【用户】", captured["events_text"])  # events labelled as user-authored
        self.assertIn("unit_id=mu_x", captured["units_text"])
        self.assertIn("false", captured["tombstones_text"])
        # scene is natural language, never the raw owner label that confused the model
        self.assertNotIn("owner_scope", captured["boundary_text"])
        self.assertIn("用户", captured["boundary_text"])
        self.assertEqual(result["summary"], "s")

    def test_describe_scene_keeps_soul_as_context_not_subject(self) -> None:
        # comment scene: soul appears as context, with an explicit "don't describe the soul" instruction
        comment = producer_mod.describe_scene(
            {"owner_scope": "soul:喜多郁代", "visibility_scope": "thread:20260616-001"}
        )
        self.assertIn("喜多郁代", comment)
        self.assertIn("用户", comment)
        self.assertIn("绝不要描述", comment)

        post = producer_mod.describe_scene({"owner_scope": "global", "visibility_scope": "public"})
        self.assertIn("用户本人", post)
        self.assertNotIn("绝不要描述", post)  # no soul to confuse here

        private = producer_mod.describe_scene(
            {"owner_scope": "soul:luna", "visibility_scope": "private:soul:luna"}
        )
        self.assertIn("私聊", private)
        self.assertIn("luna", private)

    def test_producer_raises_on_none(self) -> None:
        # A failed LLM call (None) must surface as an error, not a silent empty
        # batch — collapsing it to {"ops": []} would advance the cursor and drop
        # this evidence forever.
        with patch.object(reflection_router, "call_memory_reconcile", lambda *a, **k: None):
            producer = producer_mod.make_llm_op_producer(client=object(), model="m")
            with self.assertRaises(producer_mod.ReconcileProducerError):
                producer(boundary={"owner_scope": "global", "visibility_scope": "public"},
                         events=[], active_units=[], tombstones=[])


class ReconcileEndToEndWithMockedLLMTest(unittest.TestCase):
    """LLM-shaped JSON -> parser -> producer -> reconcile -> a real unit."""

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

    def test_full_chain_creates_unit(self) -> None:
        with db.transaction() as conn:
            e1 = mes.record_post_mutation(conn, post_id="p1", op="create", content="今天又在背单词", occurred_at=1.0).id
            e2 = mes.record_post_mutation(conn, post_id="p2", op="create", content="模拟考没考好，但不想放弃", occurred_at=2.0).id

        def fake_call(client, model, *, boundary_text, events_text, active_units_text, tombstones_text, trace_context=None):
            # emulate the LLM: cite the two real events it was shown
            ids = [int(tok.split("event_id=")[1].split(" ")[0]) for tok in events_text.split("- ") if "event_id=" in tok]
            raw = json.dumps({
                "summary": "用户在备考且坚持",
                "ops": [{"op": "add", "type": "goal", "content": "用户在准备考研，焦虑但坚持",
                         "confidence": 0.75, "tier": "core", "importance": 0.85, "evidence_event_ids": ids}],
            })
            return reflection_router._parse_memory_reconcile_content(raw)

        with patch.object(reflection_router, "call_memory_reconcile", fake_call):
            producer = producer_mod.make_llm_op_producer(client=object(), model="m")
            summary = recon.reconcile_bucket(
                "global", "public", op_producer=producer, reflection_type=recon.RECONCILE_GLOBAL
            )

        self.assertEqual(summary.applied, 1)
        units = mus.list_active_units_in_bucket("global", "public")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["content"], "用户在准备考研，焦虑但坚持")
        self.assertEqual({e["id"] for e in mus.get_unit_evidence(units[0]["id"])}, {e1, e2})

    def test_failed_producer_does_not_advance_cursor(self) -> None:
        # On producer failure the batch must stay unconsumed: cursor unchanged,
        # no units written. This is the regression guard for silent data loss.
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="今天又在背单词", occurred_at=1.0)
        cursor_before = mes.get_cursor("global", "public")

        def failing_producer(**kwargs):
            raise producer_mod.ReconcileProducerError("boom")

        with self.assertRaises(producer_mod.ReconcileProducerError):
            recon.reconcile_bucket(
                "global", "public", op_producer=failing_producer, reflection_type=recon.RECONCILE_GLOBAL
            )

        self.assertEqual(mes.get_cursor("global", "public"), cursor_before)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 0)


if __name__ == "__main__":
    unittest.main()
