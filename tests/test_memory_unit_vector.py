from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import (
    db,
    memory_events_service as mes,
    memory_read,
    memory_unit_service as mus,
    vector_index_service,
    vectorstore,
)


class UnitVectorTest(unittest.TestCase):
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

    def _unit(self, content: str, **kw) -> str:
        with db.transaction() as conn:
            ev = mes.record_post_mutation(conn, post_id="p1", op="create", content="ev", occurred_at=1.0).id
        return mus.add_unit(
            owner_scope="global", visibility_scope="public", source_channel="post",
            type=kw.get("type", "insight"), content=content, confidence=0.7,
            tier="contextual", importance=0.6, evidence_event_ids=[ev],
        )

    def test_build_unit_doc(self) -> None:
        doc = vector_index_service.build_unit_doc("mu_1", "内容", "global", "public", "goal")
        self.assertEqual(doc.doc_id, "unit-mu_1")
        self.assertEqual(doc.doc_type, "unit")
        self.assertEqual(doc.metadata["type"], "unit")
        self.assertEqual(doc.metadata["unit_id"], "mu_1")
        self.assertIsNone(vector_index_service.build_unit_doc("mu_1", "", "global", "public", "goal"))

    def test_active_unit_in_expected_docs(self) -> None:
        uid = self._unit("用户在准备考研")
        unit_docs = [d for d in vector_index_service.expected_docs_from_sqlite() if d.doc_type == "unit"]
        self.assertTrue(any(d.source_id == uid for d in unit_docs))

    def test_semantic_hit_surfaces_unit_without_keyword_overlap(self) -> None:
        uid = self._unit("用户喜欢弹吉他")
        fake_hits = [SimpleNamespace(doc_id=f"unit-{uid}", metadata={"type": "unit", "unit_id": uid}, rank=1)]
        with patch.object(vectorstore, "query_documents", lambda q, n_results=20, where=None: fake_hits):
            # "音乐爱好" shares no keyword with "用户喜欢弹吉他" but is semantically related
            hits = memory_read.retrieve_units("音乐爱好", "public_post", None)
        self.assertTrue(any(h.unit_id == uid for h in hits))

    def test_degrades_to_keyword_when_semantic_unavailable(self) -> None:
        self._unit("用户喜欢弹吉他")

        def boom(*a, **k):
            raise RuntimeError("vector index not ready")

        with patch.object(vectorstore, "query_documents", boom):
            self.assertEqual(len(memory_read.retrieve_units("吉他", "public_post", None)), 1)
            self.assertEqual(memory_read.retrieve_units("量子物理", "public_post", None), [])


if __name__ == "__main__":
    unittest.main()
