from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, soul_proactive_service

DAY = soul_proactive_service.DAY_SECONDS
NOW = 2_000_000_000.0


class SoulProactiveServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = Path(self.tmp.name) / "workspace"
        db.DB_PATH = db.WORKSPACE_DIR / "state.db"
        db.init_db()
        self.env_patch = patch.dict(
            os.environ,
            {soul_proactive_service.PROACTIVE_MESSAGE_DISABLED_ENV: ""},
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_default_config_and_env_hard_disable_skip_scan_state(self) -> None:
        self._insert_soul("A")
        self._insert_post("p-old", NOW - 8 * DAY)

        default_decision = soul_proactive_service.scan_for_candidates(
            {},
            now=NOW,
        )
        with patch.dict(
            os.environ,
            {soul_proactive_service.PROACTIVE_MESSAGE_DISABLED_ENV: "1"},
        ):
            env_decision = soul_proactive_service.scan_for_candidates(
                self._config(),
                now=NOW,
            )

        self.assertEqual("config_disabled", default_decision.reason)
        self.assertEqual("env_disabled", env_decision.reason)
        self.assertFalse(default_decision.should_call_llm)
        self.assertFalse(env_decision.should_call_llm)
        self.assertIsNone(
            db.query_one(
                "SELECT value FROM meta WHERE key = ?",
                (soul_proactive_service.LAST_SCAN_META_KEY,),
            )
        )

    def test_gate_a_allows_exactly_24_hours_but_not_just_before(self) -> None:
        self._insert_soul("A")
        self._insert_post("p-old", NOW - 8 * DAY)

        first = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )
        just_before = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW + DAY - 0.001,
        )
        exact = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW + DAY,
        )

        self.assertTrue(first.should_call_llm)
        self.assertEqual("scan_cooldown", just_before.reason)
        self.assertFalse(just_before.should_call_llm)
        self.assertTrue(exact.should_call_llm)

    def test_gate_b_allows_exact_week_and_blocks_just_under_week(self) -> None:
        self._insert_soul("exact", sort_order=1)
        self._insert_soul("short", sort_order=2)
        self._insert_post("p-old", NOW - 8 * DAY)
        self._record_letter("exact", NOW - 7 * DAY)
        self._record_letter("short", NOW - 7 * DAY + 0.001)

        decision = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )

        self.assertTrue(decision.should_call_llm)
        self.assertEqual(("exact",), decision.candidate_souls)

    def test_gate_c_allows_exact_silence_and_blocks_just_under(self) -> None:
        self._insert_soul("A")
        self._insert_post("p-boundary", NOW - 7 * DAY + 0.001)

        just_under = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )
        db.execute(
            "DELETE FROM meta WHERE key = ?",
            (soul_proactive_service.LAST_SCAN_META_KEY,),
        )
        db.execute(
            """
            UPDATE posts
            SET created_at = ?, updated_at = ?
            WHERE id = 'p-boundary'
            """,
            (NOW - 7 * DAY, NOW - 7 * DAY),
        )
        exact = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )

        self.assertEqual("silence_gate", just_under.reason)
        self.assertFalse(just_under.should_call_llm)
        self.assertTrue(exact.should_call_llm)
        self.assertEqual(7, exact.silent_for_days)

    def test_silence_gate_uses_latest_user_post_comment_or_chat_only(self) -> None:
        self._insert_soul("A")
        self._insert_post("p-old", NOW - 20 * DAY)
        db.execute(
            """
            INSERT INTO comments(
                post_id, soul_name, role, content, seq, created_at
            )
            VALUES ('p-old', 'A', 'user', '公开评论', 0, ?)
            """,
            (NOW - 8 * DAY,),
        )
        db.execute(
            """
            INSERT INTO comments(
                post_id, soul_name, role, content, seq, created_at
            )
            VALUES ('p-old', 'A', 'assistant', '刚发的 AI 评论', 1, ?)
            """,
            (NOW - DAY,),
        )
        thread_id = self._insert_thread("A", NOW - 7 * DAY + 0.001)
        db.execute(
            """
            INSERT INTO chat_messages(thread_id, role, content, created_at)
            VALUES (?, 'user', '用户私聊', ?)
            """,
            (thread_id, NOW - 7 * DAY + 0.001),
        )
        db.execute(
            """
            INSERT INTO chat_messages(thread_id, role, content, created_at)
            VALUES (?, 'assistant', '刚发的 AI 私聊', ?)
            """,
            (thread_id, NOW - 0.5 * DAY),
        )

        just_under = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )
        self.assertEqual("silence_gate", just_under.reason)
        self.assertAlmostEqual(
            NOW - 7 * DAY + 0.001,
            just_under.last_user_activity_at,
        )

        db.execute(
            "DELETE FROM meta WHERE key = ?",
            (soul_proactive_service.LAST_SCAN_META_KEY,),
        )
        db.execute(
            """
            UPDATE chat_messages
            SET created_at = ?
            WHERE role = 'user'
            """,
            (NOW - 7 * DAY,),
        )
        exact = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )
        self.assertTrue(exact.should_call_llm)

    def test_global_cooldown_allows_exact_three_days_but_not_just_before(
        self,
    ) -> None:
        self._insert_soul("sent")
        self._insert_soul("candidate")
        self._insert_post("p-old", NOW - 8 * DAY)
        message_id = self._record_letter(
            "sent",
            NOW - 3 * DAY + 0.001,
        )

        just_under = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )
        db.execute(
            "DELETE FROM meta WHERE key = ?",
            (soul_proactive_service.LAST_SCAN_META_KEY,),
        )
        db.execute(
            "UPDATE soul_letters SET sent_at = ? WHERE message_id = ?",
            (NOW - 3 * DAY, message_id),
        )
        exact = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )

        self.assertEqual("global_cooldown", just_under.reason)
        self.assertFalse(just_under.should_call_llm)
        self.assertTrue(exact.should_call_llm)
        self.assertEqual(("candidate",), exact.candidate_souls)

    def test_tiebreak_orders_never_sent_then_oldest_letter(self) -> None:
        self._insert_soul("oldest", sort_order=3)
        self._insert_soul("newer", sort_order=2)
        self._insert_soul("never", sort_order=1)
        self._insert_post("p-old", NOW - 8 * DAY)
        self._record_letter("oldest", NOW - 12 * DAY)
        self._record_letter("newer", NOW - 8 * DAY)

        decision = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )

        self.assertEqual(
            ("never", "oldest", "newer"),
            decision.candidate_souls,
        )
        self.assertEqual("never", decision.soul_name)

    def test_no_user_activity_blocks_before_any_llm_candidate(self) -> None:
        self._insert_soul("A")

        decision = soul_proactive_service.scan_for_candidates(
            self._config(),
            now=NOW,
        )

        self.assertEqual("no_user_activity", decision.reason)
        self.assertFalse(decision.should_call_llm)

    @staticmethod
    def _config() -> dict:
        return {
            "proactive_message": {
                "enabled": True,
                "silence_days": 7,
                "notify_desktop": True,
            }
        }

    @staticmethod
    def _insert_soul(name: str, *, sort_order: int = 0) -> None:
        db.execute(
            """
            INSERT INTO souls(
                name, file_path, enabled, sort_order, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (name, f"souls/{name}.md", sort_order, NOW, NOW),
        )

    @staticmethod
    def _insert_post(post_id: str, created_at: float) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                post_id,
                "2033-05-10T12:00:00+08:00",
                f"帖子 {post_id}",
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def _insert_thread(soul_name: str, created_at: float) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_threads(
                    soul_name, title, created_at, updated_at, last_message_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    soul_name,
                    f"与{soul_name}的私聊",
                    created_at,
                    created_at,
                    created_at,
                ),
            )
            return db.require_lastrowid(cursor, "test chat thread")

    def _record_letter(self, soul_name: str, sent_at: float) -> int:
        thread_id = self._insert_thread(soul_name, sent_at)
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_messages(thread_id, role, content, created_at)
                VALUES (?, 'assistant', '主动信', ?)
                """,
                (thread_id, sent_at),
            )
            message_id = db.require_lastrowid(cursor, "test soul letter")
            conn.execute(
                """
                INSERT INTO soul_letters(message_id, sent_at)
                VALUES (?, ?)
                """,
                (message_id, sent_at),
            )
        return message_id


if __name__ == "__main__":
    unittest.main()
