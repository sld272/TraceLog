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
from core.llm import memory_router


class TombstoneFormatTest(unittest.TestCase):
    def test_format_prefers_normalized_claim_over_raw_content(self) -> None:
        text = producer_mod._format_tombstones([
            {"retraction_reason": "false", "content": "咖啡这种东西我可太讨厌了",
             "normalized_claim": "用户讨厌咖啡"},
            {"retraction_reason": "outdated", "content": "在准备期末考", "normalized_claim": None},
        ])
        self.assertIn("用户讨厌咖啡", text)
        self.assertNotIn("可太讨厌", text)      # raw wording replaced by the claim
        self.assertIn("在准备期末考", text)      # claimless rows fall back to content
        self.assertIn("reason=false", text)
        self.assertIn("reason=outdated", text)


class NormalizeClaimsParserTest(unittest.TestCase):
    def test_parser_extracts_claims_and_drops_malformed(self) -> None:
        raw = json.dumps({"claims": [
            {"unit_id": "mu_1", "claim": "用户讨厌咖啡"},
            {"unit_id": "", "claim": "无 id"},
            {"unit_id": "mu_2", "claim": ""},
            "not-a-dict",
        ]})
        parsed = memory_router._parse_normalize_claims_content(raw)
        self.assertEqual(parsed, {"mu_1": "用户讨厌咖啡"})

    def test_parser_rejects_non_json(self) -> None:
        self.assertIsNone(memory_router._parse_normalize_claims_content("胡言乱语"))


