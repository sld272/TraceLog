from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from core import db, reply_service
from core.context_builder import BuiltContext
from core.llm import reply_router
from core.llm.types import LLMClient
from core.soul_service import SoulContext


class FakeClient:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {"reply": "我在。"}
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False)))
            ]
        )


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

    def test_soul_post_reply_prompt_includes_virtual_friend_boundaries(self) -> None:
        soul = SoulContext("默认", None, 0, "默认人格", "")
        client = FakeClient()

        reply_router.call_soul_post_reply("今天好累", client, "fake-model", "共享上下文", soul)
        prompt = client.calls[-1]["messages"][0]["content"]

        self.assertIn("比喻、场景感、小剧场和幽默想象", prompt)
        self.assertIn("不能伪造事实", prompt)
        self.assertIn("听起来像", prompt)
        self.assertIn("没有证据时", prompt)
        self.assertIn("不要说“我记得你", prompt)
        self.assertIn("其他 SOUL", prompt)

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
