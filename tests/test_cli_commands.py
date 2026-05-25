from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO

from core.cli import commands


class CliCommandsTest(unittest.TestCase):
    def test_non_command_inputs_are_not_handled(self) -> None:
        self.assertFalse(commands.handle_tool_command("今天想发一条普通 post"))
        handled, todos, quit_requested = commands.handle_chat_command("今天想发一条普通 post", None, "model", [])
        self.assertEqual((False, [], False), (handled, todos, quit_requested))
        handled, todos, quit_requested = commands.handle_comment_command("今天想发一条普通 post", None, "model", [])
        self.assertEqual((False, [], False), (handled, todos, quit_requested))

    def test_incomplete_chat_and_comment_commands_show_help_and_do_not_quit(self) -> None:
        with redirect_stdout(StringIO()):
            chat_result = commands.handle_chat_command("/chat", None, "model", ["todo"])
            comment_result = commands.handle_comment_command("/comment", None, "model", ["todo"])

        self.assertEqual((True, ["todo"], False), chat_result)
        self.assertEqual((True, ["todo"], False), comment_result)


if __name__ == "__main__":
    unittest.main()
