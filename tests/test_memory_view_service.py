from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    db,
    memory_events_service as mes,
    memory_unit_service as mus,
    memory_view_service as mvs,
)


class MemoryViewServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self._event_seq = 0

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _event(self) -> int:
        self._event_seq += 1
        with db.transaction() as conn:
            return mes.record_post_mutation(
                conn, post_id=f"p{self._event_seq}", op="create",
                content="证据", occurred_at=float(self._event_seq),
            ).id

    def _confirmed_core_unit(self, content: str, *, type="identity", importance=0.8, confidence=0.85) -> str:
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type=type, content=content, confidence=confidence, tier="core",
            importance=importance, evidence_event_ids=[self._event()],
        )
        mus.confirm_unit(unit_id, evidence_event_ids=[self._event()], confidence=confidence)
        return unit_id

    # --- selector ----------------------------------------------------------

    def test_confirmed_core_unit_enters_slice(self) -> None:
        unit_id = self._confirmed_core_unit("我是一名考研学生")
        core = mvs.recompute_slice("global", "public")
        self.assertIn(unit_id, core)
        self.assertEqual(mus.get_unit(unit_id)["in_md_slice"], 1)

    def test_freshly_added_confident_core_unit_enters_immediately(self) -> None:
        # a clearly-stated, high-confidence, important identity must enter the
        # portrait on its first reconcile (no op-count dwell blocking it).
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="identity", content="南大大一法语生，自学计算机", confidence=0.9, tier="core",
            importance=0.9, evidence_event_ids=[self._event()],
        )
        self.assertIn(unit_id, mvs.recompute_slice("global", "public"))

    def test_low_confidence_and_low_importance_excluded(self) -> None:
        low_conf = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="identity", content="弱信念", confidence=0.5, tier="core",
            importance=0.8, evidence_event_ids=[self._event()],
        )
        mus.confirm_unit(low_conf, evidence_event_ids=[self._event()], confidence=0.5)
        low_imp = self._confirmed_core_unit("低价值核心", importance=0.3)
        core = mvs.recompute_slice("global", "public")
        self.assertNotIn(low_conf, core)
        self.assertNotIn(low_imp, core)

    def test_importance_below_portrait_floor_excluded(self) -> None:
        # importance 0.65 is above the unit floor (0.30) but below the portrait floor (0.70)
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="identity", content="中等重要度的核心候选", confidence=0.9, tier="core",
            importance=0.65, evidence_event_ids=[self._event()],
        )
        mus.confirm_unit(unit_id, evidence_event_ids=[self._event()], confidence=0.9)
        self.assertNotIn(unit_id, mvs.recompute_slice("global", "public"))
        # bump to 0.70 -> now admitted
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET importance=0.70 WHERE id=?", (unit_id,))
        self.assertIn(unit_id, mvs.recompute_slice("global", "public"))

    def test_contextual_tier_excluded(self) -> None:
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content="普通偏好", confidence=0.95, tier="contextual",
            importance=0.9, evidence_event_ids=[self._event()],
        )
        mus.confirm_unit(unit_id, evidence_event_ids=[self._event()], confidence=0.95)
        self.assertNotIn(unit_id, mvs.recompute_slice("global", "public"))

    def test_force_include_and_exclude(self) -> None:
        forced_in = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="preference", content="强制进画像", confidence=0.3, tier="contextual",
            importance=0.1, profile_policy="force_include", evidence_event_ids=[self._event()],
        )
        forced_out = self._confirmed_core_unit("强制出画像")
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET profile_policy='force_exclude' WHERE id=?", (forced_out,))
        core = mvs.recompute_slice("global", "public")
        self.assertIn(forced_in, core)
        self.assertNotIn(forced_out, core)

    def test_no_prompt_excluded(self) -> None:
        unit_id = self._confirmed_core_unit("不可进 prompt")
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET prompt_policy='no_prompt' WHERE id=?", (unit_id,))
        self.assertNotIn(unit_id, mvs.recompute_slice("global", "public"))

    def test_user_authored_enters_without_dwell(self) -> None:
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="user",
            type="identity", content="用户亲述身份", source="user_authored", confidence=1.0,
            tier="core", importance=0.9,
        )
        self.assertIn(unit_id, mvs.recompute_slice("global", "public"))

    def test_hysteresis_keeps_until_exit(self) -> None:
        unit_id = self._confirmed_core_unit("滞回测试", confidence=0.85)
        self.assertIn(unit_id, mvs.recompute_slice("global", "public"))  # in slice now
        # drop confidence to between EXIT and ENTER -> stays (hysteresis)
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET confidence=0.7 WHERE id=?", (unit_id,))
        self.assertIn(unit_id, mvs.recompute_slice("global", "public"))
        # drop below EXIT -> leaves
        with db.transaction() as conn:
            conn.execute("UPDATE memory_units SET confidence=0.5 WHERE id=?", (unit_id,))
        self.assertNotIn(unit_id, mvs.recompute_slice("global", "public"))

    # --- rendering & view persistence -------------------------------------

    def test_template_render_groups_by_type(self) -> None:
        self._confirmed_core_unit("我是研究生", type="identity")
        self._confirmed_core_unit("想考上理想院校", type="goal")
        view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        self.assertTrue(view.used_fallback)
        self.assertIn("## 身份", view.content_md)
        self.assertIn("## 目标", view.content_md)
        self.assertIn("generated_by=tracelog", view.content_md)

    def test_synthesize_persists_view_and_members(self) -> None:
        u1 = self._confirmed_core_unit("信念一")
        view = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        row = mvs.get_view("global", "public", mvs.VIEW_USER_MD)
        self.assertEqual(row["status"], "fresh")
        members = db.query_all("SELECT unit_id FROM memory_view_units WHERE view_id=?", (view.view_id,))
        self.assertEqual([m["unit_id"] for m in members], [u1])

    def test_synthesize_resynthesize_replaces_members(self) -> None:
        self._confirmed_core_unit("第一批")
        v1 = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        self._confirmed_core_unit("第二批")
        v2 = mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        self.assertEqual(v1.view_id, v2.view_id)  # same view row
        self.assertEqual(len(v2.unit_ids), 2)

    def test_injected_synthesizer_used_and_fallback_on_error(self) -> None:
        self._confirmed_core_unit("信念")
        ok = mvs.synthesize_view(
            "global", "public", mvs.VIEW_USER_MD,
            synthesizer=lambda units, budget: "这是 LLM 综合出的连贯画像。",
        )
        self.assertFalse(ok.used_fallback)
        self.assertIn("LLM 综合", ok.content_md)

        boom = mvs.synthesize_view(
            "global", "public", mvs.VIEW_USER_MD,
            synthesizer=lambda units, budget: (_ for _ in ()).throw(RuntimeError("llm down")),
        )
        self.assertTrue(boom.used_fallback)  # template fallback

    # --- stale detection ---------------------------------------------------

    def test_pure_confirm_does_not_change_hash(self) -> None:
        unit_id = self._confirmed_core_unit("稳定信念")
        mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        # a pure confirm bumps confidence/last_confirmed but content/type/etc unchanged
        mus.confirm_unit(unit_id, evidence_event_ids=[self._event()])
        self.assertFalse(mvs.mark_stale_if_changed("global", "public", mvs.VIEW_USER_MD))

    def test_content_change_marks_stale(self) -> None:
        unit_id = self._confirmed_core_unit("旧表述")
        mvs.synthesize_view("global", "public", mvs.VIEW_USER_MD)
        mus.revise_unit(unit_id, content="新表述，含更多细节")
        self.assertTrue(mvs.mark_stale_if_changed("global", "public", mvs.VIEW_USER_MD))
        self.assertEqual(mvs.get_view("global", "public", mvs.VIEW_USER_MD)["status"], "stale")


if __name__ == "__main__":
    unittest.main()
