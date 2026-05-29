from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import (
    chat_service,
    comment_service,
    db,
    observation_extractor,
    observation_service,
    profile_service,
    soul_memory_service,
    soul_service,
)
from tests.helpers import require_not_none


class FakeClient:
    def __init__(self, content: str | None = None, contents: list[str] | None = None) -> None:
        self.content = content or json.dumps({"observations": []}, ensure_ascii=False)
        self.contents = list(contents or [])
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0) if self.contents else self.content
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class RaisingClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise self.exc


class ObservationExtractorTest(unittest.TestCase):
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
        (self.workspace / "user.md").write_text("# 用户档案\n\n## 身份与现状\n测试用户\n", encoding="utf-8")
        soul_service.sync_souls()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        profile_service.USER_MD_PATH = self.old_user_md_path
        soul_service.SOULS_DIR = self.old_souls_dir
        soul_service.SOUL_MEMORIES_DIR = self.old_service_memories_dir
        soul_memory_service.SOUL_MEMORIES_DIR = self.old_memory_memories_dir
        self.tmp.cleanup()

    def test_private_chat_extraction_writes_soul_scoped_observation(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        user_message = chat_service.append_user_message(thread.id, "以后和我说话少铺垫，直接一点。")
        self._append_chat_assistant(thread.id, "收到，我会更直接。")
        payload = {
            "observations": [
                {
                    "type": "correction",
                    "title": "默认少铺垫",
                    "summary": "用户要求默认回复更直接",
                    "narrative": "用户要求默认以后少铺垫，回复更直接。chat_observation_marker",
                    "importance": 0.8,
                    "confidence": 0.9,
                    "source_message_ids": [user_message.id],
                }
            ]
        }

        result = require_not_none(
            observation_extractor.extract_chat_thread_observations(
                thread.id,
                FakeClient(json.dumps(payload, ensure_ascii=False)),
                "fake-model",
            )
        )

        self.assertEqual(2, result.processed_count)
        self.assertEqual(1, result.observation_count)
        self.assertEqual(str(user_message.id + 1), result.cursor_value)
        self.assertEqual(str(user_message.id + 1), observation_service.get_cursor("chat_thread", str(thread.id)))
        rows = observation_service.list_active_observations(
            visibility_scope="soul_scoped",
            scope_soul_name="默认",
        )
        self.assertEqual(["默认少铺垫"], [row["title"] for row in rows])
        observation = require_not_none(observation_service.get_observation(rows[0]["id"]))
        self.assertEqual("chat", observation["source_channel"])
        self.assertEqual("soul_scoped", observation["visibility_scope"])
        self.assertEqual("默认", observation["scope_soul_name"])
        self.assertEqual("chat_message", observation["sources"][0]["source_type"])
        self.assertEqual(str(user_message.id), observation["sources"][0]["source_id"])
        self.assertEqual("source_soul_only", observation["sources"][0]["evidence_access"])
        self.assertEqual([], observation_service.list_active_observations(visibility_scope="global"))

    def test_comment_thread_extraction_writes_soul_scoped_observation(self) -> None:
        self._insert_post_and_comment("20260525-001", "默认", "我陪你拆一下。")
        thread = comment_service.get_or_create_thread("20260525-001", "默认")
        user_message = comment_service.append_user_message(thread.id, "这条 post 下我想继续公开聊练歌卡住的事。")
        payload = {
            "observations": [
                {
                    "type": "state",
                    "title": "练歌卡住",
                    "narrative": "用户在该 post 的评论线程里继续公开讨论练歌卡住。comment_observation_marker",
                    "importance": 0.6,
                    "confidence": 0.8,
                    "source_message_ids": [user_message.id],
                }
            ]
        }

        result = require_not_none(
            observation_extractor.extract_comment_thread_observations(
                thread.id,
                FakeClient(json.dumps(payload, ensure_ascii=False)),
                "fake-model",
            )
        )

        self.assertEqual(1, result.processed_count)
        self.assertEqual(1, result.observation_count)
        self.assertEqual(str(user_message.id), observation_service.get_cursor("comment_thread", str(thread.id)))
        rows = observation_service.list_active_observations(
            visibility_scope="soul_scoped",
            scope_soul_name="默认",
        )
        self.assertEqual(["练歌卡住"], [row["title"] for row in rows])
        observation = require_not_none(observation_service.get_observation(rows[0]["id"]))
        self.assertEqual("comment_thread", observation["source_channel"])
        self.assertEqual("soul_scoped", observation["visibility_scope"])
        self.assertEqual("20260525-001", observation["scope_post_id"])
        self.assertEqual("默认", observation["scope_soul_name"])
        self.assertEqual("comment_message", observation["sources"][0]["source_type"])
        self.assertEqual(str(user_message.id), observation["sources"][0]["source_id"])
        self.assertEqual("source_soul_only", observation["sources"][0]["evidence_access"])

    def test_empty_valid_result_advances_cursor_and_does_not_repeat(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        user_message = chat_service.append_user_message(thread.id, "只是打个招呼。")
        client = FakeClient(json.dumps({"observations": []}, ensure_ascii=False))

        first = require_not_none(observation_extractor.extract_chat_thread_observations(thread.id, client, "fake-model"))
        second = observation_extractor.extract_chat_thread_observations(thread.id, client, "fake-model")

        self.assertEqual(1, first.processed_count)
        self.assertEqual(0, first.observation_count)
        self.assertIsNone(second)
        self.assertEqual(1, len(client.calls))
        self.assertEqual(str(user_message.id), observation_service.get_cursor("chat_thread", str(thread.id)))

    def test_invalid_json_retries_without_advancing_cursor_before_threshold(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        chat_service.append_user_message(thread.id, "这条需要之后重试。")
        client = FakeClient("not json")

        first = require_not_none(observation_extractor.extract_chat_thread_observations(thread.id, client, "fake-model"))
        second = require_not_none(observation_extractor.extract_chat_thread_observations(thread.id, client, "fake-model"))
        metadata = self._cursor_metadata("chat_thread", str(thread.id))

        self.assertEqual("invalid_extraction_result_retry_1_of_3", first.error)
        self.assertEqual("invalid_extraction_result_retry_2_of_3", second.error)
        self.assertEqual("0", observation_service.get_cursor("chat_thread", str(thread.id)))
        self.assertEqual(2, metadata["failure_count"])
        self.assertEqual("invalid_llm_response", metadata["failure_kind"])
        self.assertEqual([], observation_service.list_active_observations(visibility_scope="soul_scoped"))

    def test_invalid_json_skips_poison_batch_after_threshold(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        user_message = chat_service.append_user_message(thread.id, "这条坏批次会被跳过。")
        client = FakeClient("not json")

        results = [
            require_not_none(observation_extractor.extract_chat_thread_observations(thread.id, client, "fake-model"))
            for _ in range(3)
        ]
        metadata = self._cursor_metadata("chat_thread", str(thread.id))
        next_result = observation_extractor.extract_chat_thread_observations(thread.id, client, "fake-model")
        pending_results = observation_extractor.run_pending_observation_extractions(client, "fake-model")

        self.assertFalse(results[0].skipped_poison_batch)
        self.assertFalse(results[1].skipped_poison_batch)
        self.assertTrue(results[2].skipped_poison_batch)
        self.assertEqual("skipped_poison_batch_after_3_invalid_results", results[2].error)
        self.assertEqual(str(user_message.id), observation_service.get_cursor("chat_thread", str(thread.id)))
        self.assertEqual(str(user_message.id), results[2].cursor_value)
        self.assertEqual(1, len(metadata["skipped_poison_batches"]))
        self.assertEqual([user_message.id], metadata["failed_message_ids"])
        self.assertIsNone(next_result)
        self.assertEqual([], pending_results)
        self.assertEqual([], observation_service.list_active_observations(visibility_scope="soul_scoped"))

    def test_new_messages_after_poison_skip_are_extracted_from_new_cursor(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        skipped_message = chat_service.append_user_message(thread.id, "坏批次。")
        bad_client = FakeClient("not json")
        for _ in range(3):
            observation_extractor.extract_chat_thread_observations(thread.id, bad_client, "fake-model")

        new_message = chat_service.append_user_message(thread.id, "以后回复更短一点。")
        payload = {
            "observations": [
                {
                    "type": "correction",
                    "title": "回复更短",
                    "narrative": "用户要求默认以后回复更短一点。",
                    "source_message_ids": [new_message.id],
                }
            ]
        }
        good_client = FakeClient(json.dumps(payload, ensure_ascii=False))

        result = require_not_none(observation_extractor.extract_chat_thread_observations(thread.id, good_client, "fake-model"))
        prompt = good_client.calls[0]["messages"][1]["content"]
        metadata = self._cursor_metadata("chat_thread", str(thread.id))

        self.assertEqual(1, result.processed_count)
        self.assertEqual(1, result.observation_count)
        self.assertNotIn("坏批次。", prompt)
        self.assertIn(str(new_message.id), prompt)
        self.assertEqual(str(new_message.id), observation_service.get_cursor("chat_thread", str(thread.id)))
        self.assertNotIn("failure_count", metadata)

    def test_api_error_does_not_advance_cursor_or_mark_poison_batch(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        chat_service.append_user_message(thread.id, "API 失败时不能跳过。")

        with self.assertRaises(ValueError):
            observation_extractor.extract_chat_thread_observations(
                thread.id,
                RaisingClient(RuntimeError("api down")),
                "fake-model",
            )

        self.assertIsNone(observation_service.get_cursor("chat_thread", str(thread.id)))
        self.assertEqual({}, self._cursor_metadata("chat_thread", str(thread.id)))

    def test_comment_thread_invalid_json_uses_poison_batch_policy(self) -> None:
        self._insert_post_and_comment("20260525-001", "默认", "我陪你拆一下。")
        thread = comment_service.get_or_create_thread("20260525-001", "默认")
        user_message = comment_service.append_user_message(thread.id, "这条评论坏批次会被跳过。")
        client = FakeClient("not json")

        for _ in range(2):
            result = require_not_none(observation_extractor.extract_comment_thread_observations(thread.id, client, "fake-model"))
            self.assertFalse(result.skipped_poison_batch)
        skipped = require_not_none(observation_extractor.extract_comment_thread_observations(thread.id, client, "fake-model"))

        self.assertTrue(skipped.skipped_poison_batch)
        self.assertEqual(str(user_message.id), observation_service.get_cursor("comment_thread", str(thread.id)))
        self.assertEqual([], observation_service.list_active_observations(visibility_scope="soul_scoped"))

    def test_write_failure_does_not_advance_cursor_or_leave_observation(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        user_message = chat_service.append_user_message(thread.id, "以后少用很长的解释。")
        payload = {
            "observations": [
                {
                    "type": "correction",
                    "title": "少长解释",
                    "narrative": "用户要求少用很长的解释。",
                    "source_message_ids": [user_message.id],
                }
            ]
        }

        with patch("core.observation_extractor.observation_service.save_extraction_batch", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                observation_extractor.extract_chat_thread_observations(
                    thread.id,
                    FakeClient(json.dumps(payload, ensure_ascii=False)),
                    "fake-model",
                )

        self.assertIsNone(observation_service.get_cursor("chat_thread", str(thread.id)))
        self.assertEqual([], observation_service.list_active_observations(visibility_scope="soul_scoped"))

    def test_invalid_source_message_ids_are_skipped_but_cursor_advances(self) -> None:
        thread = chat_service.get_or_create_thread("默认")
        user_message = chat_service.append_user_message(thread.id, "这条只有真实 id 才能作为证据。")
        payload = {
            "observations": [
                {
                    "type": "insight",
                    "title": "伪造证据",
                    "narrative": "这条 source id 不存在，应该跳过。",
                    "source_message_ids": [999999],
                }
            ]
        }

        result = require_not_none(
            observation_extractor.extract_chat_thread_observations(
                thread.id,
                FakeClient(json.dumps(payload, ensure_ascii=False)),
                "fake-model",
            )
        )

        self.assertEqual(1, result.processed_count)
        self.assertEqual(0, result.observation_count)
        self.assertEqual(str(user_message.id), observation_service.get_cursor("chat_thread", str(thread.id)))
        self.assertEqual([], observation_service.list_active_observations(visibility_scope="soul_scoped"))

    def test_run_pending_processes_chat_and_comment_sources(self) -> None:
        chat_thread = chat_service.get_or_create_thread("默认")
        chat_user = chat_service.append_user_message(chat_thread.id, "默认以后少铺垫。")
        self._insert_post_and_comment("20260525-001", "默认", "我陪你拆一下。")
        comment_thread = comment_service.get_or_create_thread("20260525-001", "默认")
        comment_user = comment_service.append_user_message(comment_thread.id, "这条评论线程公开继续聊。")
        client = FakeClient(
            contents=[
                json.dumps(
                    {
                        "observations": [
                            {
                                "type": "correction",
                                "title": "私聊纠正",
                                "narrative": "用户要求默认少铺垫。",
                                "source_message_ids": [chat_user.id],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "observations": [
                            {
                                "type": "state",
                                "title": "公开继续聊",
                                "narrative": "用户在该 post 评论线程公开继续聊。",
                                "source_message_ids": [comment_user.id],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            ]
        )

        results = observation_extractor.run_pending_observation_extractions(client, "fake-model")

        self.assertEqual(2, len(results))
        self.assertEqual(2, sum(result.observation_count for result in results))
        self.assertEqual(["公开继续聊", "私聊纠正"], [row["title"] for row in observation_service.list_active_observations(
            visibility_scope="soul_scoped",
            scope_soul_name="默认",
        )])

    def _append_chat_assistant(self, thread_id: int, content: str) -> int:
        cursor = db.query_one("SELECT COALESCE(MAX(created_at), 0) + 1 AS created_at FROM chat_messages")
        created_at = float(cursor["created_at"]) if cursor is not None else 1.0
        with db.transaction() as conn:
            result = conn.execute(
                """
                INSERT INTO chat_messages(thread_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, "assistant", content, created_at),
            )
            message_id = db.require_lastrowid(result, "chat assistant message insert")
            conn.execute(
                """
                UPDATE chat_threads
                SET updated_at = ?, last_message_at = ?
                WHERE id = ?
                """,
                (created_at, created_at, thread_id),
            )
        return message_id

    def _insert_post_and_comment(self, post_id: str, soul_name: str, comment: str) -> None:
        if db.query_one("SELECT 1 FROM posts WHERE id = ?", (post_id,)) is None:
            db.execute(
                """
                INSERT INTO posts(id, ts, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (post_id, "2026-05-25T10:00:00+08:00", "今天想认真练歌。", 1.0, 1.0),
            )
        db.execute(
            """
            INSERT INTO comments(post_id, soul_name, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (post_id, soul_name, comment, 2.0),
        )

    def _cursor_metadata(self, source_kind: str, source_key: str) -> dict:
        row = db.query_one(
            """
            SELECT metadata
            FROM observation_cursors
            WHERE source_kind = ? AND source_key = ?
            """,
            (source_kind, source_key),
        )
        if row is None or not row["metadata"]:
            return {}
        return json.loads(row["metadata"])


if __name__ == "__main__":
    unittest.main()
