from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (
    context_builder,
    db,
    memory_events_service as mes,
    memory_read,
    memory_reconciler as recon,
    memory_view_producer as view_producer,
    memory_view_service as mvs,
)
from core.llm import reflection_router


def _seed_goal_producer(content: str = "用户在准备考研"):
    """A producer that adds one core-eligible goal unit citing the batch events."""
    def producer(*, boundary, events, active_units, tombstones):
        ids = [e["id"] for e in events]
        return {
            "summary": "s",
            "ops": [{
                "op": "add", "type": "goal", "content": content,
                "confidence": 0.9, "tier": "core", "importance": 0.85,
                "evidence_event_ids": ids,
            }],
        }
    return producer


class ViewLifecycleTest(unittest.TestCase):
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

    def _reconcile(self, content: str) -> None:
        recon.reconcile_bucket(
            "global", "public",
            op_producer=_seed_goal_producer(content),
            reflection_type=recon.RECONCILE_GLOBAL,
        )

    def test_view_type_for_bucket(self) -> None:
        self.assertEqual(mvs.view_type_for_bucket("global", "public"), mvs.VIEW_USER_MD)
        self.assertEqual(
            mvs.view_type_for_bucket("soul:luna", "private:soul:luna"), mvs.VIEW_SOUL_PRIVATE
        )
        self.assertIsNone(mvs.view_type_for_bucket("soul:luna", "thread:p1"))

    def test_first_reconcile_then_refresh_creates_fresh_view(self) -> None:
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0)
        self._reconcile("用户在准备考研")

        # core unit in slice, no view yet -> this bucket needs a view
        self.assertIn(("global", "public", mvs.VIEW_USER_MD), mvs.buckets_needing_view())

        with patch.object(reflection_router, "call_view_synthesis", lambda *a, **k: "你正在备考考研。"):
            results = view_producer.refresh_views_after_reconcile(client=object(), model="m")

        self.assertTrue(results)
        view = mvs.get_view("global", "public", mvs.VIEW_USER_MD)
        self.assertEqual(view["status"], "fresh")
        self.assertIn("备考考研", view["content_md"])
        self.assertEqual(mvs.buckets_needing_view(), [])  # nothing left

    def test_changed_core_set_marks_view_stale_and_re_lists(self) -> None:
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0)
        self._reconcile("用户在准备考研")
        with patch.object(reflection_router, "call_view_synthesis", lambda *a, **k: "v1"):
            view_producer.refresh_views_after_reconcile(client=object(), model="m")
        self.assertEqual(mvs.get_view("global", "public", mvs.VIEW_USER_MD)["status"], "fresh")

        # a second distinct core unit changes the core set
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p2", op="create", content="我想考北大", occurred_at=2.0)
        self._reconcile("用户的目标院校是北大")

        self.assertEqual(mvs.get_view("global", "public", mvs.VIEW_USER_MD)["status"], "stale")
        self.assertIn(("global", "public", mvs.VIEW_USER_MD), mvs.buckets_needing_view())


class _DbTestBase(unittest.TestCase):
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

    def _seed_core_unit(self, content: str = "用户在准备考研") -> None:
        with db.transaction() as conn:
            mes.record_post_mutation(conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0)
        recon.reconcile_bucket(
            "global", "public",
            op_producer=_seed_goal_producer(content),
            reflection_type=recon.RECONCILE_GLOBAL,
        )


class PortraitReadTest(_DbTestBase):
    def test_read_portrait_uses_view_body_without_header(self) -> None:
        self._seed_core_unit("用户在准备考研")
        with patch.object(reflection_router, "call_view_synthesis", lambda *a, **k: "你正在备考。"):
            view_producer.refresh_views_after_reconcile(client=object(), model="m")
        body = mvs.read_portrait_body("global", "public", mvs.VIEW_USER_MD)
        self.assertEqual(body, "你正在备考。")
        self.assertNotIn("generated_by", body)

    def test_read_portrait_template_fallback_when_no_view(self) -> None:
        self._seed_core_unit("用户在准备考研")
        body = mvs.read_portrait_body("global", "public", mvs.VIEW_USER_MD)
        self.assertIn("准备考研", body)  # deterministic template over core units

    def test_read_portrait_empty_when_nothing(self) -> None:
        self.assertEqual(mvs.read_portrait_body("global", "public", mvs.VIEW_USER_MD), "")


class ContextBuilderPortraitTest(_DbTestBase):
    def test_v2_mode_injects_view_portrait(self) -> None:
        self._seed_core_unit("用户在准备考研")
        with patch.object(reflection_router, "call_view_synthesis", lambda *a, **k: "VIEWMARK 你在备考。"):
            view_producer.refresh_views_after_reconcile(client=object(), model="m")
        with patch.dict(os.environ, {memory_read.READ_MODE_ENV: "units"}):
            ctx = context_builder.build_context()
        self.assertIn("VIEWMARK", ctx.shared_context)
        self.assertNotIn("generated_by", ctx.shared_context)  # header stripped

    def test_legacy_mode_does_not_inject_view(self) -> None:
        self._seed_core_unit("用户在准备考研")
        with patch.object(reflection_router, "call_view_synthesis", lambda *a, **k: "VIEWMARK"):
            view_producer.refresh_views_after_reconcile(client=object(), model="m")
        ctx = context_builder.build_context()  # default legacy read mode
        self.assertNotIn("VIEWMARK", ctx.shared_context)


if __name__ == "__main__":
    unittest.main()