class ReconcileParserTest(unittest.TestCase):
    def test_parser_normalizes_ops(self) -> None:
        raw = json.dumps({
            "summary": "本轮发现一个目标",
            "ops": [
                {"op": "add", "type": "goal", "content": " 考研 ", "confidence": 1.5,
                 "tier": "core", "importance": 0.8, "evidence_event_ids": [1, "2", "x"]},
                {"op": "retain", "target_id": "mu_review"},
                {"op": "confirm", "target_id": "mu_a", "evidence_event_ids": [3]},
                {"op": "revise", "target_id": "mu_b", "content": "改了"},
                {"op": "retract", "target_id": "mu_c", "reason": "outdated"},
                {"op": "bogus", "content": "应被丢弃"},
                "not a dict",
            ],
        })
        parsed = memory_router._parse_memory_reconcile_content(raw)
        ops = parsed["ops"]
        self.assertEqual(parsed["summary"], "本轮发现一个目标")
        self.assertEqual(len(ops), 5)  # bogus + non-dict dropped

        add = ops[0]
        self.assertEqual(add["type"], "insight")
        self.assertEqual(add["content"], "考研")  # trimmed
        self.assertEqual(add["confidence"], 1.0)  # clamped
        self.assertEqual(add["evidence_event_ids"], [1, 2])  # "x" dropped

        self.assertEqual(ops[1], {
            "op": "retain",
            "evidence_event_ids": [],
            "target_id": "mu_review",
        })
        self.assertEqual(ops[4]["reason"], "outdated")

    def test_parser_coerces_unknown_type_to_insight(self) -> None:
        raw = json.dumps({"ops": [
            {"op": "add", "type": "long_term_goal", "content": "x", "evidence_event_ids": [1]}
        ]})
        parsed = memory_router._parse_memory_reconcile_content(raw)
        self.assertEqual(parsed["ops"][0]["type"], "insight")

    def test_parser_rejects_non_json(self) -> None:
        self.assertIsNone(memory_router._parse_memory_reconcile_content("not json"))

    def test_parser_handles_missing_ops(self) -> None:
        parsed = memory_router._parse_memory_reconcile_content(json.dumps({"summary": "无"}))
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

        with patch.object(memory_router, "call_memory_reconcile", fake_call):
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
        # route A public-relationship scene (soul:X, public): soul appears as
        # context, relationship-only, with an explicit "don't describe the soul".
        comment = producer_mod.describe_scene(
            {"owner_scope": "soul:喜多郁代", "visibility_scope": "public"}
        )
        self.assertIn("喜多郁代", comment)
        self.assertIn("相处", comment)
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
        with patch.object(memory_router, "call_memory_reconcile", lambda *a, **k: None):
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
            return memory_router._parse_memory_reconcile_content(raw)

        with patch.object(memory_router, "call_memory_reconcile", fake_call):
            producer = producer_mod.make_llm_op_producer(client=object(), model="m")
            summary = recon.reconcile_bucket(
                "global", "public", op_producer=producer, run_type=recon.RECONCILE_GLOBAL
            )

        self.assertEqual(summary.applied, 1)
        units = mus.list_active_units_in_bucket("global", "public")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["type"], "insight")
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
                "global", "public", op_producer=failing_producer, run_type=recon.RECONCILE_GLOBAL
            )

        self.assertEqual(mes.get_cursor("global", "public"), cursor_before)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 0)

    def test_comment_reconcile_receives_assistant_dialogue_as_context_only(self) -> None:
        now = db.now_ts()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at) "
                "VALUES ('luna', 'souls/luna.md', 1, 0, ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO posts(id, ts, content, created_at, updated_at) "
                "VALUES ('p1', 't', '原帖', ?, ?)",
                (now, now),
            )
            root = conn.execute(
                "INSERT INTO comments(post_id, soul_name, role, content, seq, created_at) "
                "VALUES ('p1', 'luna', 'assistant', '我先不讲大道理。', 0, ?)",
                (now,),
            )
            user = conn.execute(
                "INSERT INTO comments(post_id, soul_name, role, content, seq, created_at) "
                "VALUES ('p1', 'luna', 'user', '对，你又开始懂我了。', 1, ?)",
                (now + 1,),
            )
            mes.record_comment_mutation(
                conn,
                comment_id=int(root.lastrowid),
                post_id="p1",
                soul_name="luna",
                role="assistant",
                op="create",
                content="我先不讲大道理。",
                occurred_at=now,
            )
            mes.record_comment_mutation(
                conn,
                comment_id=int(user.lastrowid),
                post_id="p1",
                soul_name="luna",
                role="user",
                op="create",
                content="对，你又开始懂我了。",
                occurred_at=now + 1,
            )

        captured = {}

        def producer(*, boundary, events, active_units, tombstones):
            captured["events"] = events
            return {"summary": "no-op", "ops": []}

        recon.reconcile_bucket(
            "global",
            "public",
            op_producer=producer,
            run_type=recon.RECONCILE_GLOBAL,
        )
        self.assertEqual(len(captured["events"]), 1)
        context = captured["events"][0]["conversation_context"]
        self.assertEqual([item["role"] for item in context], ["assistant", "user"])
        self.assertIn("不讲大道理", context[0]["content"])
        self.assertNotIn("event_id", context[0])

    def test_concurrent_cursor_advance_aborts(self) -> None:
        # If another runner advances the cursor while our op-producer runs, the
        # CAS check must abort our commit so we don't create duplicate units.
        with db.transaction() as conn:
            e1 = mes.record_post_mutation(conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0).id

        def racing_producer(*, boundary, events, active_units, tombstones):
            with db.immediate_transaction() as conn:
                mes.advance_cursor(conn, "global", "public", e1)
            return {"summary": "s", "ops": [{
                "op": "add", "type": "goal", "content": "用户在准备考研",
                "confidence": 0.9, "tier": "core", "importance": 0.85, "evidence_event_ids": [e1],
            }]}

        summary = recon.reconcile_bucket(
            "global", "public", op_producer=racing_producer, run_type=recon.RECONCILE_GLOBAL
        )
        self.assertIsNone(summary)
        self.assertEqual(len(mus.list_active_units_in_bucket("global", "public")), 0)


if __name__ == "__main__":
    unittest.main()
