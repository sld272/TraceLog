from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

openai_stub = ModuleType("openai")
openai_stub.OpenAI = object
sys.modules.setdefault("openai", openai_stub)

from core.cli import app
from core.cli import sessions


class CliSessionsTest(unittest.TestCase):
    def test_chat_session_ctrl_c_requests_quit(self) -> None:
        thread = SimpleNamespace(id=1, soul_name="默认")
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=KeyboardInterrupt),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_chat_session(thread, object(), "model", todos)

        self.assertEqual((todos, True), result)

    def test_comment_session_ctrl_c_requests_quit(self) -> None:
        thread = SimpleNamespace(id=1, post_id="20260525-001", soul_name="默认")
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=KeyboardInterrupt),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_comment_session(thread, object(), "model", todos)

        self.assertEqual((todos, True), result)

    def test_chat_session_eof_still_requests_quit(self) -> None:
        thread = SimpleNamespace(id=1, soul_name="默认")
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_chat_session(thread, object(), "model", todos)

        self.assertEqual((todos, True), result)

    def test_comment_session_eof_still_requests_quit(self) -> None:
        thread = SimpleNamespace(id=1, post_id="20260525-001", soul_name="默认")
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_comment_session(thread, object(), "model", todos)

        self.assertEqual((todos, True), result)

    def test_exit_reflection_message_describes_current_reflection(self) -> None:
        output = StringIO()
        global_result = SimpleNamespace(
            id=7,
            related_post_ids=["p1", "p2"],
            patch_summary={"applied": 0, "skipped": 0},
        )
        soul_result = SimpleNamespace(patch_summary={"applied": 1, "skipped": 0})

        with (
            patch(
                "core.cli.sessions.reflector.preview_global_deep_reflection_scope",
                return_value=SimpleNamespace(post_ids=["p1", "p2"]),
            ),
            patch("core.cli.sessions.reflector.trigger_global_deep_reflection", return_value=global_result),
            patch(
                "core.cli.sessions.reflector.preview_soul_deep_reflection_scopes",
                return_value=[SimpleNamespace(interaction_count=3)],
            ),
            patch("core.cli.sessions.reflector.trigger_soul_deep_reflections", return_value=[soul_result]),
            patch(
                "core.cli.sessions.observation_consolidation.run_observation_consolidation",
                return_value=SimpleNamespace(
                    bucket_count=0,
                    merged_count=0,
                    superseded_count=0,
                    skipped_count=0,
                    invalid_count=0,
                ),
            ),
            redirect_stdout(output),
        ):
            sessions.run_deep_reflection_on_exit(object(), "model")

        text = output.getvalue()
        self.assertIn("正在整理本次记录与 SOUL 互动", text)
        self.assertIn("检测到 2 条尚未深反思的公开记录，正在反思", text)
        self.assertIn("检测到 3 条尚未沉淀的 SOUL 互动，正在反思", text)
        self.assertNotIn("补跑", text)
        self.assertNotIn("正在触发一次深反思", text)

    def test_exit_reflection_global_keyboard_interrupt_keeps_warning(self) -> None:
        output = StringIO()

        with (
            patch(
                "core.cli.sessions.reflector.preview_global_deep_reflection_scope",
                return_value=SimpleNamespace(post_ids=[]),
            ),
            patch("core.cli.sessions.reflector.trigger_global_deep_reflection", side_effect=KeyboardInterrupt),
            patch(
                "core.cli.sessions.reflector.preview_soul_deep_reflection_scopes",
                return_value=[],
            ),
            patch("core.cli.sessions.reflector.trigger_soul_deep_reflections", return_value=[]),
            patch(
                "core.cli.sessions.observation_consolidation.run_observation_consolidation",
                return_value=SimpleNamespace(
                    bucket_count=0,
                    merged_count=0,
                    superseded_count=0,
                    skipped_count=0,
                    invalid_count=0,
                ),
            ),
            redirect_stdout(output),
        ):
            sessions.run_deep_reflection_on_exit(object(), "model")

        self.assertIn("深反思被强制中断，已有数据保持不变", output.getvalue())

    def test_exit_reflection_runs_observation_consolidation_after_deep_reflections(self) -> None:
        output = StringIO()
        client = object()
        consolidation_result = SimpleNamespace(
            bucket_count=2,
            merged_count=3,
            superseded_count=1,
            skipped_count=0,
            invalid_count=1,
        )

        with (
            patch("core.cli.sessions.observation_extractor.run_pending_observation_extractions_safely", return_value=[]),
            patch("core.cli.sessions.reflector.preview_global_deep_reflection_scope", return_value=SimpleNamespace(post_ids=[])),
            patch("core.cli.sessions.reflector.trigger_global_deep_reflection", return_value=None),
            patch("core.cli.sessions.reflector.preview_soul_deep_reflection_scopes", return_value=[]),
            patch("core.cli.sessions.reflector.trigger_soul_deep_reflections", return_value=[]),
            patch(
                "core.cli.sessions.observation_consolidation.run_observation_consolidation",
                return_value=consolidation_result,
            ) as consolidation,
            patch("core.cli.sessions.logging_service.log_event") as log_event,
            redirect_stdout(output),
        ):
            sessions.run_deep_reflection_on_exit(client, "model")

        consolidation.assert_called_once_with(client, "model")
        text = output.getvalue()
        self.assertIn("Consolidation 已完成", text)
        self.assertIn("merged=3", text)
        log_event.assert_any_call(
            "observation_consolidation_saved",
            bucket_count=2,
            merged_count=3,
            superseded_count=1,
            skipped_count=0,
            invalid_count=1,
        )

    def test_exit_reflection_consolidation_failure_does_not_block_exit(self) -> None:
        output = StringIO()

        with (
            patch("core.cli.sessions.observation_extractor.run_pending_observation_extractions_safely", return_value=[]),
            patch("core.cli.sessions.reflector.preview_global_deep_reflection_scope", return_value=SimpleNamespace(post_ids=[])),
            patch("core.cli.sessions.reflector.trigger_global_deep_reflection", return_value=None),
            patch("core.cli.sessions.reflector.preview_soul_deep_reflection_scopes", return_value=[]),
            patch("core.cli.sessions.reflector.trigger_soul_deep_reflections", return_value=[]),
            patch("core.cli.sessions.observation_consolidation.run_observation_consolidation", side_effect=RuntimeError("boom")),
            patch("core.cli.sessions.logging_service.log_event") as log_event,
            redirect_stdout(output),
        ):
            sessions.run_deep_reflection_on_exit(object(), "model")

        self.assertIn("Consolidation 暂时失败", output.getvalue())
        self.assertIn("再见", output.getvalue())
        log_event.assert_any_call("observation_consolidation_failed", level="WARNING", error="boom")

    def test_startup_orphan_cleanup_prints_and_logs_deleted_count(self) -> None:
        output = StringIO()

        with (
            patch("core.cli.app.observation_service.cleanup_orphan_observations", return_value=2) as cleanup,
            patch("core.cli.app.logging_service.log_event") as log_event,
            redirect_stdout(output),
        ):
            app._cleanup_orphan_observations_on_startup()

        cleanup.assert_called_once_with()
        self.assertIn("已清理 2 条孤儿 observation", output.getvalue())
        log_event.assert_called_once_with("observation_orphan_cleanup", deleted_count=2)

    def test_startup_orphan_cleanup_failure_is_logged_without_printing_traceback(self) -> None:
        output = StringIO()

        with (
            patch("core.cli.app.observation_service.cleanup_orphan_observations", side_effect=RuntimeError("boom")),
            patch("core.cli.app.logging_service.log_event") as log_event,
            redirect_stdout(output),
        ):
            app._cleanup_orphan_observations_on_startup()

        self.assertEqual("", output.getvalue())
        log_event.assert_called_once()
        self.assertEqual("observation_orphan_cleanup_failed", log_event.call_args.args[0])
        self.assertEqual("WARNING", log_event.call_args.kwargs["level"])
        self.assertEqual("RuntimeError", log_event.call_args.kwargs["exception_type"])


if __name__ == "__main__":
    unittest.main()
