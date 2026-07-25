from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core import db, goal_activity_service, goal_service
from core.system_timezone import SYSTEM_TIMEZONE
from scripts import backfill_goal_activities as backfill


class BackfillGoalActivitiesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = Path(self.tmp.name) / "workspace"
        db.DB_PATH = db.WORKSPACE_DIR / "state.db"
        db.init_db()
        db.execute(
            """
            INSERT INTO souls(name, file_path, enabled, sort_order, created_at, updated_at)
            VALUES ('测试SOUL', 'souls/test.md', 1, 0, 1, 1)
            """
        )
        self.goal = goal_service.create_goal("考驾照", None, "long")

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_default_is_dry_run_and_dry_run_writes_nothing(self) -> None:
        created_at = self._timestamp(2026, 7, 17)
        self._insert_post(
            "p-dry",
            "这周二报名驾校了，最近每天刷科目一的题",
            created_at,
        )
        candidates = {
            "这周二报名驾校了，最近每天刷科目一的题": [
                self._activity(
                    self.goal["id"],
                    "progress",
                    "最近每天刷科目一的题",
                    0.9,
                )
            ]
        }

        result, output, _ = self._run(candidates)

        self.assertFalse(backfill.parse_args([]).apply)
        self.assertTrue(backfill.parse_args(["--apply"]).apply)
        self.assertEqual(1, len(result.activities))
        self.assertEqual(
            0,
            db.query_one("SELECT COUNT(*) AS count FROM goal_activities")["count"],
        )
        self.assertIsNone(goal_service.get_goal(self.goal["id"])["last_progress_at"])
        self.assertIn("2026-07-17 post:p-dry", output)
        self.assertIn("progress → 考驾照", output)
        self.assertIn("confidence=0.90", output)
        self.assertIn("引文：“最近每天刷科目一的题”", output)
        self.assertIn("产出动态：1", output)
        self.assertIn(
            "kind 分布：commitment=0，progress=1，blocked=0，milestone=0",
            output,
        )
        self.assertIn("写入：0（dry-run）", output)

    def test_backfill_threshold_is_point_eight(self) -> None:
        other_goal = goal_service.create_goal("跨专业考研", None, "long")
        content = "最近每天刷科目一的题，也在整理跨考资料"
        self._insert_post("p-threshold", content, 10.0)
        candidates = {
            content: [
                self._activity(
                    self.goal["id"],
                    "progress",
                    "最近每天刷科目一的题",
                    0.79,
                ),
                self._activity(
                    other_goal["id"],
                    "progress",
                    "在整理跨考资料",
                    0.8,
                ),
            ]
        }

        result, _, _ = self._run(candidates)

        self.assertEqual(backfill.BACKFILL_CONFIDENCE_THRESHOLD, 0.8)
        self.assertEqual([other_goal["id"]], [item.goal_id for item in result.activities])

    def test_mismatched_quote_is_dropped_by_shared_verbatim_check(self) -> None:
        content = "最近每天刷科目一的题"
        self._insert_post("p-quote", content, 10.0)
        candidates = {
            content: [
                self._activity(
                    self.goal["id"],
                    "progress",
                    "最近每天都在练车",
                    0.95,
                )
            ]
        }
        shared_check = backfill.suggestion_router._span_is_verbatim

        with patch.object(
            backfill.suggestion_router,
            "_span_is_verbatim",
            wraps=shared_check,
        ) as verbatim_check:
            result, _, _ = self._run(candidates)

        self.assertEqual((), result.activities)
        verbatim_check.assert_called_once_with("最近每天都在练车", content)

    def test_scan_only_includes_posts_and_user_comments_not_chats(self) -> None:
        self._insert_post("p-public", "公开帖子", 10.0)
        self._insert_comment("p-public", "assistant", "SOUL 回复", 11.0)
        self._insert_comment("p-public", "user", "评论区用户消息", 12.0)
        db.execute(
            """
            INSERT INTO chat_threads(
                id, soul_name, title, created_at, updated_at, last_message_at
            ) VALUES (1, '测试SOUL', '私聊', 1, 1, 1)
            """
        )
        db.execute(
            """
            INSERT INTO chat_messages(thread_id, role, content, created_at)
            VALUES (1, 'user', '私聊里的推进证据', 13)
            """
        )

        result, _, calls = self._run({})

        self.assertEqual(1, result.scanned_posts)
        self.assertEqual(1, result.scanned_comments)
        self.assertEqual(2, result.scanned_total)
        self.assertEqual(
            ["公开帖子", "评论区用户消息"],
            [call["user_input"] for call in calls],
        )
        self.assertNotIn("私聊里的推进证据", [call["user_input"] for call in calls])
        self.assertNotIn("SOUL 回复", [call["user_input"] for call in calls])

    def test_context_has_active_goal_ids_and_inactive_goal_is_filtered(self) -> None:
        inactive = goal_service.create_goal("已经完成的目标", None, "short")
        goal_service.update_goal(inactive["id"], status="done")
        content = "已经完成了这个目标"
        self._insert_post("p-inactive", content, 10.0)
        candidates = {
            content: [
                self._activity(
                    inactive["id"],
                    "milestone",
                    content,
                    0.99,
                )
            ]
        }

        result, _, calls = self._run(candidates)

        expected_context = (
            "# 当前目标\n\n" + goal_service.format_goal_for_context(self.goal)
        )
        self.assertEqual(expected_context, calls[0]["context"])
        self.assertIn(f"[{self.goal['id']}]", calls[0]["context"])
        self.assertNotIn(inactive["id"], calls[0]["context"])
        self.assertEqual((), result.activities)

    def test_apply_is_idempotent(self) -> None:
        content = "最近每天刷科目一的题"
        self._insert_post("p-repeat", content, 1234.0)
        candidates = {
            content: [
                self._activity(
                    self.goal["id"],
                    "progress",
                    content,
                    0.9,
                )
            ]
        }

        first, _, _ = self._run(candidates, apply=True)
        second, _, _ = self._run(candidates, apply=True)

        rows = db.query_all("SELECT * FROM goal_activities")
        self.assertEqual(1, len(rows))
        self.assertEqual("post:p-repeat", rows[0]["evidence_ref"])
        self.assertEqual(1234.0, rows[0]["created_at"])
        self.assertEqual(1, first.write_attempts)
        self.assertEqual(1, second.write_attempts)

    def test_apply_does_not_revive_rejected_activity(self) -> None:
        content = "最近每天刷科目一的题"
        self._insert_post("p-rejected", content, 1234.0)
        candidates = {
            content: [
                self._activity(
                    self.goal["id"],
                    "progress",
                    content,
                    0.9,
                )
            ]
        }
        self._run(candidates, apply=True)
        stored = db.query_one("SELECT * FROM goal_activities")
        goal_activity_service.reject(int(stored["id"]))

        self._run(candidates, apply=True)

        rows = db.query_all("SELECT * FROM goal_activities")
        self.assertEqual(1, len(rows))
        self.assertEqual("rejected", rows[0]["status"])
        self.assertIsNotNone(rows[0]["decided_at"])
        self.assertIsNone(goal_service.get_goal(self.goal["id"])["last_progress_at"])

    def test_apply_uses_source_created_at_for_posts_and_comments(self) -> None:
        post_content = "最近每天刷科目一的题"
        comment_content = "科目一已经考过了"
        self._insert_post("p-times", post_content, 111.0)
        comment_id = self._insert_comment(
            "p-times",
            "user",
            comment_content,
            222.0,
        )
        candidates = {
            post_content: [
                self._activity(
                    self.goal["id"],
                    "progress",
                    post_content,
                    0.9,
                )
            ],
            comment_content: [
                self._activity(
                    self.goal["id"],
                    "milestone",
                    comment_content,
                    0.95,
                )
            ],
        }

        self._run(candidates, apply=True)

        rows = db.query_all(
            "SELECT evidence_ref, created_at FROM goal_activities ORDER BY created_at"
        )
        self.assertEqual(
            [
                ("post:p-times", 111.0),
                (f"comment:{comment_id}", 222.0),
            ],
            [(row["evidence_ref"], row["created_at"]) for row in rows],
        )
        self.assertEqual(
            222.0,
            goal_service.get_goal(self.goal["id"])["last_progress_at"],
        )

    def test_model_failure_is_undetermined_and_apply_stays_atomic(self) -> None:
        self._insert_post("p-failed", "最近每天刷科目一的题", 10.0)

        result, output, _ = self._run(
            {},
            apply=True,
            statuses={"最近每天刷科目一的题": "api_error"},
        )

        self.assertEqual(("post:p-failed",), result.undetermined_refs)
        self.assertEqual(0, result.write_attempts)
        self.assertEqual(
            0,
            db.query_one("SELECT COUNT(*) AS count FROM goal_activities")["count"],
        )
        self.assertIn("未判定（模型调用状态：api_error）", output)
        self.assertNotIn("  无动态\n", output)
        self.assertIn("写入：0（存在未判定项，整批未写入）", output)

    def test_router_forwards_model_call_status(self) -> None:
        statuses: list[dict] = []

        def fake_completion(**kwargs):
            kwargs["status_callback"](
                {
                    "status": "api_error",
                    "error": {"type": "APIConnectionError"},
                }
            )
            return None

        with patch.object(
            backfill.suggestion_router,
            "call_json_completion",
            side_effect=fake_completion,
        ):
            result = backfill.suggestion_router.call_suggestion_router(
                object(),
                "model",
                user_input="最近每天刷科目一的题",
                status_callback=statuses.append,
            )

        self.assertEqual(
            {"goals": [], "events": [], "activities": []},
            result,
        )
        self.assertEqual("api_error", statuses[0]["status"])

    def _run(
        self,
        candidates: dict[str, list[dict]],
        *,
        apply: bool = False,
        statuses: dict[str, str] | None = None,
    ) -> tuple[backfill.BackfillResult, str, list[dict]]:
        output: list[str] = []
        calls: list[dict] = []
        status_by_input = statuses or {}

        def fake_router(
            client,
            model,
            *,
            user_input,
            context="",
            trace_context=None,
            status_callback=None,
        ):
            calls.append(
                {
                    "client": client,
                    "model": model,
                    "user_input": user_input,
                    "context": context,
                    "trace_context": trace_context,
                }
            )
            if status_callback is not None:
                status_callback(
                    {
                        "status": status_by_input.get(user_input, "ok"),
                        "error": None,
                    }
                )
            return {
                "goals": [],
                "events": [],
                "activities": candidates.get(user_input, []),
            }

        with patch.object(
            backfill.suggestion_router,
            "call_suggestion_router",
            side_effect=fake_router,
        ):
            result = backfill.run_backfill(
                object(),
                "model",
                apply=apply,
                emit=output.append,
            )
        return result, "\n".join(output), calls

    @staticmethod
    def _activity(
        goal_id: str,
        kind: str,
        evidence_span: str,
        confidence: float,
    ) -> dict:
        return {
            "goal_id": goal_id,
            "kind": kind,
            "evidence_span": evidence_span,
            "confidence": confidence,
        }

    def _insert_post(self, post_id: str, content: str, created_at: float) -> None:
        db.execute(
            """
            INSERT INTO posts(id, ts, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                post_id,
                datetime.fromtimestamp(
                    created_at,
                    tz=SYSTEM_TIMEZONE,
                ).isoformat(),
                content,
                created_at,
                created_at,
            ),
        )

    def _insert_comment(
        self,
        post_id: str,
        role: str,
        content: str,
        created_at: float,
    ) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO comments(
                    post_id, soul_name, role, content, seq, created_at
                ) VALUES (?, '测试SOUL', ?, ?, ?, ?)
                """,
                (
                    post_id,
                    role,
                    content,
                    int(created_at),
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _timestamp(year: int, month: int, day: int) -> float:
        return datetime(
            year,
            month,
            day,
            12,
            tzinfo=SYSTEM_TIMEZONE,
        ).timestamp()


if __name__ == "__main__":
    unittest.main()
