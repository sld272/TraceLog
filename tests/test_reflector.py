from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import memory
from core import db, reflector


class FakeClient:
    def __init__(
        self,
        content: str | None = '{"reflection_md":"## 深反思\\n\\n你这段时间有明确的行动线索。","patches":[]}',
        contents: list[str] | None = None,
    ) -> None:
        self.content = content
        self.contents = list(contents or [])
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        self.calls += 1
        if self.contents:
            content = self.contents.pop(0)
        else:
            content = self.content
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class ReflectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"

        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        self.old_memory_workspace = memory.WORKSPACE_DIR
        self.old_user_md_path = memory.USER_MD_PATH

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        memory.WORKSPACE_DIR = str(self.workspace)
        memory.USER_MD_PATH = str(self.workspace / "user.md")

        db.init_db()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "user.md").write_text(
            "---\n"
            "schema: tracelog/user.md@v1\n"
            "sensitivity:\n"
            "  基本信息: high\n"
            "  身份与现状: normal\n"
            "---\n\n"
            "# 用户档案\n\n"
            "## 基本信息\n"
            "- （暂无） <!-- id: bf-empty -->\n\n"
            "## 身份与现状\n"
            "- 测试用户 <!-- id: status-user -->\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        memory.WORKSPACE_DIR = self.old_memory_workspace
        memory.USER_MD_PATH = self.old_user_md_path
        self.tmp.cleanup()

    def test_trigger_global_deep_reflection_writes_reflection(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。")
        client = FakeClient()

        result = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(["20260525-001"], result.related_post_ids)
        row = db.query_one("SELECT type, content, related_posts, metadata FROM reflections WHERE id = ?", (result.id,))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("global_deep", row["type"])
        self.assertIn("深反思", row["content"])
        self.assertIn("20260525-001", row["related_posts"])
        self.assertIn("cli_exit", row["metadata"])

    def test_trigger_global_deep_reflection_skips_when_no_new_posts(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。")
        client = FakeClient()

        first = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")
        second = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, client.calls)

    def test_trigger_global_deep_reflection_applies_profile_patches_and_metadata(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天开始准备比赛。")
        payload = {
            "reflection_md": "## 深反思\n\n你开始把比赛准备推进成具体行动。",
            "patches": [
                {
                    "section": "身份与现状",
                    "ops": [{"op": "add", "value": "正在准备比赛"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.8,
                }
            ],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        result = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")
        row = db.query_one("SELECT metadata FROM reflections WHERE id = ?", (result.id,))
        metadata = json.loads(row["metadata"])

        self.assertEqual({"applied": 1, "pending": 0, "skipped": 0}, result.patch_summary)
        self.assertIn("正在准备比赛 <!-- id: status-", memory.read_profile())
        self.assertEqual({"applied": 1, "pending": 0, "skipped": 0}, metadata["profile_patch_summary"])

    def test_self_intro_name_fallback_goes_to_pending_when_model_returns_no_patch(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "我叫喜多郁代，是高一生。")
        payload = {
            "reflection_md": "## 深反思\n\n你做了一次清晰的自我介绍。",
            "patches": [],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        result = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")
        pending = db.query_one("SELECT section, patch FROM pending_user_md_changes ORDER BY id DESC LIMIT 1")

        self.assertEqual({"applied": 0, "pending": 1, "skipped": 0}, result.patch_summary)
        self.assertIsNotNone(pending)
        self.assertEqual("基本信息", pending["section"])
        self.assertIn("姓名：喜多郁代", pending["patch"])

    def test_trigger_light_reflection_writes_derived_memory(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天和小李完成了比赛计划，但有点焦虑。")
        client = FakeClient(content=json.dumps(self._light_payload(), ensure_ascii=False))

        result = reflector.trigger_light_reflection("20260525-001", client, "fake-model")

        self.assertEqual("20260525-001", result.post_id)
        post = db.query_one("SELECT importance FROM posts WHERE id = ?", ("20260525-001",))
        self.assertEqual(0.8, post["importance"])
        entity = db.query_one("SELECT type, name, aliases, mention_count FROM entities WHERE name = ?", ("小李",))
        self.assertIsNotNone(entity)
        assert entity is not None
        self.assertEqual("person", entity["type"])
        self.assertEqual(1, entity["mention_count"])
        self.assertIn("李同学", entity["aliases"])
        emotion = db.query_one("SELECT label, intensity FROM emotions WHERE post_id = ?", ("20260525-001",))
        self.assertEqual(("焦虑", 0.7), (emotion["label"], emotion["intensity"]))
        event = db.query_one("SELECT summary, category FROM events WHERE post_id = ?", ("20260525-001",))
        self.assertEqual(("和小李完成比赛计划", "project"), (event["summary"], event["category"]))

    def test_light_reflection_is_idempotent_on_rerun(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天和小李完成了比赛计划。")
        client = FakeClient(
            contents=[
                json.dumps(self._light_payload(), ensure_ascii=False),
                json.dumps(
                    {
                        "entities": [
                            {"type": "project", "name": "比赛计划", "aliases": [], "role": "object"}
                        ],
                        "emotions": [{"label": "兴奋", "intensity": 0.6}],
                        "events": [],
                        "relations": [],
                        "importance": 0.6,
                    },
                    ensure_ascii=False,
                ),
            ]
        )

        reflector.trigger_light_reflection("20260525-001", client, "fake-model")
        reflector.trigger_light_reflection("20260525-001", client, "fake-model")

        old_entity = db.query_one("SELECT mention_count FROM entities WHERE name = ?", ("小李",))
        new_entity = db.query_one("SELECT mention_count FROM entities WHERE name = ?", ("比赛计划",))
        emotions = db.query_all("SELECT label FROM emotions WHERE post_id = ?", ("20260525-001",))
        events = db.query_all("SELECT id FROM events WHERE post_id = ?", ("20260525-001",))

        self.assertEqual(0, old_entity["mention_count"])
        self.assertEqual(1, new_entity["mention_count"])
        self.assertEqual(["兴奋"], [row["label"] for row in emotions])
        self.assertEqual([], events)

    def test_run_light_reflection_safely_marks_pending_on_failure(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。")
        client = FakeClient(content="not json")

        result = reflector.run_light_reflection_safely("20260525-001", client, "fake-model")

        self.assertIsNone(result)
        row = db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_reflect:20260525-001",))
        self.assertIsNotNone(row)

    def _insert_post(self, post_id: str, ts: str, content: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, ts, content, 1.0, 1.0),
        )

    def _light_payload(self) -> dict:
        return {
            "entities": [
                {"type": "person", "name": "小李", "aliases": ["李同学"], "role": "subject"},
                {"type": "project", "name": "比赛计划", "aliases": [], "role": "object"},
            ],
            "emotions": [{"label": "焦虑", "intensity": 0.7}],
            "events": [
                {
                    "ts": "2026-05-25T10:00:00+08:00",
                    "summary": "和小李完成比赛计划",
                    "category": "project",
                }
            ],
            "relations": [
                {"a": "小李", "b": "比赛计划", "rel_type": "teammate", "strength_delta": 0.1}
            ],
            "importance": 0.8,
        }


if __name__ == "__main__":
    unittest.main()
