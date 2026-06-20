from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from core import db, memory_events_service as mes, memory_unit_service as mus


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed")
class ApiMemoryTest(unittest.TestCase):
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

    def _client(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        return TestClient(create_app())

    def _seed_unit(self) -> tuple[str, int]:
        with db.transaction() as conn:
            event_id = mes.record_post_mutation(
                conn, post_id="p1", op="create", content="我在准备考研", occurred_at=1.0
            ).id
        unit_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="state", content="用户在准备考研", confidence=0.7,
            evidence_event_ids=[event_id], tier="core", importance=0.8,
        )
        return unit_id, event_id

    def test_list_and_detail(self) -> None:
        unit_id, event_id = self._seed_unit()
        client = self._client()

        listed = client.get("/memory/units")
        self.assertEqual(200, listed.status_code)
        ids = [u["id"] for u in listed.json()["units"]]
        self.assertIn(unit_id, ids)

        detail = client.get(f"/memory/units/{unit_id}")
        self.assertEqual(200, detail.status_code)
        body = detail.json()
        self.assertEqual(body["content"], "用户在准备考研")
        self.assertEqual([e["event_id"] for e in body["evidence"]], [event_id])
        self.assertFalse(body["evidence"][0]["review_pending"])

        self.assertEqual(404, client.get("/memory/units/mu_missing").status_code)

    def test_update_marks_pending_and_enqueues_relink(self) -> None:
        unit_id, _ = self._seed_unit()
        client = self._client()

        resp = client.patch(f"/memory/units/{unit_id}", json={"content": "用户已入职"})
        self.assertEqual(200, resp.status_code)
        body = resp.json()
        self.assertEqual(body["content"], "用户已入职")
        self.assertTrue(body["evidence"][0]["review_pending"])

        # link held for AI re-link + a reconcile job enqueued to run it
        self.assertEqual(len(mus.list_pending_relinks()), 1)
        jobs = db.query_all("SELECT * FROM jobs WHERE type LIKE '%reconcile%'")
        self.assertGreaterEqual(len(jobs), 1)

    def test_update_rejects_terminal_unit(self) -> None:
        unit_id, _ = self._seed_unit()
        mus.retract_unit(unit_id, by="user", reason="false")
        client = self._client()
        resp = client.patch(f"/memory/units/{unit_id}", json={"content": "想复活"})
        self.assertEqual(422, resp.status_code)

    def test_prompt_and_profile_policy(self) -> None:
        unit_id, _ = self._seed_unit()
        client = self._client()

        r1 = client.post(f"/memory/units/{unit_id}/prompt-policy", json={"prompt_policy": "no_prompt"})
        self.assertEqual(200, r1.status_code)
        self.assertEqual(mus.get_unit(unit_id)["prompt_policy"], "no_prompt")

        r2 = client.post(
            f"/memory/units/{unit_id}/profile-policy", json={"profile_policy": "force_exclude"}
        )
        self.assertEqual(200, r2.status_code)
        self.assertEqual(mus.get_unit(unit_id)["profile_policy"], "force_exclude")

        bad = client.post(f"/memory/units/{unit_id}/prompt-policy", json={"prompt_policy": "bogus"})
        self.assertEqual(422, bad.status_code)

    def test_retract_unit(self) -> None:
        unit_id, _ = self._seed_unit()
        client = self._client()
        resp = client.request("DELETE", f"/memory/units/{unit_id}", params={"reason": "false"})
        self.assertEqual(200, resp.status_code)
        unit = mus.get_unit(unit_id)
        self.assertEqual(unit["status"], "retracted_by_user")
        self.assertEqual(unit["retraction_reason"], "false")

    def test_views_list_and_resynthesize(self) -> None:
        self._seed_unit()
        client = self._client()

        views = client.get("/memory/views")
        self.assertEqual(200, views.status_code)
        self.assertIn("views", views.json())

        resp = client.post(
            "/memory/views/resynthesize",
            json={"owner_scope": "global", "visibility_scope": "public", "view_type": "user_md"},
        )
        self.assertEqual(200, resp.status_code)
        self.assertEqual(resp.json()["view_type"], "user_md")
        from core import memory_view_service as mvs
        self.assertEqual(mvs.get_view("global", "public", "user_md")["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
