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

    def test_create_user_authored_unit_and_list_operation(self) -> None:
        client = self._client()
        created = client.post(
            "/memory/units",
            json={
                "type": "preference",
                "content": "用户喜欢安静的学习环境",
                "tier": "core",
                "importance": 0.9,
            },
        )
        self.assertEqual(200, created.status_code)
        self.assertEqual("user_authored", created.json()["source"])

        operations = client.get("/memory/operations")
        self.assertEqual(200, operations.status_code)
        operation = operations.json()["operations"][0]
        self.assertEqual("add", operation["op"])
        self.assertEqual("user", operation["actor"])
        self.assertEqual(
            "用户喜欢安静的学习环境", operation["after"]["content"]
        )

    def test_status_reports_pending_evidence(self) -> None:
        self._seed_unit()
        client = self._client()
        response = client.get("/memory/status")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["pending_event_count"])
        self.assertEqual(
            [{"owner_scope": "global", "visibility_scope": "public"}],
            response.json()["pending_buckets"],
        )

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

    def test_prompt_and_portrait_policy(self) -> None:
        unit_id, _ = self._seed_unit()
        client = self._client()

        r1 = client.post(f"/memory/units/{unit_id}/prompt-policy", json={"prompt_policy": "no_prompt"})
        self.assertEqual(200, r1.status_code)
        self.assertEqual(mus.get_unit(unit_id)["prompt_policy"], "no_prompt")
        self.assertEqual(r1.json()["prompt_policy"], "no_prompt")  # response reflects change

        r2 = client.post(
            f"/memory/units/{unit_id}/portrait-policy", json={"portrait_policy": "force_exclude"}
        )
        self.assertEqual(200, r2.status_code)
        self.assertEqual(mus.get_unit(unit_id)["portrait_policy"], "force_exclude")
        self.assertEqual(r2.json()["portrait_policy"], "force_exclude")

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

    def test_type_filter_pushed_down_before_limit(self) -> None:
        # an identity unit (older) and a state unit (newer): type filter must run
        # in SQL, not after LIMIT, or ?type=identity&limit=1 wrongly returns empty.
        e1 = self._seed_unit()[1]
        identity_id = mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type="identity", content="用户是研究生", evidence_event_ids=[e1], confidence=0.8,
        )
        client = self._client()
        resp = client.get("/memory/units", params={"type": "identity", "limit": 1})
        self.assertEqual(200, resp.status_code)
        ids = [u["id"] for u in resp.json()["units"]]
        self.assertEqual(ids, [identity_id])

    def test_resynthesize_rejects_invalid_combo(self) -> None:
        client = self._client()
        # mismatched soul (boundary) + wrong view_type
        resp = client.post(
            "/memory/views/resynthesize",
            json={
                "owner_scope": "soul:luna",
                "visibility_scope": "private:soul:nova",
                "view_type": "user_portrait",
            },
        )
        self.assertEqual(422, resp.status_code)
        # valid bucket but wrong view_type for it
        resp2 = client.post(
            "/memory/views/resynthesize",
            json={
                "owner_scope": "global",
                "visibility_scope": "public",
                "view_type": "soul_relationship_memory",
            },
        )
        self.assertEqual(422, resp2.status_code)

    def test_views_list_and_resynthesize(self) -> None:
        self._seed_unit()
        client = self._client()

        views = client.get("/memory/views")
        self.assertEqual(200, views.status_code)
        self.assertIn("views", views.json())

        resp = client.post(
            "/memory/views/resynthesize",
            json={"owner_scope": "global", "visibility_scope": "public", "view_type": "user_portrait"},
        )
        self.assertEqual(200, resp.status_code)
        self.assertEqual(resp.json()["view_type"], "user_portrait")
        from core import memory_view_service as mvs
        self.assertEqual(mvs.get_view("global", "public", "user_portrait")["status"], "fresh")

    def test_resynthesize_soul_relationship_view(self) -> None:
        with db.transaction() as conn:
            event_id = mes.record_chat_mutation(
                conn,
                message_id=1,
                soul_name="luna",
                role="user",
                op="create",
                content="难过时先陪我一会儿",
                occurred_at=1.0,
            ).id
        mus.add_unit(
            owner_scope="soul:luna",
            visibility_scope="private:soul:luna",
            source_channel="chat",
            type="relationship",
            content="用户难过时希望 luna 先陪伴，不急着讲道理",
            confidence=0.9,
            tier="core",
            importance=0.9,
            evidence_event_ids=[event_id],
        )
        client = self._client()
        resp = client.post(
            "/memory/views/resynthesize",
            json={
                "owner_scope": "soul:luna",
                "visibility_scope": "relationship",
                "view_type": "soul_relationship_memory",
            },
        )
        self.assertEqual(200, resp.status_code)
        self.assertEqual("soul_relationship_memory", resp.json()["view_type"])


if __name__ == "__main__":
    unittest.main()
