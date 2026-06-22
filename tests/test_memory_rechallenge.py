from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_reconciler as recon,
    memory_unit_service as mus,
)
from core.app_services import job_service, post_mutation


class MemoryRechallengeTest(unittest.TestCase):
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

    def _post_event(self, post_id: str, op: str, content: str | None, ts: float) -> int:
        with db.transaction() as conn:
            return mes.record_post_mutation(
                conn,
                post_id=post_id,
                op=op,
                content=content,
                occurred_at=ts,
            ).id

    def _unit(self, event_ids: list[int], content: str = "用户在准备考研") -> str:
        return mus.add_unit(
            owner_scope="global",
            visibility_scope="public",
            source_channel="post",
            type="insight",
            content=content,
            confidence=0.8,
            tier="core",
            importance=0.8,
            evidence_event_ids=event_ids,
        )

    def _settle(self, event_id: int) -> None:
        with db.transaction() as conn:
            mes.advance_cursor(conn, "global", "public", event_id)

    def _mutate_and_challenge(
        self,
        post_id: str,
        op: str,
        content: str | None,
        ts: float,
    ) -> int:
        with db.transaction() as conn:
            event = mes.record_post_mutation(
                conn,
                post_id=post_id,
                op=op,
                content=content,
                occurred_at=ts,
            )
            mus.challenge_units_for_source(conn, event.id)
            return event.id

    def test_single_evidence_delete_retracts_without_llm(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        self._settle(create_id)
        delete_id = self._mutate_and_challenge("p1", "delete", None, 2.0)

        def must_not_call(**kwargs):
            raise AssertionError("zero-evidence delete should be deterministic")

        summary = recon.reconcile_bucket(
            "global",
            "public",
            op_producer=must_not_call,
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertEqual(summary.by_op, {"retract": 1})
        self.assertEqual(mus.get_unit(unit_id)["status"], "retracted_by_model")
        self.assertEqual(mes.get_cursor("global", "public"), delete_id)
        self.assertEqual(
            db.query_one(
                "SELECT status FROM memory_unit_reconcile_queue WHERE unit_id = ?",
                (unit_id,),
            )["status"],
            "resolved",
        )

    def test_single_evidence_delete_keeps_user_authored_unit(self) -> None:
        # A user-authored belief stands on the user's own word: deleting the
        # source it once came from must NOT auto-retract it.
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="user",
            type="insight", content="用户亲述：在准备考研", source="user_authored",
            confidence=0.95, tier="core", importance=0.8, evidence_event_ids=[create_id],
        )
        self._settle(create_id)
        self._mutate_and_challenge("p1", "delete", None, 2.0)

        def must_not_call(**kwargs):
            raise AssertionError("zero-evidence delete should be deterministic")

        summary = recon.reconcile_bucket(
            "global",
            "public",
            op_producer=must_not_call,
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertEqual(summary.by_op, {"retain": 1})
        self.assertEqual(mus.get_unit(unit_id)["status"], "active")

    def test_delete_one_of_two_sources_requires_llm_decision(self) -> None:
        first = self._post_event("p1", "create", "我在准备考研", 1.0)
        second = self._post_event("p2", "create", "我一直在复习", 2.0)
        unit_id = self._unit([first, second])
        self._settle(second)
        self._mutate_and_challenge("p1", "delete", None, 3.0)
        captured: dict = {}

        def producer(**kwargs):
            captured.update(kwargs)
            return {"ops": [{"op": "retain", "target_id": unit_id}], "summary": "仍有支撑"}

        recon.reconcile_bucket(
            "global",
            "public",
            op_producer=producer,
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertEqual(mus.get_unit(unit_id)["status"], "active")
        challenged = next(item for item in captured["active_units"] if item["id"] == unit_id)
        self.assertEqual(challenged["status"], "challenged")
        self.assertEqual(
            [item["source_id"] for item in challenged["current_evidence"]],
            ["p2"],
        )

    def test_unrelated_edit_retracts_old_unit_and_adds_new_one(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        old_unit = self._unit([create_id])
        self._settle(create_id)
        edit_id = self._mutate_and_challenge("p1", "edit", "我最近开始学习吉他", 2.0)

        def producer(**kwargs):
            self.assertEqual([event["id"] for event in kwargs["events"]], [edit_id])
            return {
                "ops": [
                    {"op": "retract", "target_id": old_unit, "reason": "outdated"},
                    {
                        "op": "add",
                        "type": "preference",
                        "content": "用户最近开始学习吉他",
                        "confidence": 0.7,
                        "importance": 0.6,
                        "tier": "contextual",
                        "evidence_event_ids": [edit_id],
                    },
                ]
            }

        recon.reconcile_bucket(
            "global",
            "public",
            op_producer=producer,
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertEqual(mus.get_unit(old_unit)["status"], "retracted_by_model")
        active = mus.list_active_units_in_bucket("global", "public")
        self.assertEqual([row["content"] for row in active], ["用户最近开始学习吉他"])

    def test_edit_can_confirm_old_unit_with_latest_revision(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        self._settle(create_id)
        edit_id = self._mutate_and_challenge("p1", "edit", "我仍然在准备考研", 2.0)

        recon.reconcile_bucket(
            "global",
            "public",
            op_producer=lambda **kwargs: {
                "ops": [
                    {
                        "op": "confirm",
                        "target_id": unit_id,
                        "evidence_event_ids": [edit_id],
                        "confidence": 0.9,
                    }
                ]
            },
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertEqual(mus.get_unit(unit_id)["status"], "active")
        self.assertIn(edit_id, [int(row["id"]) for row in mus.get_unit_evidence(unit_id)])

    def test_edit_can_revise_old_unit(self) -> None:
        create_id = self._post_event("p1", "create", "我要考北京的研究生", 1.0)
        unit_id = self._unit([create_id], content="用户计划考北京的研究生")
        self._settle(create_id)
        edit_id = self._mutate_and_challenge("p1", "edit", "我要考上海的研究生", 2.0)

        recon.reconcile_bucket(
            "global",
            "public",
            op_producer=lambda **kwargs: {
                "ops": [
                    {
                        "op": "revise",
                        "target_id": unit_id,
                        "content": "用户计划考上海的研究生",
                        "evidence_event_ids": [edit_id],
                    }
                ]
            },
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertEqual(mus.get_unit(unit_id)["status"], "active")
        self.assertEqual(mus.get_unit(unit_id)["content"], "用户计划考上海的研究生")

    def test_missing_challenged_decision_keeps_cursor_and_queue_pending(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        self._settle(create_id)
        edit_id = self._mutate_and_challenge("p1", "edit", "我改学吉他", 2.0)

        with self.assertRaises(recon.ReconcileReviewError):
            recon.reconcile_bucket(
                "global",
                "public",
                op_producer=lambda **kwargs: {"ops": []},
                reflection_type=recon.RECONCILE_GLOBAL,
            )

        self.assertEqual(mes.get_cursor("global", "public"), create_id)
        self.assertEqual(mus.get_unit(unit_id)["status"], "challenged")
        self.assertEqual(
            db.query_one(
                "SELECT status FROM memory_unit_reconcile_queue WHERE trigger_event_id = ?",
                (edit_id,),
            )["status"],
            "pending",
        )

    def test_duplicate_challenged_decision_is_rejected(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        self._settle(create_id)
        self._mutate_and_challenge("p1", "edit", "我仍在准备考研", 2.0)

        with self.assertRaises(recon.ReconcileReviewError):
            recon.reconcile_bucket(
                "global",
                "public",
                op_producer=lambda **kwargs: {
                    "ops": [
                        {"op": "retain", "target_id": unit_id},
                        {"op": "retract", "target_id": unit_id, "reason": "outdated"},
                    ]
                },
                reflection_type=recon.RECONCILE_GLOBAL,
            )

    def test_source_revision_change_during_llm_aborts_old_result(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        self._settle(create_id)
        self._mutate_and_challenge("p1", "edit", "我改学吉他", 2.0)

        def racing_producer(**kwargs):
            self._mutate_and_challenge("p1", "edit", "我改学钢琴", 3.0)
            return {"ops": [{"op": "retain", "target_id": unit_id}]}

        result = recon.reconcile_bucket(
            "global",
            "public",
            op_producer=racing_producer,
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertIsNone(result)
        self.assertEqual(mus.get_unit(unit_id)["status"], "challenged")
        self.assertEqual(
            db.query_one(
                "SELECT COUNT(*) AS n FROM memory_unit_reconcile_queue "
                "WHERE unit_id = ? AND status = 'pending'",
                (unit_id,),
            )["n"],
            2,
        )

    def test_new_evidence_revision_change_during_llm_blocks_stale_add(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)

        def racing_producer(**kwargs):
            self._post_event("p1", "edit", "我开始学习吉他", 2.0)
            return {
                "ops": [
                    {
                        "op": "add",
                        "type": "goal",
                        "content": "用户在准备考研",
                        "importance": 0.8,
                        "evidence_event_ids": [create_id],
                    }
                ]
            }

        result = recon.reconcile_bucket(
            "global",
            "public",
            op_producer=racing_producer,
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertIsNone(result)
        self.assertEqual(mes.get_cursor("global", "public"), 0)
        self.assertEqual(mus.list_active_units_in_bucket("global", "public"), [])

    def test_challenged_current_evidence_is_raw_fallback_before_cursor(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        edit_id = self._mutate_and_challenge("p1", "edit", "我最近开始学习吉他", 2.0)
        self._settle(edit_id)

        items, _ = memory_read.freshness_seam(
            "public_post",
            None,
            now=db.now_ts(),
            query="吉他",
        )

        self.assertEqual([item.content for item in items], ["我最近开始学习吉他"])
        self.assertTrue(items[0].reviewing)
        self.assertEqual(memory_read.retrieve_units("考研", "public_post", None), [])
        self.assertEqual(mus.get_unit(unit_id)["status"], "challenged")

    def test_resolved_review_exits_challenged_raw_fallback(self) -> None:
        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        self._settle(create_id)
        self._mutate_and_challenge("p1", "edit", "我仍在准备考研", 2.0)

        recon.reconcile_bucket(
            "global",
            "public",
            op_producer=lambda **kwargs: {
                "ops": [{"op": "retain", "target_id": unit_id}]
            },
            reflection_type=recon.RECONCILE_GLOBAL,
        )
        items, _ = memory_read.freshness_seam(
            "public_post",
            None,
            now=db.now_ts(),
        )
        self.assertEqual(items, [])

    def test_deleted_and_superseded_text_never_appear_in_raw_fallback(self) -> None:
        create_id = self._post_event("p1", "create", "旧内容考研", 1.0)
        self._unit([create_id])
        edit_id = self._mutate_and_challenge("p1", "edit", "新内容吉他", 2.0)
        self._settle(edit_id)

        items, _ = memory_read.freshness_seam("public_post", None, now=db.now_ts())
        self.assertNotIn("旧内容考研", [item.content for item in items])
        self.assertIn("新内容吉他", [item.content for item in items])

        delete_id = self._mutate_and_challenge("p1", "delete", None, 3.0)
        self._settle(delete_id)
        items, _ = memory_read.freshness_seam("public_post", None, now=db.now_ts())
        self.assertNotIn("旧内容考研", [item.content for item in items])
        self.assertNotIn("新内容吉他", [item.content for item in items])

    def test_unit_detail_marks_evidence_revision_state(self) -> None:
        create_id = self._post_event("p1", "create", "旧内容", 1.0)
        unit_id = self._unit([create_id])
        edit_id = self._mutate_and_challenge("p1", "edit", "新内容", 2.0)
        mus.confirm_unit(unit_id, evidence_event_ids=[edit_id])

        detail = memory_read.unit_detail(unit_id)

        states = {item.event_id: item.state for item in detail.evidence}
        self.assertEqual(states[create_id], "superseded")
        self.assertEqual(states[edit_id], "current")

    def test_create_then_edit_before_first_reconcile_only_sends_latest(self) -> None:
        create_id = self._post_event("p1", "create", "旧内容", 1.0)
        edit_id = self._post_event("p1", "edit", "新内容", 2.0)
        captured: list[int] = []

        recon.reconcile_bucket(
            "global",
            "public",
            op_producer=lambda **kwargs: (
                captured.extend(int(event["id"]) for event in kwargs["events"])
                or {"ops": []}
            ),
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertEqual(captured, [edit_id])
        self.assertNotIn(create_id, captured)

    def test_create_then_delete_before_first_reconcile_calls_no_llm(self) -> None:
        self._post_event("p1", "create", "会删除", 1.0)
        delete_id = self._post_event("p1", "delete", None, 2.0)

        summary = recon.reconcile_bucket(
            "global",
            "public",
            op_producer=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("deleted source must not reach LLM")
            ),
            reflection_type=recon.RECONCILE_GLOBAL,
        )

        self.assertIsNone(summary)
        self.assertEqual(mes.get_cursor("global", "public"), delete_id)

    def test_stale_portrait_does_not_expose_challenged_claim(self) -> None:
        from core import memory_view_service as mvs

        create_id = self._post_event("p1", "create", "我在准备考研", 1.0)
        unit_id = self._unit([create_id])
        mus.confirm_unit(unit_id, evidence_event_ids=[create_id], confidence=0.9)
        mvs.recompute_slice("global", "public")
        mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        self.assertIn(
            "用户在准备考研",
            mvs.read_portrait_body("global", "public", mvs.VIEW_USER_MD),
        )

        self._mutate_and_challenge("p1", "edit", "我开始学习吉他", 2.0)

        self.assertEqual(
            mvs.get_view("global", "public", mvs.VIEW_USER_MD)["status"],
            "stale",
        )
        self.assertNotIn(
            "用户在准备考研",
            mvs.read_portrait_body("global", "public", mvs.VIEW_USER_MD),
        )


class PostMutationRechallengeTest(unittest.TestCase):
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

    def test_edit_post_challenges_unit_and_enqueues_reconcile(self) -> None:
        from core import record_service

        post_id = record_service.save_post(
            "我在准备考研",
            index_immediately=False,
            track_embedding=False,
        )
        event = mes.latest_source_event("post", post_id)
        unit_id = mus.add_unit(
            owner_scope="global",
            visibility_scope="public",
            source_channel="post",
            type="goal",
            content="用户在准备考研",
            evidence_event_ids=[int(event["id"])],
        )
        with (
            patch.dict(os.environ, {memory_read.WRITE_MODE_ENV: "reconcile"}),
            patch("core.record_service.index_post_embedding"),
        ):
            result = post_mutation.edit_post(post_id, "我开始学习吉他")

        self.assertEqual(result.content, "我开始学习吉他")
        self.assertEqual(mus.get_unit(unit_id)["status"], "challenged")
        self.assertEqual(
            [row["type"] for row in db.query_all("SELECT type FROM jobs ORDER BY id")],
            [job_service.TYPE_RUN_MEMORY_RECONCILE],
        )
