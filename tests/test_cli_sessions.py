from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import patch

openai_stub = ModuleType("openai")
setattr(openai_stub, "OpenAI", object)
sys.modules.setdefault("openai", openai_stub)

from core import chat_service, comment_service
from core.cli import sessions
from core.llm.types import LLMClient


class CliSessionsTest(unittest.TestCase):
    def test_chat_session_ctrl_c_requests_quit(self) -> None:
        thread = _chat_thread()
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=KeyboardInterrupt),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_chat_session(thread, _fake_client(), "model", todos)

        self.assertEqual((todos, True), result)

    def test_comment_session_ctrl_c_requests_quit(self) -> None:
        thread = _comment_thread()
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=KeyboardInterrupt),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_comment_session(thread, _fake_client(), "model", todos)

        self.assertEqual((todos, True), result)

    def test_chat_session_eof_still_requests_quit(self) -> None:
        thread = _chat_thread()
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_chat_session(thread, _fake_client(), "model", todos)

        self.assertEqual((todos, True), result)

    def test_comment_session_eof_still_requests_quit(self) -> None:
        thread = _comment_thread()
        todos = ["todo"]

        with (
            patch("core.cli.sessions.read_cli_input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_comment_session(thread, _fake_client(), "model", todos)

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
            redirect_stdout(output),
        ):
            sessions.run_deep_reflection_on_exit(_fake_client(), "model")

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
            redirect_stdout(output),
        ):
            sessions.run_deep_reflection_on_exit(_fake_client(), "model")

        self.assertIn("深反思被强制中断，已有数据保持不变", output.getvalue())

def _fake_client() -> LLMClient:
    return cast(LLMClient, SimpleNamespace(chat=SimpleNamespace()))


def _chat_thread() -> chat_service.ChatThread:
    return chat_service.ChatThread(
        id=1,
        soul_name="默认",
        title=None,
        created_at=1.0,
        updated_at=1.0,
        last_message_at=None,
    )


def _comment_thread() -> comment_service.CommentThread:
    return comment_service.CommentThread(
        id=1,
        post_id="20260525-001",
        soul_name="默认",
        root_comment_id=1,
        created_at=1.0,
        updated_at=1.0,
        last_message_at=None,
    )


if __name__ == "__main__":
    unittest.main()
