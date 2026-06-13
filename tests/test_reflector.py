from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import chat_service, comment_service, db, profile_service, reflector, soul_memory_service, soul_service
from core.llm import reflection_router
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
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        self.calls += 1
        content = self.contents.pop(0) if self.contents else self.content
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


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
            "  身份与角色: high\n"
            "  当前状态与关注: low\n"
            "---\n\n"
            "# 用户档案\n\n"
            "## 基本信息\n"
            "- （暂无） <!-- id: bf-empty -->\n\n"
            "## 身份与角色\n"
            "- 测试用户 <!-- id: role-user -->\n\n"
            "## 当前状态与关注\n",
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

    def test_trigger_global_deep_reflection_writes_reflection_from_raw_posts(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。raw_global_marker")
        client = FakeClient()

        result = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(["20260525-001"], result.related_post_ids)
        row = require_not_none(db.query_one("SELECT type, content, related_posts, metadata FROM reflections WHERE id = ?", (result.id,)))
        self.assertEqual("global_deep", row["type"])
        self.assertIn("深反思", row["content"])
        self.assertIn("20260525-001", row["related_posts"])
        self.assertIn("cli_exit", row["metadata"])

        prompt = client.requests[0]["messages"][1]["content"]
        self.assertIn("本次触发范围内的帖子", prompt)
        self.assertIn("raw_global_marker", prompt)
        self.assertNotIn("相关记忆", prompt)

    def test_global_deep_reflection_accepts_plain_paragraph_content(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天完成了比赛计划。")
        payload = {
            "reflection_md": "你这次把比赛计划推进得比较清楚，虽然输出不是 Markdown 分段，但已经包含足够具体的复盘内容。",
            "patches": [],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        result = require_not_none(reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit"))
        row = require_not_none(db.query_one("SELECT type, content FROM reflections WHERE id = ?", (result.id,)))

        self.assertEqual("global_deep", row["type"])
        self.assertIn("不是 Markdown 分段", row["content"])

    def test_global_deep_reflection_validator_rejects_empty_and_too_short_content(self) -> None:
        self.assertFalse(reflector._is_valid_reflection(None))
        self.assertFalse(reflector._is_valid_reflection(""))
        self.assertFalse(reflector._is_valid_reflection("太短"))
        self.assertTrue(reflector._is_valid_reflection("这是一段足够长的普通深反思正文，不依赖 Markdown 标记。"))

    def test_global_deep_reflection_prompt_requests_reconcile_not_extraction(self) -> None:
        prompt = reflection_router.GLOBAL_DEEP_REFLECTION_PROMPT

        self.assertIn("raw posts", prompt)
        self.assertIn("对账", prompt)
        self.assertIn("confirm / revise / retract", prompt)
        self.assertIn("不是只追加事实", prompt)
        self.assertIn("当前状态与关注", prompt)
        self.assertIn("快进快删", prompt)
        self.assertIn("不超过 10 条", prompt)
        self.assertNotIn("关键身份", prompt)
        self.assertNotIn("身份与现状", prompt)
        self.assertNotIn("长期目标与当前痛点", prompt)
        self.assertNotIn("近期主题与走向", prompt)
        self.assertNotIn("observations", prompt)

    def test_soul_deep_reflection_prompt_excludes_assistant_improvisation_from_memory(self) -> None:
        prompt = reflection_router.SOUL_DEEP_REFLECTION_PROMPT

        self.assertIn("SOUL/assistant 自己生成的玩笑", prompt)
        self.assertIn("比喻、小剧场", prompt)
        self.assertIn("不能作为用户事实", prompt)
        self.assertIn("用户事实只能来自用户消息", prompt)

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

        self.assertEqual([], db.query_all("SELECT id FROM reflections WHERE type = ?", ("global_deep",)))
        good_client = FakeClient()
        result = require_not_none(reflector.trigger_global_deep_reflection(good_client, "fake-model", trigger="cli_exit"))

        self.assertEqual(["20260525-001"], result.related_post_ids)

    def test_global_deep_reflection_orders_and_filters_iso_timestamps_by_absolute_time(self) -> None:
        self._insert_post("20260527-001", "2026-05-27T01:00:00+00:00", "绝对时间较晚。")
        self._insert_post("20260527-002", "2026-05-27T08:00:00+08:00", "绝对时间较早。")
        client = FakeClient()

        first = require_not_none(reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit"))
        self._insert_post("20260527-003", "2026-05-27T08:30:00+08:00", "绝对时间早于游标但字符串更大。")
        second = reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit")

        self.assertEqual(["20260527-002", "20260527-001"], first.related_post_ids)
        self.assertIsNone(second)

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
                    "section": "当前状态与关注",
                    "ops": [{"op": "add", "value": "正在准备比赛"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.5,
                }
            ],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        result = require_not_none(reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit"))
        row = require_not_none(db.query_one("SELECT metadata FROM reflections WHERE id = ?", (result.id,)))
        metadata = json.loads(row["metadata"])

        self.assertEqual({"applied": 1, "skipped": 0, "skipped_details": []}, result.patch_summary)
        self.assertIn("正在准备比赛 <!-- id: current-", profile_service.read_profile())
        self.assertEqual({"applied": 1, "skipped": 0, "skipped_details": []}, metadata["profile_patch_summary"])

    def test_global_deep_reflection_applies_update_remove_and_skips_placeholder_patch(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "我不再用测试用户这个说法，改为比赛项目参与者。")
        payload = {
            "reflection_md": "## 深反思\n\n你在修正对自己当前身份的描述。",
            "patches": [
                {
                    "section": "身份与角色",
                    "ops": [{"op": "update", "anchor": "role-user", "value": "比赛项目参与者"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.88,
                },
                {
                    "section": "基本信息",
                    "ops": [{"op": "remove", "anchor": "bf-empty"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.95,
                },
                {
                    "section": "当前状态与关注",
                    "ops": [{"op": "add", "value": "暂无"}],
                    "evidence": ["20260525-001"],
                    "confidence": 0.9,
                },
            ],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        result = require_not_none(reflector.trigger_global_deep_reflection(client, "fake-model", trigger="cli_exit"))
        content = profile_service.read_profile()

        self.assertEqual(2, result.patch_summary["applied"])
        self.assertEqual(1, result.patch_summary["skipped"])
        self.assertEqual("invalid_value", result.patch_summary["skipped_details"][0]["reason"])
        self.assertIn("比赛项目参与者 <!-- id: role-user -->", content)
        self.assertNotIn("bf-empty", content)
        self.assertNotIn("- 暂无", content)

    def test_trigger_light_reflection_writes_derived_memory(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天和小李完成了比赛计划，但有点焦虑。")
        client = FakeClient(content=json.dumps(self._light_payload(), ensure_ascii=False))

        result = reflector.trigger_light_reflection("20260525-001", client, "fake-model")

        self.assertEqual("20260525-001", result.post_id)
        post = require_not_none(db.query_one("SELECT importance FROM posts WHERE id = ?", ("20260525-001",)))
        self.assertEqual(0.8, post["importance"])
        entity = require_not_none(db.query_one("SELECT type, name, aliases, mention_count FROM entities WHERE name = ?", ("小李",)))
        self.assertEqual("person", entity["type"])
        self.assertEqual(1, entity["mention_count"])
        self.assertIn("李同学", entity["aliases"])
        emotion = require_not_none(db.query_one("SELECT label, intensity FROM emotions WHERE post_id = ?", ("20260525-001",)))
        self.assertEqual(("焦虑", 0.7), (emotion["label"], emotion["intensity"]))
        event = require_not_none(db.query_one("SELECT summary, category FROM events WHERE post_id = ?", ("20260525-001",)))
        self.assertEqual(("和小李完成比赛计划", "project"), (event["summary"], event["category"]))

    def test_light_reflection_is_idempotent_on_rerun(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天和小李完成比赛计划。")
        client = FakeClient(
            contents=[
                json.dumps(self._light_payload(), ensure_ascii=False),
                json.dumps(
                    {
                        "entities": [{"type": "project", "name": "比赛计划", "aliases": [], "role": "object"}],
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

    def test_run_light_reflection_safely_marks_pending_on_failure_and_rolls_back(self) -> None:
        self._insert_post("20260525-001", "2026-05-25T10:00:00+08:00", "今天和小李完成了比赛计划。")
        client = FakeClient(content="not json")

        result = reflector.run_light_reflection_safely("20260525-001", client, "fake-model")

        self.assertIsNone(result)
        self.assertIsNotNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("pending_reflect:20260525-001",)))
        self.assertEqual([], db.query_all("SELECT post_id FROM post_entities WHERE post_id = ?", ("20260525-001",)))
        self.assertEqual([], db.query_all("SELECT label FROM emotions WHERE post_id = ?", ("20260525-001",)))
        self.assertEqual([], db.query_all("SELECT id FROM events WHERE post_id = ?", ("20260525-001",)))

    def test_load_recent_posts_before_uses_absolute_iso_time_for_light_reflection_context(self) -> None:
        self._insert_post("20260527-001", "2026-05-27T01:00:00+00:00", "目标。")
        self._insert_post("20260527-002", "2026-05-27T08:30:00+08:00", "绝对时间更早但字符串更大。")
        self._insert_post("20260527-003", "2026-05-27T09:30:00+08:00", "绝对时间更晚。")

        rows = reflector._load_recent_posts_before("20260527-001", limit=5)

        self.assertEqual(["20260527-002"], [row["id"] for row in rows])

    def test_soul_deep_reflection_reads_raw_thread_messages_and_patches_memory(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("拾迹者")
        chat_user = chat_service.append_user_message(chat_thread.id, "我在拾迹者面前会直接说累")
        self._append_chat_assistant(chat_thread.id, "我听见了。")
        self._insert_comment_seed()
        comment_user = comment_service.append_comment("20260525-001", "拾迹者", "user", "这条评论也只给拾迹者看")
        payload = {
            "reflection_md": "## SOUL 深反思\n\n用户在这个 SOUL 面前表达了更私人的疲惫与限定可见的评论。",
            "patches": [
                {
                    "section": "对用户的理解",
                    "ops": [{"op": "add", "value": "用户在拾迹者面前更愿意直接表达疲惫和限定可见的想法"}],
                    "evidence": [f"chat_message:{chat_user.id}", f"comment_message:{comment_user.id}"],
                    "confidence": 0.86,
                }
            ],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))

        results = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")
        memory_content = soul_memory_service.read_soul_memory("拾迹者")
        reflection = require_not_none(db.query_one("SELECT type, metadata, related_posts FROM reflections WHERE type = 'soul_deep'"))
        user_revisions = db.query_all("SELECT id FROM user_md_revisions")

        self.assertEqual(1, len(results))
        self.assertEqual("拾迹者", results[0].soul_name)
        self.assertEqual({"applied": 1, "skipped": 0, "skipped_details": []}, results[0].patch_summary)
        self.assertIn("用户在拾迹者面前更愿意直接表达疲惫", memory_content)
        self.assertEqual([], user_revisions)
        metadata = json.loads(reflection["metadata"])
        self.assertEqual("拾迹者", metadata["soul_name"])
        self.assertIn(f"chat_message:{chat_user.id}", reflection["related_posts"])
        self.assertIn(f"comment_message:{comment_user.id}", reflection["related_posts"])
        prompt = client.requests[0]["messages"][1]["content"]
        self.assertIn("raw thread messages", prompt)
        self.assertIn("我在拾迹者面前会直接说累", prompt)
        self.assertIn("我听见了。", prompt)

    def test_soul_deep_reflection_skips_souls_without_new_interactions(self) -> None:
        soul_service.sync_souls()
        client = FakeClient()

        results = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")

        self.assertEqual([], results)
        self.assertEqual(0, client.calls)

    def test_soul_deep_reflection_excludes_other_soul_thread_messages(self) -> None:
        soul_service.sync_souls()
        default_thread = chat_service.get_or_create_thread("拾迹者")
        chat_service.append_user_message(default_thread.id, "default_private_marker")
        other_thread = chat_service.get_or_create_thread("毒舌好友")
        chat_service.append_user_message(other_thread.id, "other_private_marker")
        client = FakeClient()

        results = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")
        default_prompt = client.requests[0]["messages"][1]["content"]

        self.assertEqual(2, len(results))
        self.assertIn("default_private_marker", default_prompt)
        self.assertNotIn("other_private_marker", default_prompt)

    def test_soul_deep_reflection_filters_by_soul_names(self) -> None:
        soul_service.sync_souls()
        default_thread = chat_service.get_or_create_thread("拾迹者")
        chat_service.append_user_message(default_thread.id, "只反思拾迹者这条")
        other_thread = chat_service.get_or_create_thread("毒舌好友")
        chat_service.append_user_message(other_thread.id, "毒舌好友这条先不反思")
        client = FakeClient()

        results = reflector.trigger_soul_deep_reflections(
            client, "fake-model", trigger="cli_exit", soul_names=["拾迹者"]
        )

        self.assertEqual(["拾迹者"], [result.soul_name for result in results])

        # 毒舌好友 未被反思、游标未推进；之后不带过滤仍能反思到它，拾迹者则已消费。
        remaining = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit")
        remaining_names = [result.soul_name for result in remaining]
        self.assertIn("毒舌好友", remaining_names)
        self.assertNotIn("拾迹者", remaining_names)

    def test_soul_deep_reflection_cursor_prevents_rerun(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("拾迹者")
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
        cursor = require_not_none(db.query_one("SELECT value FROM meta WHERE key = ?", ("soul_thread_deep_cursor:拾迹者",)))
        self.assertEqual({"chat_message_id": chat_user.id, "comment_message_id": 0}, json.loads(cursor["value"]))

    def test_soul_deep_reflection_invalid_result_does_not_advance_cursor(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("拾迹者")
        chat_user = chat_service.append_user_message(chat_thread.id, "这条需要稍后补跑")
        bad_client = FakeClient(content=json.dumps({"reflection_md": "太短", "patches": []}, ensure_ascii=False))

        first = reflector.trigger_soul_deep_reflections(bad_client, "fake-model", trigger="cli_exit")

        self.assertEqual([], first)
        self.assertIsNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("soul_thread_deep_cursor:拾迹者",)))

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
        self.assertIsNotNone(db.query_one("SELECT value FROM meta WHERE key = ?", ("soul_thread_deep_cursor:拾迹者",)))

    def test_soul_deep_reflection_uses_message_limit(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("拾迹者")
        chat_1 = chat_service.append_user_message(chat_thread.id, "chat message 1")
        chat_2 = chat_service.append_user_message(chat_thread.id, "chat message 2")
        self._set_chat_message_time(chat_1.id, 1.0)
        self._set_chat_message_time(chat_2.id, 2.0)
        self._insert_comment_seed("20260525-root-001")
        comment_1 = comment_service.append_comment("20260525-root-001", "拾迹者", "user", "comment message 1")
        comment_2 = comment_service.append_comment("20260525-root-001", "拾迹者", "user", "comment message 2")
        self._set_comment_message_time(comment_1.id, 3.0)
        self._set_comment_message_time(comment_2.id, 4.0)

        rows = reflector._load_soul_thread_messages_since_cursor("拾迹者", 3)

        self.assertEqual(["chat message 1", "chat message 2", "comment message 1"], [row["content"] for row in rows])
        self.assertEqual([], reflector._load_soul_thread_messages_since_cursor("拾迹者", 0))

        payload = {
            "reflection_md": "## SOUL 深反思\n\n用户与拾迹者产生了多条原始互动，系统只应读取最早的三条。",
            "patches": [],
        }
        client = FakeClient(content=json.dumps(payload, ensure_ascii=False))
        results = reflector.trigger_soul_deep_reflections(client, "fake-model", trigger="cli_exit", limit_per_soul=3)
        prompt = client.requests[0]["messages"][1]["content"]

        self.assertEqual(1, len(results))
        self.assertEqual(3, results[0].interaction_count)
        self.assertIn("chat message 1", prompt)
        self.assertIn("chat message 2", prompt)
        self.assertIn("comment message 1", prompt)
        self.assertNotIn("comment message 2", prompt)

    def test_preview_soul_deep_reflection_scopes_match_pending_interactions(self) -> None:
        soul_service.sync_souls()
        chat_thread = chat_service.get_or_create_thread("拾迹者")
        chat_service.append_user_message(chat_thread.id, "这条会进入 preview")

        scopes = reflector.preview_soul_deep_reflection_scopes()

        self.assertEqual(1, len(scopes))
        self.assertEqual("拾迹者", scopes[0].soul_name)
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

    def _insert_comment_seed(self, post_id: str = "20260525-001") -> None:
        self._insert_post(post_id, "2026-05-25T10:00:00+08:00", "今天想认真练歌。")
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
            VALUES (?, ?, 'assistant', ?, 0, ?)
            """,
            (post_id, "拾迹者", "我陪你继续练。", 2.0),
        )

    def _set_chat_message_time(self, message_id: int, created_at: float) -> None:
        db.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (created_at, message_id))

    def _set_comment_message_time(self, message_id: int, created_at: float) -> None:
        db.execute("UPDATE comments SET created_at = ? WHERE id = ?", (created_at, message_id))

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
            "relations": [{"a": "小李", "b": "比赛计划", "rel_type": "teammate", "strength_delta": 0.1}],
            "importance": 0.8,
        }


if __name__ == "__main__":
    unittest.main()
