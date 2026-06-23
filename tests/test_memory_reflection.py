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

    # --- decay -------------------------------------------------------------

    def test_decay_retires_only_stale_noncore_reflected(self) -> None:
        stale = self._unit("旧的次要信息", tier="contextual")
        core = self._unit("核心身份", tier="core")
        authored = self._unit("用户自述", tier="contextual", source="user_authored")
        summary = refl.reflect_persona("global", now=self._future())
        self.assertEqual(summary.decayed, [stale])
        self.assertEqual(mus.get_unit(stale)["status"], "dormant")
        self.assertEqual(mus.get_unit(core)["status"], "active")
        self.assertEqual(mus.get_unit(authored)["status"], "active")

    def test_fresh_units_not_decayed(self) -> None:
        u = self._unit("最近的信息", tier="contextual")
        summary = refl.reflect_persona("global")  # cutoff is in the past
        self.assertEqual(summary.decayed, [])
        self.assertEqual(mus.get_unit(u)["status"], "active")

    def test_force_include_unit_not_decayed(self) -> None:
        pinned = self._unit("用户钉住的次要信息", tier="contextual")
        mus.set_portrait_policy(pinned, portrait_policy="force_include")
        summary = refl.reflect_persona("global", now=self._future())
        self.assertNotIn(pinned, summary.decayed)
        self.assertEqual(mus.get_unit(pinned)["status"], "active")

    def test_dormant_leaves_reconcile_comparison_set(self) -> None:
        u = self._unit("旧次要", tier="contextual")
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


if __name__ == "__main__":
    unittest.main()
