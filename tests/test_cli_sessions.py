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

from core import chat_service, comment_service, memory_reconcile_runner
from core.cli import sessions
from core.llm.types import LLMClient


class CliSessionsTest(unittest.TestCase):
    def test_chat_session_ctrl_c_requests_quit(self) -> None:
        with (
            patch("core.cli.sessions.read_cli_input", side_effect=KeyboardInterrupt),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_chat_session(_chat_thread(), _fake_client(), "model")
        self.assertTrue(result)

    def test_comment_session_eof_requests_quit(self) -> None:
        with (
            patch("core.cli.sessions.read_cli_input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            result = sessions.run_comment_session(_comment_thread(), _fake_client(), "model")
        self.assertTrue(result)

    def test_memory_reconcile_reports_applied_operations(self) -> None:
        summary = SimpleNamespace(applied=2)
        result = memory_reconcile_runner.ReconcileRunResult([summary], [], False)
        output = StringIO()
        with (
            patch(
                "core.cli.sessions.memory_reconcile_runner.run_pending_reconcile",
                return_value=result,
            ),
            patch(
                "core.cli.sessions.memory_view_producer.refresh_views_after_reconcile",
                return_value=[object()],
            ),
            patch("core.cli.sessions.vector_index_service.rebuild_expected_docs"),
            patch("core.cli.sessions.vector_index_service.process_outbox"),
            redirect_stdout(output),
        ):
            sessions.run_memory_reconcile(_fake_client(), "model", trigger="test")
        self.assertIn("应用 2 个操作", output.getvalue())

    def test_memory_reconcile_interrupt_preserves_pending_evidence(self) -> None:
        output = StringIO()
        with (
            patch(
                "core.cli.sessions.memory_reconcile_runner.run_pending_reconcile",
                side_effect=KeyboardInterrupt,
            ),
            redirect_stdout(output),
        ):
            sessions.run_memory_reconcile(_fake_client(), "model", trigger="test")
        self.assertIn("未消费的证据会保留", output.getvalue())


def _fake_client() -> LLMClient:
    return cast(LLMClient, SimpleNamespace(chat=SimpleNamespace()))


def _chat_thread() -> chat_service.ChatThread:
    return chat_service.ChatThread(1, "拾迹者", None, 1.0, 1.0, None)


def _comment_thread() -> comment_service.CommentConversation:
    return comment_service.CommentConversation(
        "20260525-001", "拾迹者", 1, 1.0, 1.0, None
    )


if __name__ == "__main__":
    unittest.main()
