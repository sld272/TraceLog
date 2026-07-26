from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db, soul_proactive_service
from core.llm import soul_letter_router

DAY = soul_proactive_service.DAY_SECONDS
NOW = 2_000_000_000.0


class FakeCompletions:
    def __init__(self, payloads: list[str]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.payloads.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


class FakeClient:
    def __init__(self, *payloads: str) -> None:
        self.completions = FakeCompletions(list(payloads))
        self.chat = SimpleNamespace(completions=self.completions)


class SoulProactiveDeliveryTest(unittest.TestCase):
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
        self.llm_log_patch = patch(
            "core.llm.common.logging_service.log_llm_call"
        )
        self.llm_log_patch.start()
        self.index_patch = patch(
            "core.chat_service.record_service.index_chat_message_embedding"
        )
        self.index_patch.start()

    def tearDown(self) -> None:
        self.index_patch.stop()
        self.llm_log_patch.stop()
        self.env_patch.stop()
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_silence_gate_blocks_without_any_llm_call(self) -> None:
        self._insert_soul("A")
        self._insert_post("recent", NOW - DAY)
        client = FakeClient(
            json.dumps(
                {"send": True, "message": "这条不应该生成。"},
                ensure_ascii=False,
            )
        )

        with patch.object(db, "now_ts", return_value=NOW):
            message = soul_proactive_service.run_proactive_message(
                self._config(),
                client,
                "model",
                now=NOW,
            )

        self.assertIsNone(message)
        self.assertEqual(0, len(client.completions.calls))
        self.assertEqual(0, self._count("chat_messages"))
        self.assertEqual(0, self._count("soul_letters"))

    def test_successful_send_is_atomic_and_skips_reply_side_effects(self) -> None:
        self._insert_soul("A")
        self._insert_post("p-1", NOW - 9 * DAY)
        self._insert_post("p-2", NOW - 8 * DAY)
        client = FakeClient(
            json.dumps(
                {"send": True, "message": "科一过了，这一下挺漂亮。"},
                ensure_ascii=False,
            )
        )

        with (
            patch.object(db, "now_ts", return_value=NOW),
            patch.object(soul_letter_router, "now_str", return_value="当前时间"),
            patch(
                "core.chat_service.suggestion_pipeline.collect_reply_suggestions"
            ) as collect_suggestions,
            patch(
                "core.chat_service.reply_router.call_soul_chat_reply"
            ) as generate_reply,
        ):
            message = soul_proactive_service.run_proactive_message(
                self._config(),
                client,
                "model",
                now=NOW,
            )

        self.assertIsNotNone(message)
        self.assertEqual(1, len(client.completions.calls))
        self.assertEqual(1, self._count("chat_messages"))
        self.assertEqual(1, self._count("soul_letters"))
        self.assertEqual(2, self._count("soul_message_sources"))
        row = db.query_one(
            """
            SELECT role, metadata
            FROM chat_messages
            WHERE id = ?
            """,
            (message.id,),
        )
        self.assertEqual("assistant", row["role"])
        self.assertEqual(
            {"status": "ok", "proactive_message": True},
            json.loads(row["metadata"]),
        )
        collect_suggestions.assert_not_called()
        generate_reply.assert_not_called()

    def test_letter_record_failure_rolls_back_chat_message_and_sources(
        self,
    ) -> None:
        self._insert_soul("A")
        self._insert_post("p-1", NOW - 8 * DAY)
        client = FakeClient(
            json.dumps(
                {"send": True, "message": "科一过了，这一下挺漂亮。"},
                ensure_ascii=False,
            )
        )

        def fail_after_letter_insert(
            conn,
            message_id: int,
            sent_at: float,
            *,
            material_post_ids: tuple[str, ...],
        ) -> None:
            conn.execute(
                """
                INSERT INTO soul_letters(message_id, sent_at)
                VALUES (?, ?)
                """,
                (message_id, sent_at),
            )
            raise RuntimeError("forced source failure")

        with (
            patch.object(db, "now_ts", return_value=NOW),
            patch.object(soul_letter_router, "now_str", return_value="当前时间"),
            patch.object(
                soul_proactive_service,
                "_insert_soul_letter_rows",
                side_effect=fail_after_letter_insert,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "forced source failure",
            ):
                soul_proactive_service.run_proactive_message(
                    self._config(),
                    client,
                    "model",
                    now=NOW,
                )

        self.assertEqual(0, self._count("chat_messages"))
        self.assertEqual(0, self._count("soul_letters"))
        self.assertEqual(0, self._count("soul_message_sources"))

    def test_second_letter_cannot_reuse_first_letters_material(self) -> None:
        self._insert_soul("A", sort_order=1)
        self._insert_soul("B", sort_order=2)
        self._insert_post("used-by-first", NOW - 8 * DAY)
        client = FakeClient(
            json.dumps(
                {"send": True, "message": "第一封只说旧材料。"},
                ensure_ascii=False,
            ),
            json.dumps(
                {"send": True, "message": "第二封只说新增材料。"},
                ensure_ascii=False,
            ),
        )

        with (
            patch.object(db, "now_ts", return_value=NOW),
            patch.object(soul_letter_router, "now_str", return_value="当前时间"),
        ):
            first = soul_proactive_service.run_proactive_message(
                self._config(),
                client,
                "model",
                now=NOW,
            )

        self.assertEqual(1, len(client.completions.calls))
        self._insert_post("fresh-for-second", NOW - 7 * DAY)
        with (
            patch.object(db, "now_ts", return_value=NOW + 3 * DAY),
            patch.object(soul_letter_router, "now_str", return_value="三天后"),
        ):
            second = soul_proactive_service.run_proactive_message(
                self._config(),
                client,
                "model",
                now=NOW + 3 * DAY,
            )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(2, len(client.completions.calls))
        rows = db.query_all(
            """
            SELECT chat_threads.soul_name, soul_message_sources.post_id
            FROM soul_message_sources
            JOIN chat_messages
              ON chat_messages.id = soul_message_sources.message_id
            JOIN chat_threads
              ON chat_threads.id = chat_messages.thread_id
            ORDER BY soul_message_sources.message_id
            """
        )
        self.assertEqual(
            [
                ("A", "used-by-first"),
                ("B", "fresh-for-second"),
            ],
            [
                (str(row["soul_name"]), str(row["post_id"]))
                for row in rows
            ],
        )
        second_material = client.completions.calls[1]["messages"][1]["content"]
        self.assertIn("帖子 fresh-for-second", second_material)
        self.assertNotIn("帖子 used-by-first", second_material)

    def test_zero_output_keeps_daily_scan_claim(self) -> None:
        self._insert_soul("A", sort_order=1)
        self._insert_soul("B", sort_order=2)
        self._insert_post("p-1", NOW - 8 * DAY)
        client = FakeClient(
            '{"send": false, "message": null}',
            '{"send": false, "message": null}',
        )

        with (
            patch.object(db, "now_ts", return_value=NOW),
            patch.object(soul_letter_router, "now_str", return_value="当前时间"),
        ):
            first = soul_proactive_service.run_proactive_message(
                self._config(),
                client,
                "model",
                now=NOW,
            )
        with patch.object(db, "now_ts", return_value=NOW + 60):
            second = soul_proactive_service.run_proactive_message(
                self._config(),
                client,
                "model",
                now=NOW + 60,
            )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(2, len(client.completions.calls))
        claim = db.query_one(
            "SELECT value FROM meta WHERE key = ?",
            (soul_proactive_service.LAST_SCAN_META_KEY,),
        )
        self.assertEqual(str(NOW), claim["value"])

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
        relative_path = f"souls/{name}.md"
        path = db.WORKSPACE_DIR / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n\n说话具体。", encoding="utf-8")
        db.execute(
            """
            INSERT INTO souls(
                name, file_path, enabled, sort_order, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (name, relative_path, sort_order, NOW, NOW),
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
                soul_letter_router._absolute_time(created_at),
                f"帖子 {post_id}",
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def _count(table: str) -> int:
        return int(db.query_one(f"SELECT COUNT(*) AS count FROM {table}")["count"])


if __name__ == "__main__":
    unittest.main()
