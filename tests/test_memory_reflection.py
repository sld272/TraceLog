from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    memory_events_service as mes,
    memory_reflection as refl,
    memory_unit_service as mus,
)


class MemoryReflectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self.seq = 0

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _unit(
        self,
        content: str,
        *,
        tier: str = "contextual",
        confidence: float = 0.9,
        importance: float = 0.8,
        source: str = "reflected",
        type: str = "insight",
    ) -> str:
        self.seq += 1
        with db.transaction() as conn:
            ev = mes.record_post_mutation(
                conn, post_id=f"p{self.seq}", op="create",
                content=content, occurred_at=float(self.seq),
            ).id
        return mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type=type, content=content, confidence=confidence, tier=tier,
            importance=importance, source=source, evidence_event_ids=[ev],
        )

    def _future(self) -> float:
        return db.now_ts() + 40 * refl.DAY_SECONDS

    # --- daily full sweep ----------------------------------------------------

    def test_full_sweep_runs_when_due_and_gates_within_interval(self) -> None:
        stale_state = self._unit("上周在搬家", type="state", tier="contextual")
        future = self._future()
        # fresh meta -> due immediately; the stale state decays
        summaries = refl.reflect_all_personas_if_due(now=future)
        self.assertTrue(summaries)
        self.assertEqual(mus.get_unit(stale_state)["status"], "dormant")
        # a second call inside the interval is a no-op
        self.assertEqual(refl.reflect_all_personas_if_due(now=future + 3600.0), [])
        # past the interval it runs again
        self.assertTrue(
            refl.reflect_all_personas_if_due(now=future + 2 * refl.DAY_SECONDS)
        )

    # --- decay -------------------------------------------------------------

    def test_decay_retires_only_stale_state(self) -> None:
        # Only `state` ages out. A stale durable belief (insight/preference) must
        # survive — durable memory is never forgotten by age, only by relevance.
        stale_state = self._unit("上周在搬家", type="state", tier="contextual")
        stale_durable = self._unit("喜欢安静", type="preference", tier="contextual")
        core_state = self._unit("长期目标状态", type="state", tier="core")
        authored_state = self._unit("用户自述状态", type="state", source="user_authored")
        summary = refl.reflect_persona("global", now=self._future())
        self.assertEqual(summary.decayed, [stale_state])
        self.assertEqual(mus.get_unit(stale_state)["status"], "dormant")
        self.assertEqual(mus.get_unit(stale_durable)["status"], "active")  # durable: immune
        self.assertEqual(mus.get_unit(core_state)["status"], "active")
        self.assertEqual(mus.get_unit(authored_state)["status"], "active")

    def test_fresh_state_not_decayed(self) -> None:
        u = self._unit("最近的状态", type="state", tier="contextual")
        summary = refl.reflect_persona("global")  # cutoff is in the past
        self.assertEqual(summary.decayed, [])
        self.assertEqual(mus.get_unit(u)["status"], "active")

    def test_force_include_state_not_decayed(self) -> None:
        pinned = self._unit("用户钉住的状态", type="state", tier="contextual")
        mus.set_portrait_policy(pinned, portrait_policy="force_include")
        summary = refl.reflect_persona("global", now=self._future())
        self.assertNotIn(pinned, summary.decayed)
        self.assertEqual(mus.get_unit(pinned)["status"], "active")

    def test_dormant_state_leaves_reconcile_comparison_set(self) -> None:
        u = self._unit("旧状态", type="state", tier="contextual")
        refl.reflect_persona("global", now=self._future())
        active = {r["id"] for r in mus.list_reconcile_units_in_bucket("global", "public")}
        self.assertNotIn(u, active)

    # --- promote -----------------------------------------------------------

    def test_promote_sediments_reconfirmed_contextual_to_core(self) -> None:
        u = self._unit("反复确认的重要偏好", confidence=0.9, importance=0.8)
        mus.confirm_unit(u)
        mus.confirm_unit(u)
        summary = refl.reflect_persona("global")
        self.assertIn(u, summary.promoted)
        self.assertEqual(mus.get_unit(u)["tier"], "core")

    def test_promote_needs_enough_confirms(self) -> None:
        once = self._unit("只确认一次", confidence=0.9, importance=0.8)
        mus.confirm_unit(once)
        summary = refl.reflect_persona("global")
        self.assertNotIn(once, summary.promoted)
        self.assertEqual(mus.get_unit(once)["tier"], "contextual")

    def test_promote_respects_importance_floor(self) -> None:
        u = self._unit("常提但不重要", confidence=0.9, importance=0.4)
        mus.confirm_unit(u)
        mus.confirm_unit(u)
        summary = refl.reflect_persona("global")
        self.assertNotIn(u, summary.promoted)
        self.assertEqual(mus.get_unit(u)["tier"], "contextual")


    # --- consolidation (P2b, injected producer) ----------------------------

    def _soul_unit(
        self,
        soul: str,
        visibility: str,
        content: str,
        *,
        type: str = "relationship",
        confidence: float = 0.8,
        tier: str = "contextual",
        importance: float = 0.6,
        source: str = "reflected",
    ) -> str:
        self.seq += 1
        if visibility == "public":
            with db.transaction() as conn:
                mes.record_comment_mutation(
                    conn, comment_id=self.seq, post_id=f"p{self.seq}", soul_name=soul,
                    role="user", op="create", content=content, occurred_at=float(self.seq),
                )
            ev = db.query_one(
                "SELECT id FROM memory_ingest_events "
                "WHERE source_type='comment_relationship' AND source_id=?",
                (str(self.seq),),
            )["id"]
            channel = "comment"
        else:
            with db.transaction() as conn:
                ev = mes.record_chat_mutation(
                    conn, message_id=self.seq, soul_name=soul, role="user",
                    op="create", content=content, occurred_at=float(self.seq),
                ).id
            channel = "chat"
        return mus.add_unit(
            owner_scope=f"soul:{soul}", visibility_scope=visibility, source_channel=channel,
            type=type, content=content, confidence=confidence, tier=tier,
            importance=importance, source=source, evidence_event_ids=[ev],
        )

    def test_consolidate_merges_duplicates(self) -> None:
        keep = self._unit("用户在备考考研")
        dup = self._unit("用户正在准备考研")
        summary = refl.consolidate_persona(
            "global",
            producer=lambda *, owner_scope, units: {
                "ops": [{"op": "merge", "survivor_id": keep, "absorbed_ids": [dup]}]
            },
        )
        self.assertEqual(summary.merged, [dup])
        self.assertEqual(mus.get_unit(dup)["status"], "superseded")
        self.assertEqual(mus.get_unit(dup)["superseded_by"], keep)
        self.assertEqual(mus.get_unit(keep)["status"], "active")
        # consolidation attributes its own ops — not "reflection"
        ops = [dict(r) for r in mus.list_unit_ops(unit_id=dup)]
        supersedes = [o for o in ops if o["op"] == "supersede"]
        self.assertTrue(supersedes)
        self.assertEqual(supersedes[0]["actor"], "consolidation")

    # --- consolidation scheduling (triple gate + one owner per run) ---------

    def test_pick_owner_requires_min_units(self) -> None:
        for i in range(3):
            self._unit(f"事实{i}")
        self.assertIsNone(refl.pick_consolidation_owner(min_units=4))
        self.assertEqual(refl.pick_consolidation_owner(min_units=3), "global")

    def test_consolidate_due_owner_cooldown_and_change_gates(self) -> None:
        for i in range(3):
            self._unit(f"事实{i}")
        noop = lambda *, owner_scope, units: {"ops": []}
        now = db.now_ts()

        summary = refl.consolidate_due_owner(producer=noop, now=now, min_units=3)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.owner_scope, "global")
        # inside the cooldown: not due, even with changes
        self._unit("新增事实")
        self.assertIsNone(refl.consolidate_due_owner(producer=noop, now=now + 3600.0, min_units=3))
        # past the cooldown with a change since the stamp: due again
        after = now + (refl.CONSOLIDATION_COOLDOWN_DAYS + 1) * refl.DAY_SECONDS
        self.assertIsNotNone(refl.consolidate_due_owner(producer=noop, now=after, min_units=3))
        # past another cooldown but nothing changed since: not due
        later = after + (refl.CONSOLIDATION_COOLDOWN_DAYS + 1) * refl.DAY_SECONDS
        self.assertIsNone(refl.consolidate_due_owner(producer=noop, now=later, min_units=3))

    def test_pick_owner_prefers_most_overdue(self) -> None:
        for i in range(3):
            self._unit(f"主记忆{i}")
            self._soul_unit("luna", "private:soul:luna", f"相处{i}")
        now = db.now_ts()
        # consolidate global once: luna (never consolidated) becomes most overdue
        refl.consolidate_due_owner(
            producer=lambda *, owner_scope, units: {"ops": []}, now=now, min_units=3
        )
        self.assertEqual(refl.pick_consolidation_owner(now=now, min_units=3), "soul:luna")

    def test_failed_consolidation_leaves_owner_due(self) -> None:
        for i in range(3):
            self._unit(f"事实{i}")

        def boom(*, owner_scope, units):
            raise RuntimeError("LLM down")

        with self.assertRaises(RuntimeError):
            refl.consolidate_due_owner(producer=boom, min_units=3)
        # no cooldown stamp was written: still due for a later run
        self.assertEqual(refl.pick_consolidation_owner(min_units=3), "global")

    def test_consolidate_iron_law_blocks_public_survivor(self) -> None:
        pub = self._soul_unit("luna", "public", "公开的相处默契")
        priv = self._soul_unit("luna", "private:soul:luna", "私聊里的相处默契")
        summary = refl.consolidate_persona(
            "soul:luna",
            producer=lambda *, owner_scope, units: {
                "ops": [{"op": "merge", "survivor_id": pub, "absorbed_ids": [priv]}]
            },
        )
        self.assertEqual(summary.merged, [])
        self.assertEqual(len(summary.skipped), 1)
        self.assertEqual(mus.get_unit(priv)["status"], "active")
        self.assertEqual(mus.get_unit(pub)["status"], "active")

    def test_consolidate_cross_layer_merges_into_private(self) -> None:
        # decision 1A: same belief across layers merges into the private survivor
        pub = self._soul_unit("luna", "public", "评论区叫老地方")
        priv = self._soul_unit("luna", "private:soul:luna", "私下也叫老地方")
        summary = refl.consolidate_persona(
            "soul:luna",
            producer=lambda *, owner_scope, units: {
                "ops": [{"op": "merge", "survivor_id": priv, "absorbed_ids": [pub]}]
            },
        )
        self.assertEqual(summary.merged, [pub])
        self.assertEqual(mus.get_unit(pub)["status"], "superseded")
        self.assertEqual(mus.get_unit(pub)["superseded_by"], priv)
        self.assertEqual(mus.get_unit(priv)["status"], "active")

    def test_consolidate_retracts_contradiction(self) -> None:
        u = self._unit("一条错误信念")
        summary = refl.consolidate_persona(
            "global",
            producer=lambda *, owner_scope, units: {
                "ops": [{"op": "retract", "target_id": u, "reason": "false"}]
            },
        )
        self.assertEqual(summary.retracted, [u])
        self.assertEqual(mus.get_unit(u)["status"], "retracted_by_model")

    def test_consolidate_skips_foreign_or_missing_ids(self) -> None:
        keep = self._unit("本主体的 unit")
        summary = refl.consolidate_persona(
            "global",
            producer=lambda *, owner_scope, units: {
                "ops": [{"op": "merge", "survivor_id": keep, "absorbed_ids": ["mu_nope"]}]
            },
        )
        self.assertEqual(summary.merged, [])
        self.assertEqual(len(summary.skipped), 1)
        self.assertEqual(mus.get_unit(keep)["status"], "active")


if __name__ == "__main__":
    unittest.main()
