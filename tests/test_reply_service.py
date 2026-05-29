from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from core import db, reply_service
from core.context_builder import BuiltContext
from core.llm.types import LLMClient
from core.soul_service import SoulContext


class ReplyServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        self._insert_post("p-1")
        self._insert_soul("默认")
        self._insert_soul("毒舌好友")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_fanout_passes_shared_context_to_each_soul_without_extra_llm_calls(self) -> None:
        soul_a = SoulContext("默认", None, 1, "默认人格", "")
        soul_b = SoulContext("毒舌好友", None, 2, "毒舌人格", "")
        built_context = BuiltContext(
            shared_context="共享上下文",
            enabled_souls=[soul_a, soul_b],
            relevant_post_ids=[],
        )
        captured_contexts: dict[str, str] = {}

        def fake_call(user_input, client, model, context, soul, *, trace_context=None):
            captured_contexts[soul.name] = context
            return {"reply": f"{soul.name} 回复"}

        client = cast(LLMClient, SimpleNamespace(chat=SimpleNamespace()))
        with patch("core.reply_service.reply_router.call_soul_post_reply", side_effect=fake_call) as call:
            results = reply_service.fanout("p-1", "新的公开 post", client, "fake-model", built_context)

        self.assertEqual(2, call.call_count)
        self.assertEqual(["默认", "毒舌好友"], [result.soul_name for result in results])
        self.assertEqual("共享上下文", captured_contexts["默认"])
        self.assertEqual("共享上下文", captured_contexts["毒舌好友"])
        rows = db.query_all("SELECT soul_name, content FROM comments ORDER BY soul_name")
        self.assertEqual([("毒舌好友", "毒舌好友 回复"), ("默认", "默认 回复")], [(row["soul_name"], row["content"]) for row in rows])

    def _insert_post(self, post_id: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, "2026-05-29T12:00:00+08:00", "新的公开 post", 1.0, 1.0),
        )

    def _insert_soul(self, name: str) -> None:
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            (name, f"souls/{name}.md", 1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
