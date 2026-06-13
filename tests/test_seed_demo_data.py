from __future__ import annotations

import tempfile
import textwrap
import unittest
import random
from datetime import datetime
from pathlib import Path

from core import chat_service, comment_service, db, vector_index_service
from core.app_services import job_service
from scripts import seed_demo_data

EXAMPLE_PATH = seed_demo_data.SCRIPT_DIR / seed_demo_data.EXAMPLE_FILENAME


def _write_toml(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    handle.write(textwrap.dedent(text))
    handle.close()
    return Path(handle.name)


class SeedDemoContentTest(unittest.TestCase):
    def test_example_content_loads_and_is_consistent(self) -> None:
        content = seed_demo_data.load_content(EXAMPLE_PATH)

        keys = [post.key for post in content.posts]
        self.assertEqual(["sample-start", "sample-followup"], keys)
        self.assertEqual(len(keys), len(set(keys)))  # key 无重复
        key_set = set(keys)
        for thread in content.comment_threads:
            self.assertIn(thread.post_key, key_set)  # 追问都能绑定到帖子

        blob = "\n".join(post.content for post in content.posts)
        self.assertIn("示例公开记录", blob)
        self.assertIn("替换成", blob)
        self.assertIsNotNone(content.comment_threads[0].time)
        self.assertEqual(3, content.comment_threads[0].time.month if content.comment_threads[0].time else None)
        self.assertIsNotNone(content.chats[0].time)
        self.assertEqual(3, content.chats[0].time.month if content.chats[0].time else None)

    def test_build_demo_plan_orders_by_time_and_limits(self) -> None:
        content = seed_demo_data.load_content(EXAMPLE_PATH)

        plan = seed_demo_data.build_demo_plan(content.posts, year=2026, limit=4)

        self.assertEqual(2, len(plan))
        times = [item.created_at for item in plan]
        self.assertEqual(times, sorted(times))
        self.assertEqual("2026-03-01T09:00:00+08:00", plan[0].created_at.isoformat())

    def test_resolve_content_path_returns_existing_file(self) -> None:
        path = seed_demo_data.resolve_content_path(None)

        self.assertTrue(path.exists())
        self.assertIn(path.name, {seed_demo_data.CONTENT_FILENAME, seed_demo_data.EXAMPLE_FILENAME})

    def test_load_content_rejects_duplicate_keys(self) -> None:
        path = _write_toml(
            """
            [[posts]]
            key = "a"
            month = 3
            day = 1
            hour = 9
            minute = 0
            act = "x"
            content = "一"

            [[posts]]
            key = "a"
            month = 3
            day = 2
            hour = 9
            minute = 0
            act = "x"
            content = "二"
            """
        )
        self.addCleanup(path.unlink)

        with self.assertRaises(ValueError):
            seed_demo_data.load_content(path)

    def test_load_content_rejects_orphan_post_key(self) -> None:
        path = _write_toml(
            """
            [[posts]]
            key = "real"
            month = 3
            day = 1
            hour = 9
            minute = 0
            act = "x"
            content = "一"

            [[comment_threads]]
            post_key = "ghost"
            soul_name = "拾迹者"
            followup = "?"
            """
        )
        self.addCleanup(path.unlink)

        with self.assertRaises(ValueError):
            seed_demo_data.load_content(path)

    def test_load_content_rejects_partial_interaction_time(self) -> None:
        path = _write_toml(
            """
            [[posts]]
            key = "real"
            month = 3
            day = 1
            hour = 9
            minute = 0
            act = "x"
            content = "一"

            [[chats]]
            soul_name = "拾迹者"
            message = "今晚有点烦"
            month = 3
            """
        )
        self.addCleanup(path.unlink)

        with self.assertRaisesRegex(ValueError, "自定义时间字段不完整"):
            seed_demo_data.load_content(path)

    def test_cap_returns_all_items_for_negative_count(self) -> None:
        items = [1, 2, 3]

        self.assertEqual(items, seed_demo_data._cap(items, -1))
        self.assertEqual([1], seed_demo_data._cap(items, 1))


class SeedDemoDataJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = self.workspace
        db.DB_PATH = self.workspace / "state.db"
        db.init_db()
        vector_index_service.ensure_collection(
            collection_name="tracelog_test",
            embedding_config_hash="hash",
            embedding_model="embedding",
            embedding_base_url="https://example.invalid/v1",
        )

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_create_demo_post_only_enqueues_global_reflection_when_requested(self) -> None:
        when = datetime(2026, 3, 16, 21, 40).astimezone()

        first = seed_demo_data.create_demo_post("第一条", created_at=when, trigger_global_deep_reflection=False)
        first_job_types = [job["type"] for job in job_service.list_jobs_for_post(first.post_id)]
        all_job_types = [job["type"] for job in job_service.list_jobs(limit=20)]

        self.assertIn(job_service.TYPE_RUN_LIGHT_REFLECTION, first_job_types)
        self.assertNotIn(job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION, all_job_types)

        seed_demo_data.create_demo_post("第三条", created_at=when, trigger_global_deep_reflection=True)
        all_job_types = [job["type"] for job in job_service.list_jobs(limit=20)]

        self.assertIn(job_service.TYPE_TRIGGER_GLOBAL_DEEP_REFLECTION, all_job_types)

    def test_note_soul_round_enqueues_reflection_every_interval(self) -> None:
        rounds: dict[str, int] = {}

        # 前两轮不触发
        self.assertIsNone(seed_demo_data.note_soul_round_and_maybe_reflect("拾迹者", rounds))
        self.assertIsNone(seed_demo_data.note_soul_round_and_maybe_reflect("拾迹者", rounds))

        # 第三轮触发，且只针对该人格
        job_id = seed_demo_data.note_soul_round_and_maybe_reflect("拾迹者", rounds)
        self.assertIsNotNone(job_id)
        job = job_service.get_job(int(job_id))
        self.assertIsNotNone(job)
        self.assertEqual(job_service.TYPE_TRIGGER_SOUL_DEEP_REFLECTIONS, job["type"])
        self.assertEqual("demo_seed_interval", job["payload"]["trigger"])
        self.assertEqual(["拾迹者"], job["payload"]["soul_names"])

        # 另一个人格独立计数，不受影响
        self.assertIsNone(seed_demo_data.note_soul_round_and_maybe_reflect("毒舌好友", rounds))

    def test_apply_custom_interaction_times_updates_comments_and_chat_thread(self) -> None:
        original_ts = 1_700_000_000.0
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("拾迹者", "souls/拾迹者.md", 1, 0, original_ts, original_ts),
            )
            conn.execute(
                "INSERT INTO posts(id, ts, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("post-1", "2026-03-01T09:00:00+08:00", "一条 post", original_ts, original_ts),
            )
            conn.execute(
                """
                INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("post-1", "拾迹者", "assistant", "首评", 0, original_ts),
            )
            user_comment = db.require_lastrowid(
                conn.execute(
                    """
                    INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("post-1", "拾迹者", "user", "追问", 1, original_ts),
                ),
                "user comment insert",
            )
            assistant_comment = db.require_lastrowid(
                conn.execute(
                    """
                    INSERT INTO comments(post_id, soul_name, role, content, seq, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("post-1", "拾迹者", "assistant", "回复", 2, original_ts),
                ),
                "assistant comment insert",
            )
            thread_id = db.require_lastrowid(
                conn.execute(
                    """
                    INSERT INTO chat_threads(soul_name, title, created_at, updated_at, last_message_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("拾迹者", "与拾迹者的私聊", original_ts, original_ts, original_ts),
                ),
                "chat thread insert",
            )
            user_chat = db.require_lastrowid(
                conn.execute(
                    """
                    INSERT INTO chat_messages(thread_id, role, content, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (thread_id, "user", "私聊", original_ts),
                ),
                "user chat insert",
            )
            assistant_chat = db.require_lastrowid(
                conn.execute(
                    """
                    INSERT INTO chat_messages(thread_id, role, content, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (thread_id, "assistant", "私聊回复", original_ts),
                ),
                "assistant chat insert",
            )

        rng = random.Random(7)
        comment_at = datetime(2026, 3, 2, 21, 30).astimezone()
        chat_at = datetime(2026, 3, 3, 22, 5).astimezone()
        comment_result = comment_service.CommentReplyResult(
            post_id="post-1",
            soul_name="拾迹者",
            ok=True,
            reply="回复",
            user_message_id=user_comment,
            assistant_message_id=assistant_comment,
            error=None,
        )
        chat_result = chat_service.ChatReplyResult(
            thread_id=thread_id,
            soul_name="拾迹者",
            ok=True,
            reply="私聊回复",
            user_message_id=user_chat,
            assistant_message_id=assistant_chat,
            error=None,
        )

        applied_comment_ids = seed_demo_data.apply_comment_round_time(comment_result, comment_at, rng)
        chat_updates = seed_demo_data.apply_chat_round_time(chat_result, chat_at, rng)
        seed_demo_data.backfill_comment_times(rng, skip_comment_ids=applied_comment_ids)

        comment_rows = db.query_all(
            "SELECT id, created_at FROM comments WHERE id IN (?, ?) ORDER BY id ASC",
            (user_comment, assistant_comment),
        )
        chat_rows = db.query_all(
            "SELECT id, created_at FROM chat_messages WHERE id IN (?, ?) ORDER BY id ASC",
            (user_chat, assistant_chat),
        )
        thread = db.query_one(
            "SELECT created_at, updated_at, last_message_at FROM chat_threads WHERE id = ?",
            (thread_id,),
        )

        self.assertEqual({user_comment, assistant_comment}, applied_comment_ids)
        self.assertEqual(2, chat_updates)
        self.assertEqual(comment_at.timestamp(), comment_rows[0]["created_at"])
        self.assertGreater(comment_rows[1]["created_at"], comment_at.timestamp())
        self.assertEqual(chat_at.timestamp(), chat_rows[0]["created_at"])
        self.assertGreater(chat_rows[1]["created_at"], chat_at.timestamp())
        self.assertIsNotNone(thread)
        self.assertEqual(chat_at.timestamp(), thread["created_at"])
        self.assertEqual(chat_rows[1]["created_at"], thread["updated_at"])
        self.assertEqual(chat_rows[1]["created_at"], thread["last_message_at"])


if __name__ == "__main__":
    unittest.main()
