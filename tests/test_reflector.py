from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import chat_service, comment_service, db, profile_service, reflector, soul_memory_service, soul_service
from tests.helpers import require_not_none


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
        self.old_user_md_path = profile_service.USER_MD_PATH
        self.old_souls_dir = soul_service.SOULS_DIR
        self.old_service_memories_dir = soul_service.SOUL_MEMORIES_DIR
        self.old_memory_memories_dir = soul_memory_service.SOUL_MEMORIES_DIR

        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        profile_service.USER_MD_PATH = str(self.workspace / "user.md")
        soul_service.SOULS_DIR = self.workspace / "souls"
        soul_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"
        soul_memory_service.SOUL_MEMORIES_DIR = self.workspace / "soul_memories"

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
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
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

    def test_global_deep_reflection_failure_keeps_posts_for_next_retry(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。")
        bad_client = FakeClient(content=json.dumps({"reflection_md": "太短", "patches": []}, ensure_ascii=False))

        with self.assertRaises(ValueError):
            reflector.trigger_global_deep_reflection(bad_client, "fake-model", trigger="cli_exit")

        rows = db.query_all("SELECT id FROM reflections WHERE type = ?", ("global_deep",))
        self.assertEqual([], rows)

        good_client = FakeClient()
        result = require_not_none(reflector.trigger_global_deep_reflection(good_client, "fake-model", trigger="cli_exit"))

        self.assertEqual(["20260525-001"], result.related_post_ids)

    def test_preview_global_deep_reflection_scope_matches_pending_posts(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "第一条。")
        self._insert_post("20260525-002", "2026-05-25T11:00:00+08:00", "第二条。")

        scope = reflector.preview_global_deep_reflection_scope()

        self.assertEqual(["20260525-001", "20260525-002"], scope.post_ids)
        self.assertEqual("2026-05-25T10:00:00+08:00", scope.scope_start)
        self.assertEqual("2026-05-25T11:00:00+08:00", scope.scope_end)

        reflector.trigger_global_deep_reflection(FakeClient(), "fake-model", trigger="cli_exit")
        empty_scope = reflector.preview_global_deep_reflection_scope()

        self.assertEqual([], empty_scope.post_ids)
        self.assertIsNone(empty_scope.scope_start)
        self.assertIsNone(empty_scope.scope_end)

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

        result = require_not_none(reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit"))
        row = require_not_none(db.query_one("SELECT metadata FROM reflections WHERE id = ?", (result.id,)))
        metadata = json.loads(row["metadata"])

        self.assertEqual({"applied": 1, "skipped": 0, "skipped_details": []}, result.patch_summary)
        self.assertIn("正在准备比赛 <!-- id: status-", profile_service.read_profile())
        self.assertEqual({"applied": 1, "skipped": 0, "skipped_details": []}, metadata["profile_patch_summary"])

    def test_empty_patches_do_not_write_profile_revision(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "我叫喜多郁代，是高一生。")
        payload = {
            "reflection_md": "## 深反思\n\n你做了一次清晰的自我介绍。",
            "patches": [],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        result = require_not_none(reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit"))
        rows = db.query_all("SELECT id FROM user_md_revisions")

        self.assertEqual({"applied": 0, "skipped": 0, "skipped_details": []}, result.patch_summary)
        self.assertEqual([], rows)
        self.assertNotIn("姓名：喜多郁代 <!-- id: bf-", profile_service.read_profile())

    def test_global_deep_reflection_applies_update_remove_and_skips_placeholder_patch(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "我不再用测试用户这个说法，改为比赛项目参与者。")
        payload = {
            "reflection_md": "## 深反思\n\n你在修正对自己当前身份的描述。",
            "patches": [
                {
                    "section": "身份与现状",
                    "ops": [{"op": "update", "anchor": "status-user", "value": "比赛项目参与者"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.7,
                },
                {
                    "section": "基本信息",
                    "ops": [{"op": "remove", "anchor": "bf-empty"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.95,
                },
                {
                    "section": "身份与现状",
                    "ops": [{"op": "add", "value": "暂无"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.9,
                },
            ],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        result = require_not_none(reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit"))
        content = profile_service.read_profile()
        row = require_not_none(db.query_one("SELECT metadata FROM reflections WHERE id = ?", (result.id,)))
        metadata = json.loads(row["metadata"])

        self.assertEqual(2, result.patch_summary["applied"])
        self.assertEqual(1, result.patch_summary["skipped"])
        self.assertEqual(
            [
                {
                    "reason": "invalid_value",
                    "section": "身份与现状",
                    "ops": [{"op": "add", "value": "暂无"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.9,
                }
            ],
            result.patch_summary["skipped_details"],
        )
        self.assertEqual(result.patch_summary, metadata["profile_patch_summary"])
        self.assertIn("比赛项目参与者 <!-- id: status-user -->", content)
        self.assertNotIn("bf-empty", content)
        self.assertNotIn("- 暂无", content)

    def test_trigger_light_reflection_writes_derived_memory(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天和小李完成了比赛计划，但有点焦虑。")
        client = FakeClient(content=json.dumps(self._light_payload(), ensure_ascii=False))

        result = reflector.trigger_light_reflection("20260525-001", client, "fake-model")

        self.assertEqual("20260525-001", result.post_id)
        post = require_not_none(db.query_one("SELECT importance FROM posts WHERE id = ?", ("20260525-001",)))
        self.assertEqual(0.8, post["importance"])
        entity = db.query_one("SELECT type, name, aliases, mention_count FROM entities WHERE name = ?", ("小李",))
        self.assertIsNotNone(entity)
        assert entity is not None
        self.assertEqual("person", entity["type"])
        self.assertEqual(1, entity["mention_count"])
        self.assertIn("李同学", entity["aliases"])
        emotion = require_not_none(db.query_one("SELECT label, intensity FROM emotions WHERE post_id = ?", ("20260525-001",)))
        self.assertEqual(("焦虑", 0.7), (emotion["label"], emotion["intensity"]))
        event = require_not_none(db.query_one("SELECT summary, category FROM events WHERE post_id = ?", ("20260525-001",)))
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

        old_entity = require_not_none(db.query_one("SELECT mention_count FROM entities WHERE name = ?", ("小李",)))
        new_entity = require_not_none(db.query_one("SELECT mention_count FROM entities WHERE name = ?", ("比赛计划",)))
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

    def test_soul_deep_reflection_reads_raw_chat_and_comment_messages_and_patches_memory(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("默认")
        chat_user = chat_service.append_user_message(chat_thread.id, "我在默认面前会直接说累")
        self._append_chat_assistant(chat_thread.id, "我听见了。")
        self._insert_comment_seed()
        comment_thread = comment_service.get_or_create_thread("20260525-001", "默认")
        comment_user = comment_service.append_user_message(comment_thread.id, "这条评论也只给默认看")
        payload = {
            "reflection_md": "## SOUL 深反思\n\n用户在这个 SOUL 面前表达了更私人的疲惫与限定可见的评论。",
            "patches": [
                {
                    "section": "对用户的理解",
                    "ops": [{"op": "add", "value": "用户在默认面前更愿意直接表达疲惫和限定可见的想法"}],
                    "evidence": [f"chat_message:{chat_user.id}", f"comment_message:{comment_user.id}"],
                    "confidence": 0.86,
                }
            ],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        results = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")
        memory_content = soul_memory_service.read_soul_memory("默认")
        reflection = db.query_one("SELECT type, metadata, related_posts FROM reflections WHERE type = 'soul_deep'")
        user_revisions = db.query_all("SELECT id FROM user_md_revisions")

        self.assertEqual(1, len(results))
        self.assertEqual("默认", results[0].soul_name)
        self.assertEqual({"applied": 1, "skipped": 0, "skipped_details": []}, results[0].patch_summary)
        self.assertIn("用户在默认面前更愿意直接表达疲惫", memory_content)
        self.assertEqual([], user_revisions)
        reflection = require_not_none(reflection)
        metadata = json.loads(reflection["metadata"])
        self.assertEqual("默认", metadata["soul_name"])
        self.assertIn(f"chat_message:{chat_user.id}", reflection["related_posts"])
        self.assertIn(f"comment_message:{comment_user.id}", reflection["related_posts"])

    def test_soul_deep_reflection_skips_souls_without_new_interactions(self) -> None:
        soul_service.sync_souls()
        client = FakeClient()

        results = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")

        self.assertEqual([], results)
        self.assertEqual(0, client.calls)

    def test_soul_deep_reflection_cursor_prevents_rerun(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("默认")
        chat_user = chat_service.append_user_message(chat_thread.id, "这条只处理一次")
        payload = {
            "reflection_md": "## SOUL 深反思\n\n用户说了一条只需要处理一次的私聊。",
            "patches": [
                {
                    "section": "对用户的理解",
                    "ops": [{"op": "add", "value": "用户测试 SOUL 深反思游标"}],
                    "evidence": [f"chat_message:{chat_user.id}"],
                    "confidence": 0.8,
                }
            ],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        first = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")
        second = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")

        self.assertEqual(1, len(first))
        self.assertEqual([], second)
        self.assertEqual(1, client.calls)

    def test_soul_deep_reflection_invalid_result_does_not_advance_cursor(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("默认")
        chat_user = chat_service.append_user_message(chat_thread.id, "这条需要稍后补跑")
        bad_client = FakeClient(content=json.dumps({"reflection_md": "太短", "patches": []}, ensure_ascii=False))

        first = reflector.trigger_soul_deep_reflections(bad_client, "fake-model", trigger="cli_exit")

        self.assertEqual([], first)
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("soul_deep_cursor:默认",)))

        payload = {
            "reflection_md": "## SOUL 深反思\n\n用户留下了一条需要在下次退出时补跑的私聊。",
            "patches": [
                {
                    "section": "对用户的理解",
                    "ops": [{"op": "add", "value": "用户测试 SOUL 补偿语义"}],
                    "evidence": [f"chat_message:{chat_user.id}"],
                    "confidence": 0.8,
                }
            ],
        }
        good_client = FakeClient(content=json.dumps(payload, ensure_ascii=False))
        second = reflector.trigger_soul_deep_reflections(good_client, "fake-model", trigger="cli_exit")

        self.assertEqual(1, len(second))
        self.assertEqual(1, second[0].interaction_count)
        self.assertIsNotNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("soul_deep_cursor:默认",)))

    def test_preview_soul_deep_reflection_scopes_match_pending_interactions(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("默认")
        chat_service.append_user_message(chat_thread.id, "这条会进入 preview")

        scopes = reflector.preview_soul_deep_reflection_scopes()

        self.assertEqual(1, len(scopes))
        self.assertEqual("默认", scopes[0].soul_name)
        self.assertEqual(1, scopes[0].interaction_count)

        reflector.trigger_soul_deep_reflections(FakeClient(), "fake-model", trigger="cli_exit")
        self.assertEqual([], reflector.preview_soul_deep_reflection_scopes())

    def _insert_post(self, post_id: str, ts: str, content: str) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, ts, content, 1.0, 1.0),
        )

    def _append_chat_assistant(self, thread_id: int, content: str) -> None:
        db.execute(
            """
            INSERT INTO chat_messages(thread_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (thread_id, "assistant", content, 3.0),
        )

    def _insert_comment_seed(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天想认真练歌。")
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("20260525-001", "默认", "我陪你继续练。", 2.0),
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
