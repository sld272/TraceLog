from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
