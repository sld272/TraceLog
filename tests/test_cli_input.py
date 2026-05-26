from __future__ import annotations

import unittest
from unittest.mock import patch

from core.cli_input import _display_width, read_cli_input


class CliInputTest(unittest.TestCase):
    def test_display_width_counts_cjk_as_double_width(self) -> None:
        self.assertEqual(4, _display_width("你: "))
        self.assertEqual(7, _display_width("测试abc"))

    def test_display_width_ignores_combining_marks(self) -> None:
        self.assertEqual(1, _display_width("e\u0301"))

    def test_read_cli_input_uses_plain_input_on_windows(self) -> None:
        with (
            patch("core.cli_input._is_windows", return_value=True),
            patch("core.cli_input._LineEditor") as editor_cls,
            patch("builtins.input", return_value="中文输入") as input_fn,
        ):
            result = read_cli_input("你: ")

        self.assertEqual("中文输入", result)
        input_fn.assert_called_once_with("你: ")
        editor_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
