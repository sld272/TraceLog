from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import db, memory_events_service as mes, memory_unit_service as mus
from core import memory_reconcile_runner as runner


class MemoryRelinkTest(unittest.TestCase):
    """The post-edit AI re-link pass: after a user edits a unit, its old links are
    held as review_pending and a narrow judge keeps the ones that still support
    the new content and drops the rest."""

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

    def _event(self, post_id: str, content: str = "证据", ts: float = 1.0) -> int:
        with db.transaction() as conn:
            return mes.record_post_mutation(
                conn, post_id=post_id, op="create", content=content, occurred_at=ts
            ).id

    def _unit(self, event_ids: list[int], content: str = "用户在准备考研") -> str:
        return mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content=content, evidence_event_ids=event_ids, confidence=0.7,
        )

    @staticmethod
    def _judge(keep: list[int], drop: list[int]):
        def judge(*, content: str, evidence: list[dict]) -> dict:
            return {"keep_event_ids": list(keep), "drop_event_ids": list(drop)}
        return judge

    def _effective_ids(self, unit_id: str) -> set[int]:
        return {int(r["id"]) for r in mus.current_effective_evidence_for_unit(unit_id)}

    def test_pending_not_claimed_as_support_until_judged(self) -> None:
        e1 = self._event("p1")
        unit_id = self._unit([e1])
        mus.update_unit(unit_id, content="用户改口：在找工作")
        # pending links must not be claimed as current support
        self.assertEqual(mus.current_effective_evidence_for_unit(unit_id), [])
        self.assertEqual(len(mus.list_pending_relinks()), 1)

    def test_all_relevant_keeps_links(self) -> None:
        e1, e2 = self._event("p1"), self._event("p2")
        unit_id = self._unit([e1, e2])
        mus.update_unit(unit_id, content="用户在准备考研，目标上海")
        result = runner.run_pending_relinks(None, "m", judge=self._judge([e1, e2], []))
        self.assertEqual(result.applied, 1)
        self.assertEqual(self._effective_ids(unit_id), {e1, e2})
        self.assertEqual(mus.list_pending_relinks(), [])

    def test_all_irrelevant_drops_links(self) -> None:
        e1, e2 = self._event("p1"), self._event("p2")
        unit_id = self._unit([e1, e2])
        mus.update_unit(unit_id, content="完全不相干的新内容")
        runner.run_pending_relinks(None, "m", judge=self._judge([], [e1, e2]))
        self.assertEqual(mus.get_unit_evidence(unit_id), [])
        self.assertEqual(mus.current_effective_evidence_for_unit(unit_id), [])
        self.assertEqual(mus.list_pending_relinks(), [])

    def test_partial_relevance_keeps_subset(self) -> None:
        e1, e2 = self._event("p1"), self._event("p2")
        unit_id = self._unit([e1, e2])
        mus.update_unit(unit_id, content="只和第一条有关了")
        # judge keeps only e1; anything not kept is dropped
        runner.run_pending_relinks(None, "m", judge=self._judge([e1], []))
        self.assertEqual(self._effective_ids(unit_id), {e1})
        self.assertEqual([e["id"] for e in mus.get_unit_evidence(unit_id)], [e1])

    def test_judge_failure_keeps_pending_and_loses_nothing(self) -> None:
        e1 = self._event("p1")
        unit_id = self._unit([e1])
        mus.update_unit(unit_id, content="新内容")

        def boom(*, content: str, evidence: list[dict]) -> dict:
            raise RuntimeError("LLM down")

        result = runner.run_pending_relinks(None, "m", judge=boom)
        self.assertEqual(result.applied, 0)
        self.assertEqual(len(result.failures), 1)
        # review still pending, link still pending, evidence not lost
        self.assertEqual(len(mus.list_pending_relinks()), 1)
        self.assertEqual([e["id"] for e in mus.get_unit_evidence(unit_id)], [e1])
        self.assertEqual(mus.current_effective_evidence_for_unit(unit_id), [])

    def test_reconcile_pass_reports_relink_failure_and_keeps_backlog(self) -> None:
        # a re-link judge failure must surface in the run result (so the job is
        # retried, not reported done) and leave the review pending as backlog.
        e1 = self._event("p1")
        unit_id = self._unit([e1])
        mus.update_unit(unit_id, content="新内容")

        def noop_producer(**kwargs):
            return {"ops": [], "summary": ""}

        def boom(*, content, evidence):
            raise RuntimeError("LLM down")

        result = runner.run_pending_reconcile(
            None, "m", op_producer=noop_producer, relink_judge=boom
        )
        self.assertTrue(result.relink_failures)
        self.assertTrue(result.has_pending_after_run)
        self.assertEqual(len(mus.list_pending_relinks()), 1)

    def test_stale_result_cannot_overwrite_newer_edit(self) -> None:
        e1 = self._event("p1")
        unit_id = self._unit([e1])
        mus.update_unit(unit_id, content="第一次编辑")
        stale = mus.list_pending_relinks()[0]
        stale_id = int(stale["relink_id"])
        stale_version = float(stale["updated_at"])

        # user edits again before the stale judge result is applied
        mus.update_unit(unit_id, content="第二次编辑")

        applied = mus.apply_relink(
            stale_id, unit_id, expected_version=stale_version,
            keep_event_ids=[e1], drop_event_ids=[],
        )
        self.assertFalse(applied)  # stale row no longer pending / version moved
        self.assertEqual(mus.get_unit(unit_id)["content"], "第二次编辑")
        # the link is still pending under the new edit's review, not silently kept
        self.assertEqual(mus.current_effective_evidence_for_unit(unit_id), [])
        self.assertEqual(len(mus.list_pending_relinks()), 1)


if __name__ == "__main__":
    unittest.main()
